"""Model selection is explicit. User chose to defer live provider integration in P03."""

from after_sales.agents.baseline_script import baseline_steps
from after_sales.agents.scripted import ScriptedChatModel, ScriptStep
from after_sales.domain.models import Ticket
from after_sales.tools.contracts import ToolContext


class LiveModelDeferred(Exception):
    pass


def create_model(
    mode: str, ticket: Ticket, context: ToolContext, *, steps: tuple[ScriptStep, ...] | None = None
):
    if mode == "live":
        raise LiveModelDeferred("live provider integration is deferred by user choice")
    if mode != "scripted":
        raise ValueError("unsupported model mode")
    return ScriptedChatModel(steps=baseline_steps(ticket, context) if steps is None else steps)
