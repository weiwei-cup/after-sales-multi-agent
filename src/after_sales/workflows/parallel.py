"""P07 independent candidate discovery and order investigation meet at a checked join."""

import copy
import json
from typing import Annotated, Literal

from after_sales.agents.baseline_script import call
from after_sales.agents.bounded import BoundedTelemetry, error_category
from after_sales.agents.review import create_review_model, digest
from after_sales.agents.roles import business_outputs
from after_sales.agents.scripted import ScriptedChatModel, ScriptStep
from after_sales.agents.telemetry import CallLimitExceeded
from after_sales.domain.models import DomainModel, Identifier
from after_sales.repositories.budgets import BudgetStore, RunCancelled
from after_sales.repositories.run_store import RunStore
from after_sales.repositories.sqlite import read_database, transaction
from after_sales.tools.contracts import EvidenceRef, ToolResult
from after_sales.tools.evidence import canonical
from after_sales.workflows.contracts import Role
from after_sales.workflows.durable import (
    LIMIT_FIELDS,
    PersistentReviewRun,
    PersistentRuntime,
    PersistentState,
)
from after_sales.workflows.reducers import merge_evidence, merge_results, result_conflicts
from after_sales.workflows.reviewed import ResumeRejected
from after_sales.workflows.serial import HandoffRejected


class PolicyCandidates(DomainModel):
    ticket_id: Identifier
    result: ToolResult
    applicability: Literal["not_evaluated"] = "not_evaluated"


class ParallelState(PersistentState):
    task_results: Annotated[list[dict], merge_results]
    evidence_join: Annotated[dict, merge_evidence]
    policy_candidates: dict | None
    parallel_plan: list[dict]


class ParallelStore(RunStore):
    workflow_version = "parallel-review-v1"
    state_version = "parallel-state-v1"

    def register_extra(self, connection):
        connection.execute("INSERT INTO run_control(run_id) VALUES (?)", (self.run_id,))
        connection.execute("INSERT INTO run_budget(run_id) VALUES (?)", (self.run_id,))

    def branch_results(self, task_id):
        with read_database(self.path) as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM branch_results WHERE run_id=? AND task_id=? "
                    "ORDER BY result_hash",
                    (self.run_id, task_id),
                )
            ]
        if any(
            digest(json.loads(row["result_json"])) != row["result_hash"]
            or json.loads(row["result_json"])["task_id"] != task_id
            for row in rows
        ):
            raise HandoffRejected("durable branch result hash or task binding mismatch")
        return rows

    def record_branch(self, result, evidence):
        with transaction(self.path) as db:
            db.execute(
                "INSERT OR IGNORE INTO branch_results VALUES (?,?,?,?,?)",
                (
                    self.run_id,
                    result["task_id"],
                    digest(result),
                    canonical(result),
                    canonical(evidence),
                ),
            )


def parallel_model(role, stage, payload):
    if stage != "policy_candidates":
        return create_review_model(role, stage, payload)

    def finish(messages, *, repair=False):
        result = business_outputs(messages, ("search_policy_candidates",))[
            "search_policy_candidates"
        ]
        return call(
            "PolicyCandidates",
            PolicyCandidates(ticket_id=payload["ticket_id"], result=result).model_dump(mode="json"),
            "candidates-repair" if repair else "candidates-output",
        )

    return ScriptedChatModel(
        script_id="policy-candidates-v1",
        steps=(
            ScriptStep(
                "search",
                response=call(
                    "search_policy_candidates", {"intent": payload["intent"]}, "candidates-read"
                ),
            ),
            ScriptStep("output", ("search_policy_candidates",), finish),
            ScriptStep("repair", ("PolicyCandidates",), lambda m: finish(m, repair=True)),
        ),
    )


class ParallelRuntime(PersistentRuntime):
    async def intake(self, state, config):
        update = await super().intake(state, config)
        update["parallel_plan"] = (
            []
            if update["route"] == "draft"
            else [
                {
                    "task_id": f"{state['input_revision']}:order",
                    "node": "order_branch",
                    "depends_on": [],
                },
                {
                    "task_id": f"{state['input_revision']}:policy_candidates",
                    "node": "candidate_branch",
                    "depends_on": [],
                },
                {
                    "task_id": f"{state['input_revision']}:policy_rules",
                    "node": "policy",
                    "depends_on": ["order", "policy_candidates"],
                },
            ]
        )
        return update

    async def repair(self, state, config):
        count = self.trace.ledger.reserve_review_repair()
        if count is None:
            return {"route": "handoff", "reason": "REPAIR_LIMIT_REACHED"}
        self.trace.event(
            "review_repair", role="application", attempt=count, feedback=state["review"]
        )
        return {
            "repair_count": count,
            "route": "research" if state["review"]["outcome"] == "research" else "draft",
        }

    async def branch(self, state, config, kind):
        task_id = f"{state['input_revision']}:{kind}"
        stored = self.owner.store.branch_results(task_id)
        if stored:
            for row in stored:
                self.session.evidence.restore_trusted(
                    json.loads(row["evidence_json"]), self.session.context
                )
            self.trace.event(
                "branch_replayed", role="application", task_id=task_id, variants=len(stored)
            )
            return {
                "task_results": [json.loads(row["result_json"]) for row in stored],
                "evidence_join": {"items": self.session.evidence.export(self.session.context)},
            }
        self.trace.event("branch_started", role="application", task_id=task_id)
        try:
            if kind == "order":
                output = (await super().order(state, config))["order_findings"]
            else:
                payload = {"ticket_id": state["ticket_id"], "intent": state["intake"]["intent"]}
                result, actual = await self.invoke_role(
                    Role.POLICY,
                    "policy_candidates",
                    payload,
                    PolicyCandidates,
                    ("search_policy_candidates",),
                    config,
                )
                if (
                    result.ticket_id != state["ticket_id"]
                    or actual.get("search_policy_candidates") != result.result
                ):
                    raise HandoffRejected("candidate output must match actual discovery")
                output = result.model_dump(mode="json")
            row = {"task_id": task_id, "kind": kind, "status": "completed", "output": output}
        except RunCancelled:
            raise
        except Exception as error:
            row = {
                "task_id": task_id,
                "kind": kind,
                "status": "failed",
                "output": None,
                "error_code": type(error).__name__,
                "category": error_category(error),
            }
        self.owner.store.record_branch(row, self.session.evidence.export(self.session.context))
        self.trace.event(
            "branch_finished",
            role="application",
            task_id=task_id,
            status=row["status"],
            decision_summary="preserve completed evidence and join before applicability",
        )
        return {
            "task_results": [row],
            "evidence_join": {"items": self.session.evidence.export(self.session.context)},
        }

    async def order_branch(self, state, config):
        return await self.branch(state, config, "order")

    async def candidate_branch(self, state, config):
        return await self.branch(state, config, "policy_candidates")

    async def join(self, state, config):
        current = [
            row
            for row in state["task_results"]
            if row["task_id"].startswith(f"{state['input_revision']}:")
        ]
        conflicts = result_conflicts(current)
        evidence_conflicts = state["evidence_join"].get("conflicts", [])
        by_kind = {r["kind"]: r for r in current}
        order = by_kind.get("order", {})
        policy = by_kind.get("policy_candidates", {})
        findings, candidates = order.get("output"), policy.get("output")
        failed = len(by_kind) != 2 or any(r["status"] == "failed" for r in current)
        route = (
            "handoff"
            if failed or conflicts or evidence_conflicts
            else "policy"
            if findings["outcome"] == "ready"
            else "draft"
        )
        self.trace.event(
            "join_decided",
            role="application",
            tasks=[r["task_id"] for r in current],
            conflicts=conflicts,
            evidence_conflicts=evidence_conflicts,
            route=route,
            decision_summary="branches checked; applicability waits for owned order facts",
        )
        return {
            "order_findings": findings,
            "policy_candidates": candidates,
            "route": route,
            "reason": "BRANCH_FAILED_OR_CONFLICT" if route == "handoff" else None,
        }

    async def policy(self, state, config):
        # Candidate set is an observed snapshot, not evidence of eligibility. Existing scoped
        # policy tools and pure rules still compute applicability and re-discover omitted conflicts.
        candidates = state.get("policy_candidates")
        if candidates and candidates["result"]["ok"]:
            for ref in candidates["result"]["evidence_refs"]:
                self.session.evidence.resolve(
                    EvidenceRef.model_validate(ref), self.session.context, "policy"
                )
        update = await super().policy(state, config)
        if candidates and not candidates["result"]["ok"]:
            # Don't let a second search hide the failed independent branch.
            raise HandoffRejected("candidate discovery failed; manual review required")
        applicable = update["policy_assessment"]["policy_evidence"]
        candidate_data = candidates["result"]["data"]["policies"] if candidates else []
        if any(
            canonical(p["facts"]) not in {canonical(c) for c in candidate_data} for p in applicable
        ):
            raise HandoffRejected(
                "candidate/applicable policy snapshots changed; manual review required"
            )
        return update


class ParallelReviewRun(PersistentReviewRun):
    workflow_version = ParallelStore.workflow_version
    state_version = ParallelStore.state_version
    state_schema = ParallelState
    store_class = ParallelStore
    telemetry_class = BoundedTelemetry
    runtime_class = ParallelRuntime
    report_version = "parallel-run-v1"
    phase = "P07"
    parallel = True
    limit_fields = (
        *LIMIT_FIELDS,
        "max_concurrency",
        "token_budget",
        "model_token_reservation",
        "active_time_budget_seconds",
        "transient_retry_limit",
        "retry_backoff_seconds",
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model_factory", parallel_model)
        super().__init__(*args, **kwargs)
        self.state.update(
            task_results=[],
            evidence_join={"items": [], "conflicts": []},
            policy_candidates=None,
            parallel_plan=[],
        )
        self.checkpoint_state = copy.deepcopy(self.state)

    @classmethod
    async def create(cls, *args, **kwargs):
        run = await super().create(*args, **kwargs)
        try:
            run.trace.event(
                "budget_initialized",
                role="application",
                limits={name: getattr(run.settings, name) for name in run.limit_fields},
            )
            return run
        except BaseException:
            await run.aclose()
            raise

    @classmethod
    async def load(cls, *args, **kwargs):
        run = await super().load(*args, **kwargs)
        run.trace.sync()
        run.persist()
        try:
            run.trace.ledger.check()
        except RunCancelled:
            run.apply_cancellation()
        except CallLimitExceeded:
            pass  # The next advance projects budget exhaustion as a terminal handoff.
        return run

    def after_node(self, state, name):
        self.trace.ledger.check()
        super().after_node(state, name)

    def apply_cancellation(self):
        if self.state["status"] == "cancelled":
            return
        self.state.update(status="cancelled", pending_input=None, reason="CANCEL_REQUESTED")
        self.error = {"code": "CANCEL_REQUESTED", "message": "运行已取消；已提交动作保留。"}
        # Keep actual ticket status and action ledger; invalidate future approval only.
        self.store.invalidate()
        self.trace.ledger.end()
        self.trace.event(
            "run_cancelled",
            role="application",
            receipts=len(self.store.receipts()),
            stop_reason="CANCEL_REQUESTED",
        )
        self.persist(status="cancelled")

    async def advance(self, command):
        try:
            self.trace.ledger.begin()
            report = await super().advance(command)
        except RunCancelled:
            self.apply_cancellation()
            return self.report()
        except CallLimitExceeded as error:
            self.state.update(status="handed_off", pending_input=None, reason=type(error).__name__)
            self.error = {"code": type(error).__name__, "message": "累计预算已用完，转人工处理。"}
            self.store.invalidate()
            self.persist(status="handed_off", terminal_error=True)
            return self.report()
        finally:
            self.trace.ledger.end()
        if self.error and self.error["code"] == "RunCancelled":
            self.apply_cancellation()
        elif report["status"] != "paused":
            self.trace.event(
                "run_stopped",
                role="application",
                status=self.state["status"],
                stop_reason=self.state.get("reason"),
            )
        else:
            self.trace.event("run_paused", role="application", status="paused")
        return self.report()

    async def resume(self, raw):
        if self.state["status"] == "cancelled":
            raise ResumeRejected("run cancelled; already committed receipts remain available")
        try:
            self.trace.ledger.check()
        except RunCancelled:
            self.apply_cancellation()
            return self.report()
        return await super().resume(raw)

    def validate_resume(self, raw, state=None):
        request = super().validate_resume(raw, state)
        if (
            request.decision == "revise"
            and self.trace.ledger.snapshot()["review_repairs"] >= self.settings.review_repair_limit
        ):
            raise ResumeRejected("durable shared review repair budget reached")
        return request

    def report(self, **kwargs):
        self.trace.sync()
        return super().report(**kwargs)


def request_cancel(repository, run_id, settings):
    # Explicit workflow validation; cancellation never loads or waits for the graph/process lock.
    ParallelStore(repository.path, run_id).load(settings.checkpoint_db_path)
    changed = BudgetStore(repository.path, run_id, settings).cancel()
    with read_database(repository.path) as db:
        row = db.execute(
            "SELECT cancel_requested,status FROM run_control JOIN runs "
            "ON runs.id=run_control.run_id WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return {
        "run_id": run_id,
        "cancel_requested": bool(row[0]),
        "new_request": changed,
        "acknowledged": row[1] == "cancelled",
        "run_status": row[1],
    }
