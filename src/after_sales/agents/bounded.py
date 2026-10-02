"""P07 shared slots, finite transient retries, durable attempt and event accounting."""

import asyncio
import json
from time import perf_counter

from langchain_core.messages import AIMessage

from after_sales.agents.contracts import ActionReceipt
from after_sales.agents.scripted import ScriptError
from after_sales.agents.telemetry import RunTelemetry, SchemaRepairExceeded
from after_sales.repositories.budgets import BudgetStore
from after_sales.tools.contracts import ErrorCode, ToolResult
from after_sales.workflows.contracts import Role


def error_category(error):
    if isinstance(error, (TimeoutError, ConnectionError)) or getattr(
        error, "status_code", None
    ) in (429, 502, 503, 504):
        return "transient"
    if (
        isinstance(error, (ScriptError, SchemaRepairExceeded))
        or type(error).__name__ == "HandoffRejected"
    ):
        return "model_contract"
    if getattr(error, "status_code", None) in (400, 401, 403, 404):
        return "business"
    return "program"


class BoundedTelemetry(RunTelemetry):
    def __init__(self, owner, settings):
        super().__init__(settings)
        self.owner = owner
        self.elapsed_offset = 0.0
        self.slots = asyncio.Semaphore(settings.max_concurrency)
        self.in_flight = self.peak_in_flight = 0
        self.ledger = BudgetStore(owner.repository.path, owner.run_id, settings)

    def sync(self):
        if not self.owner.registered:
            return
        data = self.ledger.snapshot()
        self.events = data["events"]
        self.schema_repairs = data["schema_repairs"]
        self.model_calls = sum(c["kind"] == "model" for c in data["calls"])
        self.tool_calls = sum(c["kind"] == "tool" for c in data["calls"])
        self.validation_tool_calls = sum(
            c["kind"] == "tool" and c["role"] == "validator" for c in data["calls"]
        )
        self.model_elapsed_ms = sum(c["duration_ms"] for c in data["calls"] if c["kind"] == "model")
        self.tool_elapsed_ms = sum(c["duration_ms"] for c in data["calls"] if c["kind"] == "tool")
        self.usage = [json.loads(c["usage_json"]) for c in data["calls"] if c["usage_json"]]

    def event(self, kind, *, role="single_agent", **data):
        if not self.owner.registered:
            return super().event(kind, role=role, **data)
        context = {
            "role": role,
            "workflow_version": self.owner.workflow_version,
            "state_version": self.owner.state_version,
            "run_id": self.owner.run_id,
            "ticket_id": self.owner.ticket.id,
            "input_revision": self.owner.ticket.input_revision,
            "proposal_revision": self.owner.state.get("proposal_revision", 0),
        }
        entry = self.ledger.event(kind, **{**context, **data})
        previous = self.events[-1]["sequence"] if self.events else 0
        if entry["sequence"] == previous + 1:
            self.events.append(entry)
        else:
            self.sync()  # Include events committed by a separate cancellation process.
        if kind in {
            "model_finished",
            "tool_finished",
            "agent_finished",
            "branch_finished",
            "run_started",
            "run_stopped",
            "run_paused",
            "run_cancelled",
            "budget_initialized",
            "action_committed",
        }:
            self.owner.persist()

    def reserve(self, kind, role):
        sequence = self.ledger.reserve(kind, role)
        self.sync()
        self.event("call_reserved", role=role, attempt_id=sequence, call_kind=kind)
        self.owner.fault("after_call_reservation")
        return sequence

    def reserve_tool(self, role):
        # P06's synchronous, transactionally idempotent executor also consumes a tool slot.
        return self.reserve("tool", role)

    def repair_schema(self, error, *, role="single_agent"):
        self.event(
            "schema_invalid", role=role, error_code=type(error).__name__, category="model_contract"
        )
        self.ledger.repair_schema()
        self.sync()
        self.event("schema_repair", role=role, attempt=self.schema_repairs)
        return "结构化结果不符合当前输出 schema。请修复字段与业务约束，再提交一次。"

    async def attempt(self, kind, role, operation, *, name=None, call_id=None):
        # BEGIN IMMEDIATE arbitrates reservations across processes before any await.
        sequence = self.reserve(kind, role)
        started = perf_counter()
        try:
            async with self.slots:
                self.ledger.check()
                self.in_flight += 1
                self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
                self.event(
                    f"{kind}_started",
                    role=role,
                    attempt_id=sequence,
                    tool=name,
                    call_id=call_id,
                    queue_ms=round((perf_counter() - started) * 1000, 3),
                )
                try:
                    timeout = (
                        self.settings.model_timeout_seconds
                        if kind == "model"
                        else self.settings.tool_timeout_seconds
                    )
                    result = await asyncio.wait_for(
                        operation(), min(timeout, self.ledger.remaining_seconds())
                    )
                finally:
                    self.in_flight -= 1
            usage = None
            if kind == "model":
                reports = []
                for message in result.result:
                    self.messages.append({**message.model_dump(mode="json"), "agent_role": role})
                    if isinstance(message, AIMessage) and message.usage_metadata:
                        reports.append(dict(message.usage_metadata))
                # Missing metadata on any AI message leaves the whole call unknown.
                ai = [m for m in result.result if isinstance(m, AIMessage)]
                if ai and len(reports) == len(ai):
                    usage = {
                        k: sum(r.get(k, 0) for r in reports)
                        for k in ("input_tokens", "output_tokens", "total_tokens")
                    }
            duration = round((perf_counter() - started) * 1000, 3)
            self.ledger.settle(sequence, "succeeded", duration, usage)
            data = {}
            if kind == "tool":
                parsed = (
                    result
                    if isinstance(result, ToolResult)
                    else ToolResult.model_validate_json(result.content)
                )
                data = {
                    "result": parsed.model_dump(mode="json"),
                    "category": None
                    if parsed.ok
                    else "transient"
                    if parsed.error.retryable
                    and parsed.error.code in (ErrorCode.TOOL_BUSY, ErrorCode.TOOL_TIMEOUT)
                    else "business",
                }
            self.event(
                f"{kind}_finished",
                role=role,
                attempt_id=sequence,
                tool=name,
                call_id=call_id,
                duration_ms=duration,
                token_state="reported" if usage else "unknown",
                **data,
            )
            return result
        except BaseException as error:
            duration = round((perf_counter() - started) * 1000, 3)
            # The attempt may already be settled if event serialization fails; do not refund it.
            calls = self.ledger.snapshot()["calls"]
            if next(c for c in calls if c["sequence"] == sequence)["status"] == "reserved":
                self.ledger.settle(sequence, "failed", duration)
            self.event(
                f"{kind}_failed",
                role=role,
                attempt_id=sequence,
                tool=name,
                call_id=call_id,
                duration_ms=duration,
                error_code=type(error).__name__,
                category=error_category(error),
            )
            raise

    async def bounded(self, kind, role, operation, **kwargs):
        for index in range(self.settings.transient_retry_limit + 1):
            try:
                result = await self.attempt(kind, role, operation, **kwargs)
            except Exception as error:
                if (
                    error_category(error) != "transient"
                    or index == self.settings.transient_retry_limit
                ):
                    raise
            else:
                if kind != "tool":
                    return result
                parsed = (
                    result
                    if isinstance(result, ToolResult)
                    else ToolResult.model_validate_json(result.content)
                )
                if (
                    parsed.ok
                    or not parsed.error.retryable
                    or parsed.error.code not in (ErrorCode.TOOL_BUSY, ErrorCode.TOOL_TIMEOUT)
                    or index == self.settings.transient_retry_limit
                ):
                    return result
            delay = self.settings.retry_backoff_seconds * (2**index)
            self.event(
                "retry_scheduled",
                role=role,
                call_kind=kind,
                retry=index + 1,
                delay_seconds=delay,
                category="transient",
            )
            await asyncio.sleep(delay)
            self.ledger.check()
        raise AssertionError("finite retry loop exhausted")

    async def model(self, operation, *, role):
        return await self.bounded("model", role, operation)

    async def tool(self, name, call_id, operation, *, role):
        return await self.bounded("tool", role, operation, name=name, call_id=call_id)

    async def action(self, operation):
        async def invoke():
            receipt = operation()
            return ToolResult(ok=True, data=receipt.model_dump(mode="json"))

        result = await self.attempt("tool", "executor", invoke, name="execute_action")
        return ActionReceipt.model_validate(result.data)

    def role_counts(self, role, after_sequence):
        events = [
            e
            for e in self.events
            if e["sequence"] > after_sequence and e["role"] == role and e["kind"] == "call_reserved"
        ]
        return sum(e["call_kind"] == "model" for e in events), sum(
            e["call_kind"] == "tool" for e in events
        )

    def role_statistics(self):
        calls = self.ledger.snapshot()["calls"]
        return {
            role.value: {
                "model_calls": sum(c["kind"] == "model" and c["role"] == role.value for c in calls),
                "tool_calls": sum(c["kind"] == "tool" and c["role"] == role.value for c in calls),
            }
            for role in Role
        }

    def statistics(self):
        self.sync()
        result = super().statistics()
        if self.owner.registered:
            data = self.ledger.snapshot()
            models = [c for c in data["calls"] if c["kind"] == "model"]
            unknown = sum(c["actual_tokens"] is None for c in models)
            active = set()
            peak = 0
            for event in data["events"]:
                if event["kind"] in ("model_started", "tool_started"):
                    active.add(event["attempt_id"])
                    peak = max(peak, len(active))
                elif event["kind"] in (
                    "model_finished",
                    "tool_finished",
                    "model_failed",
                    "tool_failed",
                ):
                    active.discard(event["attempt_id"])
            result.update(
                elapsed_ms=data["active_ms"],
                active_elapsed_ms=data["active_ms"],
                token_usage_note="partially_reported"
                if unknown and self.usage
                else "not_reported"
                if unknown
                else "provider_reported",
                token_usage_state="unknown" if unknown else "reported",
                actual_total_tokens=None if unknown else sum(c["actual_tokens"] for c in models),
                known_total_tokens=sum(c["actual_tokens"] or 0 for c in models),
                unknown_usage_calls=unknown,
                review_repairs_reserved=data["review_repairs"],
                token_budget_charged=sum(
                    c["actual_tokens"] if c["actual_tokens"] is not None else c["token_hold"]
                    for c in models
                ),
                monetary_cost=None,
                price_state="unknown",
                max_concurrency=self.settings.max_concurrency,
                peak_in_flight=max(self.peak_in_flight, peak),
                reservations=[
                    {k: v for k, v in c.items() if k != "usage_json"} for c in data["calls"]
                ],
                stop_reason=self.owner.state.get("reason"),
            )
        return result
