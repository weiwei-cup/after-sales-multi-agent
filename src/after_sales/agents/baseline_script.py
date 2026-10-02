"""A finite demo script that builds answers only from actual ToolMessages, never gold labels."""

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from after_sales.agents.contracts import Decision
from after_sales.agents.scripted import ScriptError, ScriptStep
from after_sales.domain.models import ActionType, Ticket, TicketType
from after_sales.tools.contracts import ToolContext, ToolResult

READS = (
    "get_order",
    "get_order_products",
    "get_tracking",
    "get_delivery_proof",
    "get_after_sales_history",
    "search_policies",
)
QUESTIONS = {
    "order_id": "请提供需要处理的订单号。",
    "received_at": "缺少可核验的收货时间，请补充收货资料。",
    "product_state": "请补充商品是否拆封及商品资料。",
    "logistics_evidence": "需要补充可核验的物流调查结果。",
    "after_sales_history": "售后历史尚未查明，需要补充查询。",
    "policy_evidence": "政策资料尚未查明，需要复核适用版本。",
    "tool_service": "资料查询未完成，需要恢复查询后继续处理。",
}


def outputs(messages: list[BaseMessage]) -> dict[str, ToolResult]:
    calls = {
        call["id"]: call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    results = {}
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name == "ResolutionProposal":
            continue
        if message.tool_call_id not in calls:
            raise ScriptError("tool result has no matching model call")
        result = ToolResult.model_validate_json(message.content)
        call = calls[message.tool_call_id]
        key = message.name
        if key == "evaluate_policy":
            key += ":" + call["args"]["action"]
        results[key] = result
    return results


def call(name: str, arguments: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id}])


def proposal_payload(ticket: Ticket, context: ToolContext, messages: list[BaseMessage]) -> dict:
    return proposal_from_results(ticket.id, ticket.type, outputs(messages))


def proposal_from_results(
    ticket_id: str, intent: TicketType, results: dict[str, ToolResult]
) -> dict:
    """Share business drafting across architectures without sharing their internal messages."""
    refs = {
        ref.evidence_id: ref.model_dump(mode="json")
        for result in results.values()
        if result.ok
        for ref in result.evidence_refs
    }
    order_result = results.get("get_order")
    order = order_result.data if order_result is not None and order_result.ok else None
    proposal = {
        "ticket_id": ticket_id,
        "order_id": order["id"] if order else None,
        "decision": Decision.HUMAN_REVIEW.value,
        "claims": [],
        "evidence_refs": list(refs.values()),
        "actions": [],
        "unresolved_questions": [],
        "customer_reply_draft": "现有资料需要人工复核。",
        "candidate_only": True,
    }

    def information(fields):
        proposal["decision"] = Decision.REQUEST_INFORMATION.value
        proposal["unresolved_questions"] = [
            {"field": field, "question": QUESTIONS[field]} for field in sorted(set(fields))
        ]
        proposal["customer_reply_draft"] = "为继续处理，需要补充或核验以下资料：" + "；".join(
            item["question"] for item in proposal["unresolved_questions"]
        )
        return proposal

    if order is None:
        if order_result is not None and order_result.error.code == "NOT_OWNED":
            proposal["customer_reply_draft"] = "该订单无法由当前客户访问，需要人工核对订单归属。"
            return proposal
        return information(
            [
                "order_id"
                if order_result is None
                or order_result.error.code in {"NOT_FOUND", "MISSING_REFERENCE"}
                else "tool_service"
            ]
        )
    order_ref = order_result.evidence_refs[0].model_dump(mode="json")
    proposal["claims"] = [
        {"fact": "order_status", "value": order["status"], "evidence_ref": order_ref},
        {
            "fact": "refund_remaining_cents",
            "value": order["paid_cents"] - order["refunded_cents"],
            "evidence_ref": order_ref,
        },
    ]
    proof = results.get("get_delivery_proof")
    if proof is not None and proof.ok:
        proposal["claims"].append(
            {
                "fact": "proof_status",
                "value": proof.data["proof_status"],
                "evidence_ref": proof.evidence_refs[0].model_dump(mode="json"),
            }
        )
    assessments = {
        key.split(":", 1)[1]: result
        for key, result in results.items()
        if key.startswith("evaluate_policy:") and result.ok
    }
    if any(result.data["disposition"] == "policy_conflict" for result in assessments.values()):
        proposal["customer_reply_draft"] = (
            "同时适用的演示政策存在冲突，需要人工复核后再确定处理方式。"
        )
        return proposal
    if any(result.data["disposition"] == "existing_application" for result in assessments.values()):
        proposal["decision"] = Decision.EXISTING.value
        proposal["customer_reply_draft"] = "已查询到有效售后申请，请继续跟进已有记录。"
        return proposal
    gaps = []
    for result in assessments.values():
        if result.data["disposition"] == "needs_information":
            for evaluation in result.data["evaluations"]:
                for condition in evaluation["conditions"]:
                    if condition["value"] == "unknown":
                        gaps.append(
                            {
                                "return_window": "received_at",
                                "product_category": "product_state",
                                "unopened": "product_state",
                                "product_scope": "product_state",
                                "confirmed_lost": "logistics_evidence",
                                "no_existing_application": "after_sales_history",
                                "overdue": "logistics_evidence",
                            }.get(condition["name"], "policy_evidence")
                        )
            if result.data["missing_policy_refs"]:
                gaps.append("policy_evidence")
    if any(not result.ok for result in results.values()):
        gaps.append("tool_service")
    if gaps:
        return information(gaps)
    for action, decision, reply in (
        (
            ActionType.MOCK_REFUND,
            Decision.REFUND,
            "资料满足演示退款条件，可提交退款候选，待操作员确认。",
        ),
        (
            ActionType.RETURN_REQUEST,
            Decision.RETURN,
            "资料满足演示退货条件，可提交退货申请候选，待操作员确认。",
        ),
        (
            ActionType.LOGISTICS_CASE,
            Decision.INVESTIGATE,
            "可提交物流调查候选，待操作员确认；当前不能凭延迟或缺凭证确定丢件。",
        ),
    ):
        assessment = assessments.get(action.value)
        if assessment is None or not assessment.data["eligible"]:
            continue
        data = assessment.data
        proposal["decision"] = decision.value
        proposal["customer_reply_draft"] = reply
        proposal["actions"] = [
            {
                "type": action.value,
                "order_id": order["id"],
                "amount_cents": data["evaluations"][0]["requested_amount_cents"]
                if action == ActionType.MOCK_REFUND
                else None,
                "assessment_ref": assessment.evidence_refs[0].model_dump(mode="json"),
                "policy_refs": [
                    {"policy_id": evaluation["policy_id"], "version": evaluation["policy_version"]}
                    for evaluation in data["evaluations"]
                ],
                "evidence_refs": list(refs.values()),
                "requires_operator_confirmation": True,
            }
        ]
        return proposal
    if intent == TicketType.RETURN and assessments:
        proposal["decision"] = Decision.DECLINE.value
        proposal["customer_reply_draft"] = (
            "根据已核验资料，当前退货条件不符合演示政策。请查看具体条件与依据。"
        )
    elif intent == TicketType.DELAY and assessments:
        proposal["decision"] = Decision.INFORM.value
        proposal["customer_reply_draft"] = (
            "当前资料不支持创建新的物流调查或退款候选，可继续关注物流进度。"
        )
    return proposal


def baseline_steps(ticket: Ticket, context: ToolContext) -> tuple[ScriptStep, ...]:
    def finish(messages, *, repair=False):
        return call(
            "ResolutionProposal",
            proposal_payload(ticket, context, messages),
            "script-proposal-repair" if repair else "script-proposal",
        )

    if context.supplied_order_id is None:
        return (
            ScriptStep("missing_order", response=finish),
            ScriptStep(
                "repair", ("ResolutionProposal",), lambda messages: finish(messages, repair=True)
            ),
        )
    steps = []
    for index, name in enumerate(READS):

        def read(messages, name=name, index=index):
            results = outputs(messages)
            if "get_order" in results and not results["get_order"].ok:
                return finish(messages)
            arguments = (
                {"intent": ticket.type.value}
                if name == "search_policies"
                else {"order_id": context.supplied_order_id}
            )
            return call(name, arguments, f"script-read-{index + 1}")

        steps.append(ScriptStep(name, () if index == 0 else (READS[index - 1],), read))

    slots = 1 if ticket.type == TicketType.RETURN else 2
    for index in range(slots):

        def assess(messages, index=index):
            results = outputs(messages)
            policies = results["search_policies"]
            if not policies.ok:
                return finish(messages)
            actions = sorted(
                {
                    action
                    for policy in policies.data["policies"]
                    for action in policy["allowed_actions"]
                }
            )
            if index >= len(actions):
                return finish(messages)
            if len(actions) > slots:
                raise ScriptError("baseline script does not support this action sequence")
            action = actions[index]
            arguments = {
                "action": action,
                "order_ref": results["get_order"].evidence_refs[0].model_dump(mode="json"),
                "policy_refs": [
                    ref.model_dump(mode="json")
                    for policy, ref in zip(
                        policies.data["policies"], policies.evidence_refs, strict=True
                    )
                    if action in policy["allowed_actions"]
                ],
            }
            for field, name in (
                ("products_ref", "get_order_products"),
                ("tracking_ref", "get_tracking"),
                ("history_ref", "get_after_sales_history"),
            ):
                if results[name].ok:
                    arguments[field] = results[name].evidence_refs[0].model_dump(mode="json")
            return call("evaluate_policy", arguments, f"script-assess-{index + 1}")

        steps.append(
            ScriptStep(
                f"assess-{index + 1}",
                ("search_policies" if index == 0 else "evaluate_policy",),
                assess,
            )
        )
    steps.extend(
        [
            ScriptStep("proposal", ("evaluate_policy",), finish),
            ScriptStep(
                "repair", ("ResolutionProposal",), lambda messages: finish(messages, repair=True)
            ),
        ]
    )
    return tuple(steps)
