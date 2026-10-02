"""Minimal per-run events and hard call limits, including application rule checks."""

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from after_sales.config import Settings
from after_sales.tools.contracts import ToolResult


class CallLimitExceeded(Exception):
    pass


class SchemaRepairExceeded(Exception):
    pass


class RunTelemetry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.events: list[dict[str, object]] = []
        self.model_calls = self.tool_calls = self.validation_tool_calls = self.schema_repairs = 0
        self.model_elapsed_ms = self.tool_elapsed_ms = 0.0
        self.usage: list[dict[str, int]] = []
        self.messages: list[dict[str, object]] = []
        self.started = perf_counter()

    def event(self, kind: str, *, role: str = "single_agent", **data):
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "kind": kind,
                "role": role,
                "elapsed_ms": round((perf_counter() - self.started) * 1000, 3),
                **data,
            }
        )

    def reserve_model(self, role: str = "single_agent"):
        if self.model_calls >= self.settings.max_model_calls:
            self.event("call_limit_reached", role=role, limit="model_calls")
            raise CallLimitExceeded("model call limit reached")
        self.model_calls += 1

    def reserve_tool(self, role: str):
        if self.tool_calls >= self.settings.max_tool_calls:
            self.event("call_limit_reached", role=role, limit="tool_calls")
            raise CallLimitExceeded("tool call limit reached")
        self.tool_calls += 1
        if role == "validator":
            self.validation_tool_calls += 1

    def repair_schema(self, error: Exception, *, role: str = "single_agent") -> str:
        self.event("schema_invalid", role=role, error_code=type(error).__name__)
        if self.schema_repairs >= self.settings.proposal_repair_limit:
            raise SchemaRepairExceeded("proposal schema repair limit reached") from error
        self.schema_repairs += 1
        self.event("schema_repair", role=role, attempt=self.schema_repairs)
        return "结构化结果不符合当前输出 schema。请修复字段与业务约束，再提交一次。"

    async def tool(self, name: str, call_id: str, operation: Callable[[], Awaitable], *, role: str):
        self.reserve_tool(role)
        self.event("tool_started", role=role, tool=name, call_id=call_id)
        started = perf_counter()
        try:
            result = await operation()
            parsed = (
                result
                if isinstance(result, ToolResult)
                else ToolResult.model_validate_json(result.content)
            )
            duration = round((perf_counter() - started) * 1000, 3)
            self.event(
                "tool_finished",
                role=role,
                tool=name,
                call_id=call_id,
                duration_ms=duration,
                result=parsed.model_dump(mode="json"),
            )
            return result
        except Exception as error:
            self.event(
                "tool_failed",
                role=role,
                tool=name,
                call_id=call_id,
                error_code=type(error).__name__,
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
            raise
        finally:
            self.tool_elapsed_ms += (perf_counter() - started) * 1000

    def statistics(self) -> dict[str, object]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "agent_tool_calls": self.tool_calls - self.validation_tool_calls,
            "validation_tool_calls": self.validation_tool_calls,
            "schema_repairs": self.schema_repairs,
            "model_elapsed_ms": round(self.model_elapsed_ms, 3),
            "tool_elapsed_ms": round(self.tool_elapsed_ms, 3),
            "elapsed_ms": round((perf_counter() - self.started) * 1000, 3),
            "token_usage": self.usage or None,
            "token_usage_note": "provider_reported" if self.usage else "not_reported",
        }


class TraceMiddleware(AgentMiddleware):
    def __init__(self, telemetry: RunTelemetry, *, role: str = "single_agent"):
        self.telemetry = telemetry
        self.role = role

    async def awrap_model_call(self, request, handler):
        trace = self.telemetry
        trace.reserve_model(self.role)
        trace.event("model_started", role=self.role, call=trace.model_calls)
        started = perf_counter()
        try:
            response = await asyncio.wait_for(
                handler(request), trace.settings.model_timeout_seconds
            )
            duration = round((perf_counter() - started) * 1000, 3)
            for message in response.result:
                entry = message.model_dump(mode="json")
                if self.role != "single_agent":
                    entry["agent_role"] = self.role
                trace.messages.append(entry)
                if isinstance(message, AIMessage) and message.usage_metadata:
                    trace.usage.append(dict(message.usage_metadata))
            trace.event(
                "model_finished",
                role=self.role,
                call=trace.model_calls,
                duration_ms=duration,
                tool_calls=[
                    call
                    for message in response.result
                    if isinstance(message, AIMessage)
                    for call in message.tool_calls
                ],
            )
            return response
        except Exception as error:
            trace.event(
                "model_failed",
                role=self.role,
                call=trace.model_calls,
                error_code=type(error).__name__,
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
            raise
        finally:
            trace.model_elapsed_ms += (perf_counter() - started) * 1000

    async def awrap_tool_call(self, request, handler):
        result = await self.telemetry.tool(
            request.tool_call["name"],
            request.tool_call["id"],
            lambda: handler(request),
            role=self.role,
        )
        if isinstance(result, ToolMessage):
            entry = result.model_dump(mode="json")
            if self.role != "single_agent":
                entry["agent_role"] = self.role
            self.telemetry.messages.append(entry)
        return result
