"""Durable application records; graph checkpoints live in their separate SQLite file."""

import json
from datetime import datetime
from pathlib import Path

from after_sales.agents.contracts import Confirmation, PendingInput, ResumeInput
from after_sales.agents.review import digest
from after_sales.domain.models import Ticket, TicketMessage, TicketStatus, utc_text
from after_sales.repositories.errors import RecordNotFound
from after_sales.repositories.sqlite import _ticket, read_database, transaction
from after_sales.tools.evidence import canonical

WORKFLOW_VERSION = "persistent-review-v1"
STATE_VERSION = "persistent-state-v1"


class IncompatibleRun(ValueError):
    pass


class DurableInputConflict(ValueError):
    pass


def validate_bindings(request: ResumeInput, pending: PendingInput):
    actual = (
        request.run_id,
        request.ticket_id,
        request.role,
        request.actor_id,
        request.input_revision,
        request.proposal_revision,
        request.proposal_hash,
        request.action_hashes,
    )
    expected = (
        pending.run_id,
        pending.ticket_id,
        pending.expected_role,
        pending.expected_actor,
        pending.input_revision,
        pending.proposal_revision,
        pending.proposal_hash,
        {a.action_id: a.content_hash for a in pending.actions},
    )
    if actual != expected or request.pending_id != pending.pending_id:
        raise DurableInputConflict("durable input bindings do not match")
    if request.role == "customer" and set(request.answers) != {q.field for q in pending.questions}:
        raise DurableInputConflict("answers do not match the durable questions")


class RunStore:
    workflow_version = WORKFLOW_VERSION
    state_version = STATE_VERSION

    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id

    def register_extra(self, connection):
        pass

    def register(self, ticket, context, checkpoint_path, limits, runtime):
        with transaction(self.path) as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id=?", (self.run_id,)).fetchone():
                raise DurableInputConflict("run ID already exists; use resume")
            active = connection.execute(
                "SELECT id FROM runs WHERE ticket_id=? AND status IN "
                "('queued','running','paused','interrupted')",
                (ticket.id,),
            ).fetchone()
            if active:
                raise DurableInputConflict(f"ticket already has an active run; resume {active[0]}")
            connection.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    ticket.id,
                    self.run_id,
                    "queued",
                    utc_text(context.as_of_time),
                    utc_text(context.as_of_time),
                    1,
                ),
            )
            connection.execute(
                "INSERT INTO workflow_runtime VALUES (?,?,?,?,?,?,?,0)",
                (
                    self.run_id,
                    self.workflow_version,
                    self.state_version,
                    "scripted",
                    str(checkpoint_path.resolve()),
                    canonical(limits),
                    canonical(runtime),
                ),
            )
            self.register_extra(connection)

    def load(self, checkpoint_path):
        with read_database(self.path) as connection:
            if connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                0
            ] not in (2, 3):
                raise IncompatibleRun(
                    "P06 requires business schema v2; use db migrate before starting"
                )
            row = connection.execute(
                "SELECT w.*,r.ticket_id,r.status FROM workflow_runtime w "
                "JOIN runs r ON r.id=w.run_id WHERE run_id=?",
                (self.run_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("persistent run not found; a JSON report cannot restore it")
            values = dict(row)
        if (values["workflow_version"], values["schema_version"], values["model_mode"]) != (
            self.workflow_version,
            self.state_version,
            "scripted",
        ):
            raise IncompatibleRun(
                "workflow/schema/model version incompatible; stored data preserved"
            )
        if values["checkpoint_path"] != str(checkpoint_path.resolve()):
            raise IncompatibleRun("checkpoint path differs from this run's registered path")
        values["runtime"] = json.loads(values["runtime_json"])
        values["limits"] = json.loads(values["limits_json"])
        return values

    def save_runtime(self, runtime, *, status=None, terminal_error=None):
        with transaction(self.path) as connection:
            connection.execute(
                "UPDATE workflow_runtime SET runtime_json=? WHERE run_id=?",
                (canonical(runtime), self.run_id),
            )
            if terminal_error is not None:
                connection.execute(
                    "UPDATE workflow_runtime SET terminal_error=? WHERE run_id=?",
                    (int(terminal_error), self.run_id),
                )
            if status is not None:
                stored = (
                    "completed"
                    if status == "handed_off"
                    else "failed"
                    if status == "skipped"
                    else status
                )
                connection.execute("UPDATE runs SET status=? WHERE id=?", (stored, self.run_id))

    def save_plan(self, state):
        revision, payload = state["proposal_revision"], canonical(state)
        with transaction(self.path) as connection:
            old = connection.execute(
                "SELECT payload_json FROM proposal_plans WHERE run_id=? AND proposal_revision=?",
                (self.run_id, revision),
            ).fetchone()
            # Replays may refresh diagnostics, never the same proposal version.
            if old is not None and json.loads(old[0])["proposal"] != state["proposal"]:
                raise DurableInputConflict("same proposal revision has different contents")
            connection.execute("UPDATE proposal_plans SET active=0 WHERE run_id=?", (self.run_id,))
            connection.execute(
                "INSERT INTO proposal_plans VALUES (?,?,?,?,?,1) "
                "ON CONFLICT(run_id,proposal_revision) DO UPDATE SET "
                "active=1,payload_json=excluded.payload_json",
                (
                    self.run_id,
                    revision,
                    state["input_revision"],
                    digest(state["proposal"]),
                    payload,
                ),
            )

    def save_pending(self, pending: PendingInput):
        encoded = canonical(pending.model_dump(mode="json"))
        with transaction(self.path) as connection:
            old = connection.execute(
                "SELECT payload_json FROM pending_inputs WHERE pending_id=?", (pending.pending_id,)
            ).fetchone()
            if old is not None:
                if old[0] != encoded:
                    raise DurableInputConflict("pending ID content conflict")
                return
            connection.execute(
                "UPDATE pending_inputs SET status='superseded' WHERE run_id=? AND status='open'",
                (self.run_id,),
            )
            connection.execute(
                "INSERT INTO pending_inputs VALUES (?,?,?,'open')",
                (pending.pending_id, self.run_id, encoded),
            )
            status = (
                TicketStatus.WAITING_CUSTOMER
                if pending.kind == "customer_info"
                else TicketStatus.WAITING_REVIEW
            )
            connection.execute(
                "UPDATE tickets SET status=?,version=version+1 WHERE id=?",
                (status.value, pending.ticket_id),
            )

    def history(self):
        with read_database(self.path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM pending_inputs WHERE run_id=? ORDER BY rowid", (self.run_id,)
                )
            ]

    def reply(self, pending_id):
        with read_database(self.path) as connection:
            row = connection.execute(
                "SELECT h.* FROM human_inputs h JOIN pending_inputs p USING(pending_id) "
                "WHERE pending_id=? AND run_id=?",
                (pending_id, self.run_id),
            ).fetchone()
            return dict(row) if row else None

    def confirmations(self):
        results = []
        for row in self.history():
            reply = self.reply(row["pending_id"])
            if reply is None:
                continue
            request = ResumeInput.model_validate_json(reply["envelope_json"])
            if request.role == "operator":
                results.append(
                    Confirmation(
                        pending_id=request.pending_id,
                        actor_id=request.actor_id,
                        decision=request.decision,
                        input_revision=request.input_revision,
                        proposal_revision=request.proposal_revision,
                        proposal_hash=request.proposal_hash,
                        action_hashes=request.action_hashes,
                    )
                )
        return results

    def accept(self, request: ResumeInput, ticket: Ticket, now: datetime):
        from after_sales.repositories.budgets import check_cancelled

        encoded = canonical(request.model_dump(mode="json"))
        with transaction(self.path) as connection:
            check_cancelled(connection, self.run_id)
            row = connection.execute(
                "SELECT * FROM pending_inputs WHERE pending_id=? AND run_id=?",
                (request.pending_id, self.run_id),
            ).fetchone()
            if row is None:
                raise DurableInputConflict("pending not found")
            old = connection.execute(
                "SELECT envelope_json,ticket_json FROM human_inputs WHERE pending_id=?",
                (request.pending_id,),
            ).fetchone()
            if old:
                if old[0] != encoded:
                    raise DurableInputConflict("consumed pending cannot accept different input")
                return Ticket.model_validate_json(old[1])
            if row["status"] != "open":
                raise DurableInputConflict("pending is no longer open")
            pending = PendingInput.model_validate_json(row["payload_json"])
            validate_bindings(request, pending)
            current = _ticket(connection, ticket.id)
            if (current.customer_id, current.type, current.input_revision) != (
                ticket.customer_id,
                ticket.type,
                request.input_revision,
            ):
                raise DurableInputConflict("ticket input or identity changed; old pending invalid")
            updated = ticket
            if request.role == "customer":
                revision = ticket.input_revision + 1
                messages = tuple(
                    TicketMessage(
                        id="msg-" + digest([self.run_id, request.pending_id, field])[:32],
                        ticket_id=ticket.id,
                        role="customer",
                        content=f"{field}: {value}",
                        created_at=now,
                    )
                    for field, value in request.answers.items()
                )
                values = ticket.model_dump(mode="python")
                values.update(
                    input_revision=revision,
                    version=current.version + 1,
                    messages=(*ticket.messages, *messages),
                    status=TicketStatus.PROCESSING,
                )
                if "order_id" in request.answers:
                    values.update(supplied_order_id=request.answers["order_id"], order_id=None)
                updated = Ticket.model_validate(values)
                connection.execute(
                    "UPDATE tickets SET "
                    "supplied_order_id=?,order_id=?,input_revision=?,version=?,status=? WHERE id=?",
                    (
                        updated.supplied_order_id,
                        updated.order_id,
                        revision,
                        updated.version,
                        updated.status.value,
                        ticket.id,
                    ),
                )
                for message in messages:
                    connection.execute(
                        "INSERT INTO ticket_messages VALUES (?,?,?,?,?)",
                        (message.id, ticket.id, "customer", message.content, utc_text(now)),
                    )
            connection.execute(
                "INSERT INTO human_inputs VALUES (?,?,?,?)",
                (request.pending_id, encoded, updated.model_dump_json(), utc_text(now)),
            )
            connection.execute(
                "UPDATE pending_inputs SET status='consumed' WHERE pending_id=?",
                (request.pending_id,),
            )
        return updated

    def invalidate(self):
        with transaction(self.path) as connection:
            connection.execute("UPDATE proposal_plans SET active=0 WHERE run_id=?", (self.run_id,))
            connection.execute(
                "UPDATE pending_inputs SET status='invalidated' WHERE run_id=? AND "
                "status IN ('open','consumed')",
                (self.run_id,),
            )

    def receipts(self):
        with read_database(self.path) as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT receipt_json FROM action_ledger WHERE run_id=? ORDER BY rowid",
                    (self.run_id,),
                )
            ]
