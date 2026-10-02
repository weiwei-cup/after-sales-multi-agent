"""Finite role scripts consume their own tool messages and explicit handoff DTOs."""

from langchain_core.messages import BaseMessage, ToolMessage

from after_sales.agents.baseline_script import call, outputs, proposal_from_results
from after_sales.agents.contracts import Decision
from after_sales.agents.scripted import ScriptedChatModel, ScriptError, ScriptStep
from after_sales.domain.models import TicketType
from after_sales.tools.contracts import ToolResult
from after_sales.workflows.contracts import (
    ORDER_TOOLS,
    POLICY_TOOLS,
    SOURCE_TO_TOOL,
    EvidenceSnapshot,
    IntakeResult,
    OrderInvestigation,
    PolicyAssessment,
    QueryIssue,
    Role,
)

ORDER_QUESTION = "核验当前工单订单及商品、物流、凭证、售后历史，保留查询失败。"
POLICY_QUESTION = "根据已核验订单证据检索适用政策并计算条件，保留未知条件和冲突。"


def business_outputs(
    messages: list[BaseMessage], tools: tuple[str, ...] = (*ORDER_TOOLS, *POLICY_TOOLS)
) -> dict[str, ToolResult]:
    return outputs([m for m in messages if not isinstance(m, ToolMessage) or m.name in tools])


def schema_feedback(messages, schema: str) -> bool:
    last = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
    return last is not None and last.name == schema


def intake_payload(payload: dict) -> dict:
    intent = TicketType(payload["intent"])
    order_id = payload["supplied_order_id"]
    tasks = []
    if intent != TicketType.UNKNOWN and order_id is not None:
        tasks = [
            {
                "task_id": "order-facts",
                "owner": Role.ORDER.value,
                "depends_on": [],
                "tools": list(ORDER_TOOLS),
                "question": ORDER_QUESTION,
            },
            {
                "task_id": "policy-rules",
                "owner": Role.POLICY.value,
                "depends_on": ["order-facts"],
                "tools": list(POLICY_TOOLS),
                "question": POLICY_QUESTION,
            },
        ]
    return {
        "ticket_id": payload["ticket_id"],
        "intent": intent.value,
        "slots": {
            "order_id": order_id,
            "customer_claims": [
                m["content"] for m in payload["ticket_messages"] if m["role"] == "customer"
            ],
        },
        "missing_information": []
        if order_id is not None
        else [{"field": "order_id", "question": "请提供需要处理的订单号。"}],
        "plan": {"tasks": tasks},
    }


def order_handoff(payload: dict, results: dict[str, ToolResult]) -> OrderInvestigation:
    order = results.get("get_order")
    ready = order is not None and order.ok
    facts, errors = [], []
    for source, name in SOURCE_TO_TOOL.items():
        result = results.get(name)
        if result is None:
            continue
        if result.ok:
            if len(result.evidence_refs) != 1:
                raise ScriptError("order query requires one snapshot reference")
            facts.append(
                EvidenceSnapshot(
                    source_type=source,
                    source_id=payload["order_id"],
                    ref=result.evidence_refs[0],
                    facts=result.data,
                )
            )
        else:
            errors.append(QueryIssue(tool=name, error=result.error))
    return OrderInvestigation(
        ticket_id=payload["ticket_id"],
        order_id=payload["order_id"] if ready else None,
        outcome="ready" if ready else "unavailable",
        facts=tuple(facts),
        tool_errors=tuple(errors),
    )


def policy_handoff(payload: dict, results: dict[str, ToolResult]) -> PolicyAssessment:
    search = results.get("search_policies")
    policies, assessments, errors = [], [], []
    if search is not None and search.ok:
        for policy, ref in zip(search.data["policies"], search.evidence_refs, strict=True):
            policies.append(
                EvidenceSnapshot(
                    source_type="policy",
                    source_id=policy["id"],
                    ref=ref,
                    facts=policy,
                )
            )
    for name, result in results.items():
        if not result.ok:
            errors.append(QueryIssue(tool=name.split(":", 1)[0], error=result.error))
        elif name.startswith("evaluate_policy:"):
            assessments.append(
                EvidenceSnapshot(
                    source_type="assessment",
                    source_id=payload["order_id"],
                    ref=result.evidence_refs[0],
                    facts=result.data,
                )
            )
    return PolicyAssessment(
        ticket_id=payload["ticket_id"],
        order_id=payload["order_id"],
        search_completed=search is not None and search.ok,
        policy_evidence=tuple(policies),
        assessments=tuple(assessments),
        tool_errors=tuple(errors),
    )


def results_from_order(findings: OrderInvestigation | None) -> dict[str, ToolResult]:
    if findings is None:
        return {}
    results = {
        SOURCE_TO_TOOL[f.source_type]: ToolResult(ok=True, data=f.facts, evidence_refs=(f.ref,))
        for f in findings.facts
    }
    results.update({e.tool: ToolResult(ok=False, error=e.error) for e in findings.tool_errors})
    return results


def draft_payload(payload: dict) -> dict:
    intake = IntakeResult.model_validate(payload["intake"])
    if intake.intent == TicketType.UNKNOWN:
        return {
            "ticket_id": intake.ticket_id,
            "decision": Decision.HUMAN_REVIEW.value,
            "customer_reply_draft": "当前诉求尚不能归入支持的售后场景，需要人工核实。",
        }
    order = (
        OrderInvestigation.model_validate(payload["order_findings"])
        if payload["order_findings"]
        else None
    )
    results = results_from_order(order)
    if payload["policy_assessment"]:
        policy = PolicyAssessment.model_validate(payload["policy_assessment"])
        if policy.search_completed:
            results["search_policies"] = ToolResult(
                ok=True,
                data={"policies": [f.facts for f in policy.policy_evidence]},
                evidence_refs=tuple(f.ref for f in policy.policy_evidence),
            )
        for snapshot in policy.assessments:
            results["evaluate_policy:" + snapshot.facts["action"]] = ToolResult(
                ok=True,
                data=snapshot.facts,
                evidence_refs=(snapshot.ref,),
            )
        for error in policy.tool_errors:
            results[error.tool] = ToolResult(ok=False, error=error.error)
    return proposal_from_results(intake.ticket_id, intake.intent, results)


def create_role_model(role: Role, stage: str, payload: dict) -> ScriptedChatModel:
    """Each invocation starts a fresh message history and a fresh finite script."""
    if role == Role.COORDINATOR:
        if stage not in {"intake", "draft"}:
            raise ValueError("unsupported coordinator stage")
        builder = intake_payload if stage == "intake" else draft_payload
        schema = "IntakeResult" if stage == "intake" else "ResolutionProposal"
        return ScriptedChatModel(
            script_id=f"{role}-{stage}-v1",
            steps=(
                ScriptStep(
                    stage, response=lambda _: call(schema, builder(payload), f"{stage}-output")
                ),
                ScriptStep(
                    "repair", (schema,), lambda _: call(schema, builder(payload), f"{stage}-repair")
                ),
            ),
        )
    if role == Role.ORDER:

        def finish(messages: list[BaseMessage], *, repair=None):
            return call(
                "OrderInvestigation",
                order_handoff(payload, business_outputs(messages)).model_dump(mode="json"),
                repair or "order-output",
            )

        steps = []
        for index, name in enumerate(ORDER_TOOLS):

            def read(messages, index=index, name=name):
                if schema_feedback(messages, "OrderInvestigation"):
                    return finish(messages, repair=f"order-repair-{index}")
                results = business_outputs(messages)
                if "get_order" in results and not results["get_order"].ok:
                    return finish(messages)
                return call(name, {"order_id": payload["order_id"]}, f"order-read-{index + 1}")

            steps.append(
                ScriptStep(
                    name,
                    () if index == 0 else (ORDER_TOOLS[index - 1],),
                    read,
                    expected_tool_alternatives=() if index == 0 else (("OrderInvestigation",),),
                )
            )
        steps.extend(
            (
                ScriptStep(
                    "order_output",
                    (ORDER_TOOLS[-1],),
                    lambda m: finish(
                        m,
                        repair="order-repair-output"
                        if schema_feedback(m, "OrderInvestigation")
                        else None,
                    ),
                    expected_tool_alternatives=(("OrderInvestigation",),),
                ),
                ScriptStep(
                    "repair",
                    ("OrderInvestigation",),
                    lambda m: finish(m, repair="order-repair-final"),
                ),
            )
        )
        return ScriptedChatModel(script_id="order-v1", steps=tuple(steps))
    if role != Role.POLICY:
        raise ValueError("unsupported role")
    findings = OrderInvestigation.model_validate(payload["order_findings"])
    order_results = results_from_order(findings)
    slots = 1 if payload["intent"] == TicketType.RETURN.value else 2

    def finish(messages, *, repair=None):
        return call(
            "PolicyAssessment",
            policy_handoff(payload, business_outputs(messages)).model_dump(mode="json"),
            repair or "policy-output",
        )

    steps = [
        ScriptStep(
            "search",
            response=call("search_policies", {"intent": payload["intent"]}, "policy-search"),
        )
    ]
    for index in range(slots):

        def assess(messages, index=index):
            if schema_feedback(messages, "PolicyAssessment"):
                return finish(messages, repair=f"policy-repair-{index}")
            search = business_outputs(messages)["search_policies"]
            if not search.ok:
                return finish(messages)
            actions = sorted({a for p in search.data["policies"] for a in p["allowed_actions"]})
            if len(actions) > slots:
                raise ScriptError("policy role script does not support this action sequence")
            if index >= len(actions):
                return finish(messages)
            action = actions[index]
            arguments = {
                "action": action,
                "order_ref": order_results["get_order"].evidence_refs[0].model_dump(mode="json"),
                "policy_refs": [
                    r.model_dump(mode="json")
                    for p, r in zip(search.data["policies"], search.evidence_refs, strict=True)
                    if action in p["allowed_actions"]
                ],
            }
            for field, name in (
                ("products_ref", "get_order_products"),
                ("tracking_ref", "get_tracking"),
                ("history_ref", "get_after_sales_history"),
            ):
                if name in order_results and order_results[name].ok:
                    arguments[field] = order_results[name].evidence_refs[0].model_dump(mode="json")
            return call("evaluate_policy", arguments, f"policy-assess-{index + 1}")

        steps.append(
            ScriptStep(
                f"assess-{index + 1}",
                ("search_policies" if index == 0 else "evaluate_policy",),
                assess,
                expected_tool_alternatives=(("PolicyAssessment",),),
            )
        )
    steps.extend(
        (
            ScriptStep(
                "policy_output",
                ("evaluate_policy",),
                lambda m: finish(
                    m,
                    repair="policy-repair-output"
                    if schema_feedback(m, "PolicyAssessment")
                    else None,
                ),
                expected_tool_alternatives=(("PolicyAssessment",),),
            ),
            ScriptStep(
                "repair", ("PolicyAssessment",), lambda m: finish(m, repair="policy-repair-final")
            ),
        )
    )
    return ScriptedChatModel(script_id="policy-v1", steps=tuple(steps))
