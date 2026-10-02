"""A finite chat model that exercises LangChain's real tool loop without provider calls."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field, PrivateAttr


class ScriptError(Exception):
    """An unexpected call must fail rather than quietly synthesizing a fallback answer."""


@dataclass(frozen=True)
class ScriptStep:
    name: str
    expected_tools: tuple[str, ...] = ()
    response: AIMessage | Callable[[list[BaseMessage]], AIMessage] | None = None
    error: Exception | None = None


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    steps: tuple[ScriptStep, ...] = Field(exclude=True)
    script_id: str = "baseline-v1"
    cache: bool = False
    _cursor: int = PrivateAttr(default=0)
    _observed: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _issued_call_ids: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "after-sales-scripted"

    @property
    def _identifying_params(self) -> dict[str, str]:
        return {"script_id": self.script_id, "script_version": "script-v1"}

    @property
    def observed_messages(self) -> list[list[BaseMessage]]:
        return [[message.model_copy(deep=True) for message in batch] for batch in self._observed]

    def bind_tools(self, tools: Sequence, *, tool_choice=None, **kwargs):
        names = tuple(convert_to_openai_tool(tool)["function"]["name"] for tool in tools)
        return self.bind(bound_tool_names=names, tool_choice=tool_choice, **kwargs)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        if self._cursor >= len(self.steps):
            raise ScriptError("script exhausted; no response was specified")
        step = self.steps[self._cursor]
        previous = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)),
            None,
        )
        returned = messages[previous + 1 :] if previous is not None else []
        tools = [message for message in returned if isinstance(message, ToolMessage)]
        if tuple(message.name for message in tools) != step.expected_tools:
            raise ScriptError(f"unexpected tool response sequence for step {step.name}")
        if previous is not None:
            calls = messages[previous].tool_calls
            if {call["id"] for call in calls} != {message.tool_call_id for message in tools}:
                raise ScriptError("tool call IDs do not match returned messages")
        self._observed.append([message.model_copy(deep=True) for message in messages])
        self._cursor += 1
        if step.error is not None:
            raise step.error
        response = step.response(messages) if callable(step.response) else step.response
        if response is None:
            raise ScriptError("script step has no response")
        names = kwargs.get("bound_tool_names", ())
        if any(call["name"] not in names for call in response.tool_calls):
            raise ScriptError("script requested a tool that was not bound")
        ids = [call["id"] for call in response.tool_calls]
        if len(ids) != len(set(ids)):
            raise ScriptError("script emitted duplicate tool call IDs")
        if self._issued_call_ids.intersection(ids):
            raise ScriptError("script reused an earlier tool call ID")
        self._issued_call_ids.update(ids)
        return ChatResult(generations=[ChatGeneration(message=response.model_copy(deep=True))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
