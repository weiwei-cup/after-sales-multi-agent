"""Finite offline review and targeted research scripts, using actual tool messages."""

import hashlib

from after_sales.agents.baseline_script import call
from after_sales.agents.contracts import ResolutionProposal, ReviewResult
from after_sales.agents.roles import (
    business_outputs,
    create_role_model,
    draft_payload,
    order_handoff,
    results_from_order,
    schema_feedback,
)
from after_sales.agents.scripted import ScriptedChatModel, ScriptStep
from after_sales.tools.evidence import canonical
from after_sales.workflows.contracts import OrderInvestigation, Role

REVIEW_TOOLS = ("validate_evidence_refs", "get_evidence")


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def action_id(run_id: str, action: dict) -> str:
    return "action-" + digest([run_id, action["type"], action["order_id"]])[:32]


def safe_draft(payload: dict) -> dict:
    proposal = ResolutionProposal.model_validate(draft_payload(payload)).model_dump(mode="json")
    for action in proposal["actions"]:
        edit = payload.get("refund_amounts", {}).get(action_id(payload["run_id"], action))
        if edit is not None:
            action["amount_cents"] = edit
    return proposal


def merge_research(payload, actual):
    previous = OrderInvestigation.model_validate(payload["order_findings"])
    merged = {**results_from_order(previous), **actual}
    if "get_order" in actual and not actual["get_order"].ok:
        merged = {"get_order": actual["get_order"]}
    return order_handoff(payload, merged)


def create_review_model(role: Role, stage: str, payload: dict) -> ScriptedChatModel:
    if role == Role.COORDINATOR and stage == "draft":
        return ScriptedChatModel(
            script_id="review-coordinator-v1",
            steps=(
                ScriptStep(
                    "draft",
                    response=lambda _: call(
                        "ResolutionProposal", safe_draft(payload), "draft-output"
                    ),
                ),
                ScriptStep(
                    "repair",
                    ("ResolutionProposal",),
                    lambda _: call("ResolutionProposal", safe_draft(payload), "draft-repair"),
                ),
            ),
        )
    if role == Role.ORDER and stage == "research":
        tools = tuple(payload["tools"])

        def finish(messages):
            return call(
                "OrderInvestigation",
                merge_research(payload, business_outputs(messages, tools)).model_dump(mode="json"),
                "research-output",
            )

        steps = []
        for index, name in enumerate(tools):

            def read(messages, index=index, name=name):
                actual = business_outputs(messages, tools)
                if schema_feedback(messages, "OrderInvestigation") or (
                    "get_order" in actual and not actual["get_order"].ok
                ):
                    return finish(messages)
                return call(name, {"order_id": payload["order_id"]}, f"research-read-{index}")

            steps.append(
                ScriptStep(
                    name,
                    () if index == 0 else (tools[index - 1],),
                    read,
                    expected_tool_alternatives=() if index == 0 else (("OrderInvestigation",),),
                )
            )
        steps.extend(
            (
                ScriptStep(
                    "output",
                    (tools[-1],),
                    finish,
                    expected_tool_alternatives=(("OrderInvestigation",),),
                ),
                ScriptStep("repair", ("OrderInvestigation",), finish),
            )
        )
        return ScriptedChatModel(script_id="order-research-v1", steps=tuple(steps))
    if role != Role.REVIEW:
        return create_role_model(role, stage, payload)
    expected = ReviewResult.model_validate(payload["minimum_review"])

    def finish(messages):
        actual = business_outputs(messages, REVIEW_TOOLS)
        result = expected
        if any(not item.ok for item in actual.values()):
            result = ReviewResult(
                outcome="handoff",
                issues=({"code": "REVIEW_EVIDENCE_INVALID", "message": "审核引用核验失败。"},),
            )
        return call("ReviewResult", result.model_dump(mode="json"), "review-output")

    refs = payload["proposal"]["evidence_refs"]
    steps = []
    if refs:
        steps.append(
            ScriptStep(
                "references", response=call("validate_evidence_refs", {"refs": refs}, "review-refs")
            )
        )
    steps.extend(
        (
            ScriptStep("review", ("validate_evidence_refs",) if refs else (), finish),
            ScriptStep("repair", ("ReviewResult",), finish),
        )
    )
    return ScriptedChatModel(script_id="reviewer-v1", steps=tuple(steps))
