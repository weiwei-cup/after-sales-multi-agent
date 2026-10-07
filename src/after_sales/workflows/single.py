"""One investigator Agent, sharing durable guards and actions with the multi-Agent graph."""

import copy
from enum import StrEnum

from after_sales.agents.baseline_script import READS, baseline_steps
from after_sales.agents.contracts import ResolutionProposal
from after_sales.agents.roles import intake_payload, order_handoff, policy_handoff
from after_sales.agents.runner import SYSTEM_PROMPT
from after_sales.agents.scripted import ScriptedChatModel
from after_sales.domain.models import Ticket
from after_sales.tools.contracts import ToolContext
from after_sales.workflows.parallel import ParallelReviewRun, ParallelRuntime, ParallelStore
from after_sales.workflows.reviewed import minimum_review


class SingleRole(StrEnum):
    INVESTIGATOR = "single_agent"


def single_model(role, stage, payload):
    if role != SingleRole.INVESTIGATOR or stage != "draft":
        raise ValueError("single investigator has no delegated model stages")
    ticket = Ticket.model_validate(payload["ticket"])
    context = ToolContext.model_validate(payload["context"])
    return ScriptedChatModel(script_id="single-durable-v1", steps=baseline_steps(ticket, context))


class SingleStore(ParallelStore):
    workflow_version = "single-review-v1"
    state_version = "single-review-state-v1"


class SingleRuntime(ParallelRuntime):
    async def intake(self, state, config):
        return {"intake": intake_payload(state["input"]), "route": "draft"}

    async def draft(self, state, config):
        payload = {
            "ticket": self.owner.ticket.model_dump(mode="json"),
            "context": self.session.context.model_dump(mode="json"),
            "review_feedback": copy.deepcopy(state["review"]),
            "validation_feedback": copy.deepcopy(state["validation"]),
        }
        proposal, actual = await self.invoke_role(
            SingleRole.INVESTIGATOR,
            "draft",
            payload,
            ResolutionProposal,
            (*READS, "get_policy", "evaluate_policy"),
            config,
            system_prompt=SYSTEM_PROMPT,
        )
        # Trusted tool results also feed the shared deterministic reviewer. No second Agent.
        binding = {"ticket_id": state["ticket_id"], "order_id": self.owner.ticket.supplied_order_id}
        order = order_handoff(binding, actual)
        policy = (
            policy_handoff(
                binding,
                {
                    name: value
                    for name, value in actual.items()
                    if name == "search_policies" or name.startswith("evaluate_policy:")
                },
            )
            if order.outcome == "ready"
            else None
        )
        data = proposal.model_dump(mode="json")
        from after_sales.agents.review import action_id

        for action in data["actions"]:
            edit = state["refund_amounts"].get(action_id(self.owner.run_id, action))
            if edit is not None:
                action["amount_cents"] = edit
        return {
            "proposal": data,
            "order_findings": order.model_dump(mode="json"),
            "policy_assessment": policy.model_dump(mode="json") if policy else None,
            "proposal_revision": state["proposal_revision"] + 1,
            "pending_input": None,
            "validation": None,
            "review": None,
            "status": "running",
            "route": "validate",
        }

    async def review(self, state, config):
        result = minimum_review(state)
        route = {
            "research": "repair",
            "revise": "repair",
            "customer_info": "prepare_customer",
            "handoff": "handoff",
            "accept": "prepare_operator" if state["proposal"]["actions"] else "finish",
        }[result.outcome]
        self.trace.event("code_review", role="validator", outcome=result.outcome)
        return {"review": result.model_dump(mode="json"), "route": route}

    async def research(self, state, config):
        # The same investigator gets a fresh, bounded pass, with explicit feedback.
        return {"route": "draft"}


class SingleReviewRun(ParallelReviewRun):
    architecture = "single"
    script_version = "script-v1"
    workflow_version = SingleStore.workflow_version
    state_version = SingleStore.state_version
    store_class = SingleStore
    runtime_class = SingleRuntime
    report_version = "single-review-run-v1"
    phase = "P10"
    parallel = False

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model_factory", single_model)
        super().__init__(*args, **kwargs)
