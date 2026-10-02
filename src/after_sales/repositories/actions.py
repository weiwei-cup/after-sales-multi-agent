"""Approved mock effects with latest-fact checks and an atomic idempotency ledger."""

import json
from datetime import UTC, datetime
from pathlib import Path

from after_sales.agents.contracts import ActionReceipt, PendingInput, ResumeInput
from after_sales.agents.review import digest
from after_sales.domain.models import (
    ActionType,
    AfterSalesRecord,
    DeliveryProof,
    Product,
    TicketStatus,
    TrackingEvent,
    utc_text,
)
from after_sales.domain.rules import RULES_VERSION, Truth, evaluate_policy, policy_conflicts
from after_sales.repositories.budgets import check_cancelled
from after_sales.repositories.run_store import DurableInputConflict, validate_bindings
from after_sales.repositories.sqlite import _order, _policies, _ticket, read_database, transaction
from after_sales.tools.evidence import canonical


class ApprovalStale(ValueError):
    """No effect was written; refresh facts and request a new approval."""


def operation_payload(ticket_id, input_revision, candidate):
    return {
        "ticket_id": ticket_id,
        "input_revision": input_revision,
        "type": candidate.type.value,
        "order_id": candidate.order_id,
        "amount_cents": candidate.amount_cents,
    }


def operation_key(payload):
    # An amount edit does not create a second operation for the same business request.
    return "op-" + digest({k: v for k, v in payload.items() if k != "amount_cents"})[:40]


def latest_facts(connection, order):
    products = [
        Product.model_validate(dict(r))
        for r in connection.execute(
            "SELECT p.* FROM products p JOIN order_items i ON p.id=i.product_id "
            "WHERE i.order_id=? ORDER BY p.id",
            (order.id,),
        )
    ]
    tracking = [
        TrackingEvent.model_validate(dict(r))
        for r in connection.execute(
            "SELECT * FROM tracking_events WHERE order_id=? ORDER BY occurred_at,id", (order.id,)
        )
    ]
    row = connection.execute(
        "SELECT * FROM delivery_proofs WHERE order_id=?", (order.id,)
    ).fetchone()
    proof = DeliveryProof.model_validate(dict(row)) if row else None
    history = [
        AfterSalesRecord.model_validate(dict(r))
        for r in connection.execute(
            "SELECT id,order_id,type,status,amount_cents,created_at,version FROM "
            "after_sales_history WHERE order_id=? ORDER BY created_at,id",
            (order.id,),
        )
    ]
    facts = {
        "order": order.model_dump(mode="json"),
        "products": {"products": [p.model_dump(mode="json") for p in products]},
        "tracking": {"events": [p.model_dump(mode="json") for p in tracking]},
        "history": {"records": [p.model_dump(mode="json") for p in history]},
        "proof": {
            "proof": proof.model_dump(mode="json") if proof else None,
            "availability": "available" if proof else "not_collected",
            "proof_status": proof.proof_status if proof else "unknown",
        },
    }
    return facts, products, tracking, history


class ActionService:
    def __init__(self, path: Path, *, fault=None):
        self.path = path
        self.fault = fault or (lambda stage: None)

    def lookup(self, payload):
        with read_database(self.path) as connection:
            return self._existing(connection, payload)

    def _existing(self, connection, payload):
        row = connection.execute(
            "SELECT * FROM action_ledger WHERE operation_key=?", (operation_key(payload),)
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != digest(payload) or row["payload_json"] != canonical(payload):
            raise DurableInputConflict("operation key already committed with a different payload")
        return ActionReceipt.model_validate_json(row["receipt_json"])

    def execute(
        self, run_id: str, pending_id: str, binding, *, now: datetime | None = None, clock=None
    ):
        with transaction(self.path) as connection:
            stored = connection.execute(
                "SELECT p.*,h.envelope_json,h.ticket_json FROM pending_inputs p JOIN human_inputs "
                "h USING(pending_id) WHERE pending_id=? AND run_id=?",
                (pending_id, run_id),
            ).fetchone()
            if stored is None or stored["status"] not in {"consumed", "invalidated"}:
                raise DurableInputConflict("execution requires a consumed durable approval")
            pending = PendingInput.model_validate_json(stored["payload_json"])
            request = ResumeInput.model_validate_json(stored["envelope_json"])
            validate_bindings(request, pending)
            if (
                request.decision != "approve"
                or request.role != "operator"
                or binding not in pending.actions
            ):
                raise DurableInputConflict("operator did not approve this action")
            payload = operation_payload(
                pending.ticket_id, pending.input_revision, binding.candidate
            )
            existing = self._existing(connection, payload)
            if existing:
                return existing
            check_cancelled(connection, run_id)
            if stored["status"] == "invalidated":
                raise ApprovalStale("approval already invalidated; refresh required")
            plan = connection.execute(
                "SELECT * FROM proposal_plans WHERE run_id=? AND active=1", (run_id,)
            ).fetchone()
            if plan is None or (
                plan["proposal_revision"],
                plan["input_revision"],
                plan["proposal_hash"],
            ) != (pending.proposal_revision, pending.input_revision, pending.proposal_hash):
                raise ApprovalStale("approved plan is no longer active")
            state = json.loads(plan["payload_json"])
            if (
                digest(state["proposal"]) != pending.proposal_hash
                or not state["validation"]["ok"]
                or state["review"]["outcome"] != "accept"
                or [a.candidate.model_dump(mode="json") for a in pending.actions]
                != state["proposal"]["actions"]
            ):
                raise DurableInputConflict("approval has no safe matching stored plan")
            if any(
                a["facts"]["rules_version"] != RULES_VERSION
                for a in state["policy_assessment"]["assessments"]
            ):
                raise ApprovalStale("rules version changed")
            ticket = _ticket(connection, pending.ticket_id)
            if (
                ticket.input_revision,
                ticket.customer_id,
                ticket.type.value,
                ticket.supplied_order_id,
            ) != (
                pending.input_revision,
                json.loads(stored["ticket_json"])["customer_id"],
                state["input"]["intent"],
                binding.candidate.order_id,
            ):
                raise ApprovalStale("ticket input or identity changed")
            order = _order(connection, binding.candidate.order_id, ticket.customer_id)
            current, products, tracking, history = latest_facts(connection, order)
            approved = {f["source_type"]: f["facts"] for f in state["order_findings"]["facts"]}
            if current != approved:
                raise ApprovalStale("business facts changed after the displayed proposal")
            # Sample after the write lock: waiting for a busy DB can cross a deadline.
            now = clock() if clock else now if now is not None else datetime.now(UTC)
            policies = _policies(
                connection,
                intent=ticket.type,
                as_of_time=now,
                product_ids=tuple(i.product_id for i in order.items),
            )
            old_policies = [p["facts"] for p in state["policy_assessment"]["policy_evidence"]]
            if [p.model_dump(mode="json") for p in policies] != old_policies:
                raise ApprovalStale("applicable policy set or contents changed")
            applicable = tuple(p for p in policies if binding.candidate.type in p.allowed_actions)
            if not applicable or policy_conflicts(applicable):
                raise ApprovalStale("policy missing or conflicting")
            for policy in applicable:
                evaluation = evaluate_policy(
                    policy,
                    binding.candidate.type,
                    intent=ticket.type,
                    order=order,
                    products=tuple(products),
                    tracking=tuple(tracking),
                    history=tuple(history),
                    as_of_time=now,
                    requested_amount_cents=binding.candidate.amount_cents,
                )
                if evaluation.eligibility != Truth.TRUE or evaluation.existing_record_ids:
                    raise ApprovalStale("execution-time rules no longer permit this action")
            action = binding.candidate
            key = operation_key(payload)
            status = {
                ActionType.LOGISTICS_CASE: TicketStatus.PROCESSING,
                ActionType.RETURN_REQUEST: TicketStatus.WAITING_RETURN,
                ActionType.MOCK_REFUND: TicketStatus.RESOLVED,
            }[action.type]
            receipt = ActionReceipt(
                operation_key=key,
                payload_hash=digest(payload),
                run_id=run_id,
                ticket_id=ticket.id,
                action_id=binding.action_id,
                input_revision=pending.input_revision,
                type=action.type,
                order_id=order.id,
                amount_cents=action.amount_cents,
                business_record_id="act-" + digest(key)[:32],
                business_status=status,
                committed_at=now,
            )
            connection.execute(
                "INSERT INTO action_ledger VALUES (?,?,?,?,?,?,?,?)",
                (
                    key,
                    run_id,
                    ticket.id,
                    binding.action_id,
                    digest(payload),
                    canonical(payload),
                    receipt.model_dump_json(),
                    utc_text(now),
                ),
            )
            if action.type == ActionType.MOCK_REFUND:
                changed = connection.execute(
                    "UPDATE orders SET "
                    "refunded_cents=refunded_cents+?,version=version+1 "
                    "WHERE id=? AND version=? AND refunded_cents+?<=paid_cents",
                    (action.amount_cents, order.id, order.version, action.amount_cents),
                ).rowcount
                if changed != 1:
                    raise ApprovalStale("refund balance changed")
            connection.execute(
                "INSERT INTO after_sales_history VALUES (?,?,?,?,?,?,?,?)",
                (
                    receipt.business_record_id,
                    order.id,
                    action.type.value,
                    "completed" if action.type == ActionType.MOCK_REFUND else "active",
                    action.amount_cents,
                    utc_text(now),
                    1,
                    key,
                ),
            )
            connection.execute(
                "UPDATE tickets SET order_id=?,status=?,version=version+1 WHERE id=?",
                (order.id, status.value, ticket.id),
            )
            connection.execute(
                "INSERT INTO action_events VALUES (?,'action_committed',?)",
                (key, receipt.model_dump_json()),
            )
            self.fault("before_commit")
        self.fault("after_commit")
        return receipt
