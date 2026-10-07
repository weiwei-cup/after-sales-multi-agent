"""P06: durable interrupts, persisted budgets and replay-safe mock business effects."""

import copy
import fcntl
import json
import sqlite3
from datetime import UTC, datetime

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from after_sales.agents.contracts import REPORT_ADAPTER, PendingInput, ResumeInput
from after_sales.agents.review import create_review_model
from after_sales.agents.roles import intake_payload, order_handoff, policy_handoff
from after_sales.agents.scripted import ScriptError
from after_sales.agents.telemetry import CallLimitExceeded, RunTelemetry, SchemaRepairExceeded
from after_sales.domain.models import Ticket
from after_sales.repositories.actions import ActionService, ApprovalStale, operation_payload
from after_sales.repositories.run_store import (
    STATE_VERSION,
    WORKFLOW_VERSION,
    DurableInputConflict,
    IncompatibleRun,
    RunStore,
)
from after_sales.repositories.sqlite import migrate, transaction
from after_sales.tools.contracts import ToolContext, ToolResult
from after_sales.tools.evidence import EvidenceStore, canonical
from after_sales.tools.inspection import inspect_ticket
from after_sales.tools.service import ToolSession
from after_sales.workflows.reviewed import (
    InMemoryReviewRun,
    ResumeRejected,
    ReviewRuntime,
    ReviewState,
    TicketOverlayRepository,
    build_review_graph,
)
from after_sales.workflows.serial import HandoffRejected

CHECKPOINT_APPLICATION_ID = 0x41534350
LIMIT_FIELDS = (
    "max_model_calls",
    "max_tool_calls",
    "review_repair_limit",
    "proposal_repair_limit",
    "model_timeout_seconds",
    "tool_timeout_seconds",
    "tool_max_result_bytes",
)
TRACE_FIELDS = (
    "events",
    "messages",
    "usage",
    "model_calls",
    "tool_calls",
    "validation_tool_calls",
    "schema_repairs",
    "model_elapsed_ms",
    "tool_elapsed_ms",
)


def checkpoint_file(path, *, create):
    path = path.resolve()
    if not create and not path.is_file():
        raise IncompatibleRun("checkpoint database absent; stored business records preserved")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path if create else path.as_uri() + "?mode=ro", uri=not create)
    try:
        if create:
            connection.execute("BEGIN IMMEDIATE")
        appid = connection.execute("PRAGMA application_id").fetchone()[0]
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables and appid == 0 and create:
            connection.execute(f"PRAGMA application_id={CHECKPOINT_APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.execute("CREATE TABLE workflow_manifest (schema_version TEXT NOT NULL)")
            connection.execute("INSERT INTO workflow_manifest VALUES (?)", (STATE_VERSION,))
        else:
            if (
                appid != CHECKPOINT_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != 1
                or tables - {"workflow_manifest", "checkpoints", "writes"}
            ):
                raise IncompatibleRun("unsupported checkpoint owner/schema; data preserved")
            if "workflow_manifest" not in tables or connection.execute(
                "SELECT schema_version FROM workflow_manifest"
            ).fetchall() != [(STATE_VERSION,)]:
                raise IncompatibleRun("checkpoint manifest incompatible; data preserved")
        if create:
            connection.commit()
    finally:
        connection.close()


class PersistentState(ReviewState):
    executed_actions: list[dict]


class DurableTelemetry(RunTelemetry):
    def __init__(self, owner, settings):
        super().__init__(settings)
        self.owner = owner
        self.elapsed_offset = 0.0

    def persist(self):
        if self.owner.registered:
            self.owner.persist()

    def reserve_model(self, role="single_agent"):
        super().reserve_model(role)
        self.persist()

    def reserve_tool(self, role):
        super().reserve_tool(role)
        self.persist()

    def event(self, kind, **kwargs):
        super().event(kind, **kwargs)
        self.events[-1]["elapsed_ms"] += self.elapsed_offset
        self.persist()

    def statistics(self):
        result = super().statistics()
        result["elapsed_ms"] += self.elapsed_offset
        return result


class PersistentRuntime(ReviewRuntime):
    async def approve(self, state, config):
        confirmation = next(
            c
            for c in reversed(self.owner.confirmations)
            if c.decision == "approve"
            and c.proposal_revision == state["proposal_revision"]
            and c.input_revision == state["input_revision"]
        )
        pending = self.owner.pending_history[confirmation.pending_id]
        receipts = []
        try:
            for binding in pending.actions:
                payload = operation_payload(
                    pending.ticket_id, pending.input_revision, binding.candidate
                )
                receipt = self.owner.actions.lookup(payload)
                if receipt is None:
                    if hasattr(self.trace, "action"):
                        receipt = await self.trace.action(
                            lambda binding=binding: self.owner.actions.execute(
                                self.owner.run_id,
                                pending.pending_id,
                                binding,
                                clock=self.owner.clock,
                            )
                        )
                    else:
                        self.trace.reserve_tool("executor")
                        self.trace.event(
                            "action_started", role="executor", action_id=binding.action_id
                        )
                        receipt = self.owner.actions.execute(
                            self.owner.run_id, pending.pending_id, binding, clock=self.owner.clock
                        )
                    self.trace.event(
                        "action_committed", role="executor", receipt=receipt.model_dump(mode="json")
                    )
                else:
                    self.trace.event(
                        "action_replayed", role="executor", receipt=receipt.model_dump(mode="json")
                    )
                receipts.append(receipt.model_dump(mode="json"))
        except ApprovalStale as error:
            if receipts:
                raise HandoffRejected(
                    "a partially committed multi-action plan needs manual review"
                ) from error
            self.owner.store.invalidate()
            self.trace.event("approval_invalidated", role="executor", reason=str(error))
            return {"route": "refresh", "status": "running", "reason": "APPROVAL_STALE"}
        return {"executed_actions": receipts, "status": "completed", "route": "end", "reason": None}

    async def refresh(self, state, config):
        current = self.owner.repository.get_ticket(self.owner.ticket.id)
        if (current.customer_id, current.type) != (
            self.owner.ticket.customer_id,
            self.owner.ticket.type,
        ):
            raise HandoffRejected("ticket identity changed; manual review required")
        self.owner.ticket = current
        evidence = self.session.evidence
        self.session.close()
        self.owner.as_of_time = self.owner.clock()
        self.session = self.owner.new_session(evidence)
        source_session = self.session
        trace = self.trace

        class TracedSession:
            context, evidence = source_session.context, source_session.evidence

            async def call(self, name, arguments):
                return await trace.tool(
                    name,
                    f"refresh-{trace.tool_calls + 1}",
                    lambda: source_session.call(name, arguments),
                    role="executor",
                )

        inspection = await inspect_ticket(TracedSession())
        actual = {name: ToolResult.model_validate(r) for name, r in inspection["results"].items()}
        payload = {"ticket_id": current.id, "order_id": current.supplied_order_id}
        order = order_handoff(payload, actual)
        policy = policy_handoff(payload, actual) if order.outcome == "ready" else None
        return {
            "input": self.owner.ticket_input(),
            "input_revision": current.input_revision,
            "intake": intake_payload(self.owner.ticket_input()),
            "order_findings": order.model_dump(mode="json"),
            "policy_assessment": policy.model_dump(mode="json") if policy else None,
            "refund_amounts": {},
            "proposal": None,
            "validation": None,
            "review": None,
            "pending_input": None,
            "route": "draft",
            "status": "running",
            "reason": None,
        }


class PersistentReviewRun(InMemoryReviewRun):
    workflow_version = WORKFLOW_VERSION
    state_version = STATE_VERSION
    state_schema = PersistentState
    store_class = RunStore
    telemetry_class = DurableTelemetry
    runtime_class = PersistentRuntime
    limit_fields = LIMIT_FIELDS
    report_version = "persistent-run-v1"
    phase = "P06"

    def __init__(self, repository, ticket_id, settings, *, clock=None, fault=None, **kwargs):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.as_of_time = self.clock()
        self.registered = False
        self.fault = fault or (lambda stage: None)
        super().__init__(repository, ticket_id, settings, **kwargs)
        self.runtime.session.close()
        self.trace = self.telemetry_class(self, settings)
        self.runtime = self.runtime_class(
            self, self.new_session(), self.trace, kwargs.get("model_factory", create_review_model)
        )
        self.store = self.store_class(repository.path, self.run_id)
        self.actions = ActionService(repository.path, fault=self.fault)
        self.state.update(schema_version=self.state_version, executed_actions=[])
        self.checkpoint_state = copy.deepcopy(self.state)
        self.saver_context = self.process_lock = self.ticket_lock = None
        self.replaying = False

    def new_session(self, evidence_store=None):
        repository = TicketOverlayRepository(self.repository, self.ticket)
        context = ToolContext(
            ticket_id=self.ticket.id,
            ticket_version=self.ticket.version,
            customer_id=self.ticket.customer_id,
            supplied_order_id=self.ticket.supplied_order_id,
            intent=self.ticket.type,
            session_id=self.run_id,
            as_of_time=self.as_of_time,
            dataset_version=self.repository.metadata()["version"],
        )
        return ToolSession(
            repository,
            context,
            evidence_store=evidence_store,
            timeout_seconds=self.settings.tool_timeout_seconds,
            max_result_bytes=self.settings.tool_max_result_bytes,
        )

    def payload(self):
        return {
            "ticket": self.ticket.model_dump(mode="json"),
            "context": self.runtime.session.context.model_dump(mode="json"),
            "trace": {name: getattr(self.trace, name) for name in TRACE_FIELDS},
            "agent_runs": self.runtime.agent_runs,
            "elapsed_ms": self.trace.statistics()["elapsed_ms"],
            "error": self.error,
            "operator_id": self.operator_id,
            "evidence": self.runtime.session.evidence.export(self.runtime.session.context),
        }

    def persist(self, **kwargs):
        self.store.save_runtime(self.payload(), **kwargs)

    def acquire(self):
        directory = self.settings.checkpoint_db_path.resolve().parent / "run-locks"
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / f"run-{self.run_id}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ResumeRejected("this run is already owned by another process") from None
        self.process_lock = handle
        handle = (directory / f"ticket-{self.ticket.id}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ResumeRejected("this ticket is already owned by another process") from None
        self.ticket_lock = handle

    async def open_saver(self):
        self.saver_context = AsyncSqliteSaver.from_conn_string(
            str(self.settings.checkpoint_db_path.resolve())
        )
        self.checkpointer = await self.saver_context.__aenter__()
        await self.checkpointer.setup()
        self.graph = build_review_graph(
            self.runtime,
            self.checkpointer,
            state_schema=self.state_schema,
            persistent=True,
            parallel=getattr(self, "parallel", False),
        )

    @classmethod
    async def create(cls, repository, ticket_id, settings, **kwargs):
        if kwargs.get("mode", settings.model_mode) != "scripted":
            raise ValueError("P06 durable workflow currently supports scripted only")
        migrate(repository.path)
        run = cls(repository, ticket_id, settings, **kwargs)
        try:
            run.acquire()
            checkpoint_file(settings.checkpoint_db_path, create=True)
            await run.open_saver()
            if (await run.graph.aget_state(run.config)).values:
                raise DurableInputConflict("thread already has checkpoints; choose a new run ID")
            run.store.register(
                run.ticket,
                run.runtime.session.context,
                settings.checkpoint_db_path,
                {name: getattr(settings, name) for name in run.limit_fields},
                run.payload(),
            )
            run.registered = True
            return run
        except BaseException:
            await run.aclose()
            raise

    @classmethod
    async def load(cls, repository, run_id, settings, **kwargs):
        stored = cls.store_class(repository.path, run_id).load(settings.checkpoint_db_path)
        # HTTP admission persists an initial queued runtime before any checkpoint exists.
        checkpoint_file(
            settings.checkpoint_db_path,
            create=stored["status"] == "queued" and stored["http_admitted"],
        )
        settings = settings.model_copy(update=stored["limits"])
        payload = stored["runtime"]
        run = cls(
            repository,
            stored["ticket_id"],
            settings,
            run_id=run_id,
            operator_id=payload["operator_id"],
            **kwargs,
        )
        try:
            run.acquire()
            run.runtime.session.close()
            run.ticket = Ticket.model_validate(payload["ticket"])
            run.as_of_time = ToolContext.model_validate(payload["context"]).as_of_time
            evidence = EvidenceStore()
            run.runtime.session = run.new_session(evidence)
            evidence.restore_trusted(payload["evidence"], run.runtime.session.context)
            for name in TRACE_FIELDS:
                setattr(run.trace, name, payload["trace"][name])
            run.trace.elapsed_offset = payload["elapsed_ms"]
            run.runtime.agent_runs = payload["agent_runs"]
            run.error = payload["error"]
            for row in run.store.history():
                pending = PendingInput.model_validate_json(row["payload_json"])
                run.pending_history[pending.pending_id] = pending
                if row["status"] != "open":
                    run.consumed.add(pending.pending_id)
            run.confirmations = run.store.confirmations()
            await run.open_saver()
            snapshot = await run.graph.aget_state(run.config)
            run.started = run.registered = True
            if snapshot.values and snapshot.values.get("schema_version") != run.state_version:
                raise IncompatibleRun("stored graph state version unsupported")
            run.checkpoint_state = copy.deepcopy(snapshot.values)
            run.state = copy.deepcopy(snapshot.values) if snapshot.values else run.state
            if stored["status"] == "cancelled" and hasattr(run.trace, "ledger"):
                run.state.update(status="cancelled", pending_input=None, reason="CANCEL_REQUESTED")
            elif stored["terminal_error"]:
                run.state.update(status="handed_off", pending_input=None, reason=run.error["code"])
            elif snapshot.next:
                pending = run.state.get("pending_input")
                is_wait = snapshot.next in (("wait_customer",), ("wait_operator",))
                if not is_wait or not pending or run.store.reply(pending["pending_id"]):
                    run.state.update(status="interrupted", pending_input=None)
            elif not snapshot.values:
                run.state.update(status="interrupted", pending_input=None)
            if run.state["status"] in {"running", "queued"}:
                run.state.update(status="interrupted", pending_input=None)
            run.persist(status=run.state["status"])
            return run
        except BaseException:
            await run.aclose()
            raise

    def after_node(self, state, name):
        self.state = copy.deepcopy(state)
        if state.get("proposal") and state.get("route") != "refresh":
            self.store.save_plan(state)
        if name in {"finish", "handoff"}:
            with transaction(self.repository.path) as connection:
                status = "handed_off" if name == "handoff" else "resolved"
                if name == "finish" and state["proposal"]["decision"] == "existing_application":
                    active = {
                        r[0]
                        for r in connection.execute(
                            "SELECT type FROM after_sales_history "
                            "WHERE order_id=? AND status='active'",
                            (state["proposal"]["order_id"],),
                        )
                    }
                    status = (
                        "waiting_return"
                        if "create_return_request" in active
                        else ("processing" if "open_logistics_case" in active else "resolved")
                    )
                connection.execute(
                    "UPDATE tickets SET status=?,version=version+1 WHERE id=? AND status<>?",
                    (status, self.ticket.id, status),
                )
        self.persist(status=state["status"])
        self.fault(f"after_node:{name}")

    def prepare_pending(self, state, kind):
        result = super().prepare_pending(state, kind)
        self.store.save_pending(PendingInput.model_validate(result["pending_input"]))
        return result

    def validate_resume(self, raw, state=None):
        request = ResumeInput.model_validate(raw)
        consumed = request.pending_id in self.consumed
        if consumed and self.replaying:
            old = self.store.reply(request.pending_id)
            if old and canonical(request.model_dump(mode="json")) == old["envelope_json"]:
                self.consumed.remove(request.pending_id)
                try:
                    return super().validate_resume(request, state)
                finally:
                    self.consumed.add(request.pending_id)
        return super().validate_resume(request, state)

    def consume(self, request):
        self.consumed.add(request.pending_id)
        if not any(
            e["kind"] == "pending_consumed" and e["pending_id"] == request.pending_id
            for e in self.trace.events
        ):
            self.trace.event("pending_consumed", role="application", pending_id=request.pending_id)

    def record_confirmation(self, confirmation):
        self.confirmations = self.store.confirmations()

    def apply_customer(self, request):
        stored = self.store.reply(request.pending_id)
        self.ticket = Ticket.model_validate_json(stored["ticket_json"])
        previous = self.runtime.session
        self.runtime.session = self.new_session(previous.evidence)
        previous.close()
        return self.ticket_input()

    async def advance(self, command):
        try:
            async for _ in self.graph.astream(command, self.config, stream_mode="updates"):
                pass
            snapshot = await self.graph.aget_state(self.config)
            self.checkpoint_state = copy.deepcopy(snapshot.values)
            self.state = copy.deepcopy(snapshot.values)
        except Exception as error:
            snapshot = await self.graph.aget_state(self.config)
            self.checkpoint_state = copy.deepcopy(snapshot.values)
            if snapshot.values:
                self.state = copy.deepcopy(snapshot.values)
            terminal = isinstance(
                error,
                (
                    CallLimitExceeded,
                    SchemaRepairExceeded,
                    ScriptError,
                    HandoffRejected,
                    DurableInputConflict,
                    GraphRecursionError,
                    TimeoutError,
                ),
            )
            self.state.update(
                status="handed_off" if terminal else "interrupted",
                pending_input=None,
                reason=type(error).__name__,
            )
            self.error = {
                "code": type(error).__name__,
                "message": "运行停止；请检查记录后恢复或人工处理。",
            }
            if terminal:
                self.store.invalidate()
                if not self.store.receipts():
                    with transaction(self.repository.path) as connection:
                        connection.execute(
                            "UPDATE tickets SET status='handed_off',version=version+1 "
                            "WHERE id=? AND status<>'handed_off'",
                            (self.ticket.id,),
                        )
            self.persist(terminal_error=terminal)
        self.persist(status=self.state["status"])
        return self.report()

    async def resume(self, raw):
        async with self.lock:
            request = ResumeInput.model_validate(raw)
            if self.state["status"] == "completed" and request.decision == "approve":
                old = self.store.reply(request.pending_id)
                if old and old["envelope_json"] == canonical(request.model_dump(mode="json")):
                    return self.report()
            request = self.validate_resume(raw)
            self.store.accept(request, self.ticket, self.clock())
            self.confirmations = self.store.confirmations()
            self.fault("after_input_commit")
            self.replaying = True
            try:
                return await self.advance(Command(resume=request.model_dump(mode="json")))
            finally:
                self.replaying = False

    async def recover(self):
        async with self.lock:
            if self.state["status"] != "interrupted":
                return self.report()
            snapshot = await self.graph.aget_state(self.config)
            pending = snapshot.values.get("pending_input") if snapshot.values else None
            reply = self.store.reply(pending["pending_id"]) if pending else None
            command = Command(resume=json.loads(reply["envelope_json"])) if reply else None
            if not snapshot.values or (not snapshot.next and not snapshot.values.get("node_trace")):
                command = {**self.state, "status": "running"}
            self.error = None
            self.replaying = True
            try:
                return await self.advance(command)
            finally:
                self.replaying = False

    def report(self, **kwargs):
        report = super().report(raw=True)
        receipts = self.store.receipts()
        # Include receipts from a repeated run whose operation already belongs to an earlier run.
        for row in self.state.get("executed_actions", []):
            if not any(r["operation_key"] == row["operation_key"] for r in receipts):
                receipts.append(row)
        report.update(
            schema_version=self.report_version,
            phase=self.phase,
            workflow_version=self.workflow_version,
            candidate_only=not receipts,
            executed_actions=receipts,
            resume_scope="cross_process",
            checkpointer="AsyncSqliteSaver",
            checkpoint_schema=self.state_version,
            checkpoint_state=copy.deepcopy(self.checkpoint_state),
            business_status=self.repository.get_ticket(self.ticket.id).status.value,
        )
        return REPORT_ADAPTER.validate_python(report).model_dump(mode="json")

    async def aclose(self):
        self.runtime.session.close()
        if self.saver_context:
            await self.saver_context.__aexit__(None, None, None)
            self.saver_context = None
        if self.process_lock:
            self.process_lock.close()
            self.process_lock = None
        if self.ticket_lock:
            self.ticket_lock.close()
            self.ticket_lock = None
        self.closed = True


async def run_durable(repository, ticket_id, settings, **kwargs):
    run = await PersistentReviewRun.create(repository, ticket_id, settings, **kwargs)
    try:
        return await run.start()
    finally:
        await run.aclose()
