import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from after_sales.agents.baseline_script import call
from after_sales.agents.scripted import ScriptedChatModel, ScriptError, ScriptStep

pytestmark = pytest.mark.unit


@tool
def lookup(order_id: str) -> str:
    """Read one demo order."""
    return order_id


def test_scripted_model_binds_tools_and_consumes_matching_tool_messages():
    first = call("lookup", {"order_id": "ORD-005"}, "call-one")
    model = ScriptedChatModel(
        steps=(
            ScriptStep("query", response=first),
            ScriptStep("answer", ("lookup",), AIMessage(content="资料已读取。")),
        )
    )
    bound = model.bind_tools([lookup])
    messages = [HumanMessage(content="查询订单")]
    response = bound.invoke(messages)
    assert response.tool_calls == first.tool_calls
    messages.extend(
        [response, ToolMessage(content="真实结果", name="lookup", tool_call_id="call-one")]
    )
    assert asyncio.run(bound.ainvoke(messages)).content == "资料已读取。"
    observed = model.observed_messages
    assert observed[-1][-1].content == "真实结果"
    observed[-1][-1].content = "修改副本"
    assert model.observed_messages[-1][-1].content == "真实结果"
    with pytest.raises(ScriptError, match="exhausted"):
        bound.invoke(messages)


@pytest.mark.parametrize("bad_id", ["forged-id", ""])
def test_wrong_returned_call_id_fails_explicitly(bad_id):
    model = ScriptedChatModel(steps=(ScriptStep("answer", ("lookup",), AIMessage(content="done")),))
    with pytest.raises(ScriptError, match="IDs"):
        model.bind_tools([lookup]).invoke(
            [
                HumanMessage(content="query"),
                call("lookup", {}, "expected-id"),
                ToolMessage(content="result", name="lookup", tool_call_id=bad_id),
            ]
        )


def test_unexpected_tool_sequence_cannot_fall_back_to_plausible_answer():
    model = ScriptedChatModel(steps=(ScriptStep("answer", ("lookup",), AIMessage(content="done")),))
    with pytest.raises(ScriptError, match="sequence"):
        model.invoke([HumanMessage(content="no tool response")])


def test_unbound_tool_call_fails_before_execution():
    model = ScriptedChatModel(
        steps=(ScriptStep("query", response=call("hidden_tool", {}, "call-1")),)
    )
    with pytest.raises(ScriptError, match="not bound"):
        model.bind_tools([lookup]).invoke([HumanMessage(content="query")])


def test_scripted_exception_is_not_swallowed_by_model():
    model = ScriptedChatModel(steps=(ScriptStep("failure", error=RuntimeError("injected")),))
    with pytest.raises(RuntimeError, match="injected"):
        model.invoke([HumanMessage(content="query")])


def test_step_without_response_fails_explicitly():
    model = ScriptedChatModel(steps=(ScriptStep("empty"),))
    with pytest.raises(ScriptError, match="no response"):
        model.invoke([HumanMessage(content="query")])


def test_tool_ids_cannot_be_reused_between_model_calls():
    model = ScriptedChatModel(
        steps=(
            ScriptStep("one", response=call("lookup", {}, "same-id")),
            ScriptStep("two", ("lookup",), call("lookup", {}, "same-id")),
        )
    )
    bound = model.bind_tools([lookup])
    messages = [HumanMessage(content="query")]
    first = bound.invoke(messages)
    with pytest.raises(ScriptError, match="reused"):
        bound.invoke(
            [*messages, first, ToolMessage(content="result", name="lookup", tool_call_id="same-id")]
        )
