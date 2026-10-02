from typing import TypedDict

import pytest
from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

pytestmark = pytest.mark.integration


class CounterState(TypedDict):
    count: int


def make_graph(checkpointer=None):
    graph = StateGraph(CounterState)
    graph.add_node("increment", lambda state: {"count": state["count"] + 1})
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)
    return graph.compile(checkpointer=checkpointer)


def test_framework_interfaces_and_graph_execution():
    assert callable(create_agent)
    assert make_graph().invoke({"count": 1}) == {"count": 2}


def test_sqlite_checkpoint_survives_new_connection(tmp_path):
    database = str(tmp_path / "checkpoints.sqlite")
    config = {"configurable": {"thread_id": "p00-smoke"}}
    with SqliteSaver.from_conn_string(database) as saver:
        assert make_graph(saver).invoke({"count": 3}, config) == {"count": 4}
    with SqliteSaver.from_conn_string(database) as saver:
        snapshot = make_graph(saver).get_state(config)
        assert snapshot.values == {"count": 4}
        assert snapshot.next == ()
