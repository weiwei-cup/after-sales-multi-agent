"""Validate factual claims and recheck action candidates before accepting a proposal."""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from after_sales.agents.contracts import (
    Decision,
    ProposalValidation,
    ResolutionProposal,
    ValidationIssue,
)
from after_sales.tools.contracts import EvidenceRef, ToolFailure, ToolResult
from after_sales.tools.evidence import canonical
from after_sales.tools.service import ToolSession


async def validate_proposal(
    proposal: ResolutionProposal,
    session: ToolSession,
    *,
    recheck: Callable[[dict], Awaitable[ToolResult]] | None = None,
) -> ProposalValidation:
    issues = []
    checked = 0

    def issue(code, message):
        issues.append(ValidationIssue(code=code, message=message))

    if proposal.ticket_id != session.context.ticket_id:
        issue("TICKET_MISMATCH", "建议不属于当前工单")
    if proposal.order_id is not None and proposal.order_id != session.context.supplied_order_id:
        issue("ORDER_MISMATCH", "建议不属于当前工单订单")
    top_refs = {(ref.evidence_id, ref.source_version) for ref in proposal.evidence_refs}
    cited = {}
    for ref in proposal.evidence_refs:
        try:
            cited[ref.evidence_id] = session.evidence.resolve(ref, session.context)
        except ToolFailure as error:
            issue(error.code.value, error.message)
    if len(top_refs) != len(proposal.evidence_refs):
        issue("DUPLICATE_REFERENCE", "建议引用不能重复")
    if proposal.order_id is not None:
        orders = [
            json.loads(item.facts_json) for item in cited.values() if item.source_type == "order"
        ]
        if not any(
            order["id"] == proposal.order_id and order["customer_id"] == session.context.customer_id
            for order in orders
        ):
            issue("ORDER_EVIDENCE_REQUIRED", "订单关联需要当前客户的订单证据")

    for claim in proposal.claims:
        ref = claim.evidence_ref
        try:
            if (ref.evidence_id, ref.source_version) not in top_refs:
                issue("UNCITED_CLAIM", "事实声明未列入建议证据")
            item = session.evidence.resolve(ref, session.context)
            facts = json.loads(item.facts_json)
            if item.source_id != proposal.order_id:
                issue("CLAIM_ORDER_MISMATCH", "声明证据不属于建议订单")
                continue
            if claim.fact in {
                "order_status",
                "received_at",
                "expected_delivery_at",
                "refund_remaining_cents",
            }:
                if item.source_type != "order":
                    issue("CLAIM_SOURCE_MISMATCH", "订单声明需要订单证据")
                    continue
                expected = (
                    facts["paid_cents"] - facts["refunded_cents"]
                    if claim.fact == "refund_remaining_cents"
                    else facts["status" if claim.fact == "order_status" else claim.fact]
                )
            elif claim.fact == "proof_status":
                if item.source_type != "proof":
                    issue("CLAIM_SOURCE_MISMATCH", "凭证声明需要凭证证据")
                    continue
                expected = facts["proof_status"]
            else:
                if item.source_type != "tracking":
                    issue("CLAIM_SOURCE_MISMATCH", "丢件声明需要物流证据")
                    continue
                expected = any(
                    event["event_type"] == "investigation_confirmed_lost"
                    and event["source"] == "mock_carrier_investigation"
                    and datetime.fromisoformat(event["occurred_at"]) <= session.context.as_of_time
                    for event in facts["events"]
                )
            if canonical(claim.value) != canonical(expected):
                issue("UNSUPPORTED_CLAIM", "声明与引用中的可信事实不符")
        except ToolFailure as error:
            issue(error.code.value, error.message)

    seen_actions = set()
    for action in proposal.actions:
        key = (action.type, action.order_id)
        if key in seen_actions:
            issue("DUPLICATE_ACTION", "同一建议不能重复提出同一动作")
        seen_actions.add(key)
        if (
            action.order_id != proposal.order_id
            or action.order_id != session.context.supplied_order_id
        ):
            issue("ACTION_ORDER_MISMATCH", "动作候选不属于建议订单")
            continue
        action_refs = [action.assessment_ref, *action.evidence_refs]
        if any((ref.evidence_id, ref.source_version) not in top_refs for ref in action_refs):
            issue("UNCITED_ACTION", "动作引用未完整列入建议证据")
        try:
            for ref in action_refs:
                session.evidence.resolve(ref, session.context)
            snapshot = session.evidence.resolve(
                action.assessment_ref, session.context, "assessment"
            )
            assessment = json.loads(snapshot.facts_json)
            if snapshot.source_id != action.order_id or assessment["action"] != action.type.value:
                issue("ASSESSMENT_MISMATCH", "规则证据与动作候选不符")
                continue
            if not assessment["eligible"]:
                issue("ACTION_INELIGIBLE", "已有代码规则不支持该动作候选")
            inputs = {}
            policy_refs = []
            expected_policies = set()
            for raw_ref in assessment["input_evidence_refs"]:
                ref = EvidenceRef.model_validate(raw_ref)
                evidence = session.evidence.resolve(ref, session.context)
                if evidence.source_type == "policy":
                    policy_refs.append(raw_ref)
                    expected_policies.add((evidence.source_id, int(evidence.source_version)))
                else:
                    field = {
                        "order": "order_ref",
                        "products": "products_ref",
                        "tracking": "tracking_ref",
                        "history": "history_ref",
                    }.get(evidence.source_type)
                    if field:
                        inputs[field] = raw_ref
            supplied = {(ref.policy_id, ref.version) for ref in action.policy_refs}
            if supplied != expected_policies:
                issue("POLICY_REFERENCE_MISMATCH", "动作政策引用必须完整且版本一致")
            if any(
                (ref["evidence_id"], ref["source_version"]) not in top_refs
                for ref in assessment["input_evidence_refs"]
            ):
                issue("UNCITED_RULE_INPUT", "动作所依据的规则输入未完整列入建议证据")
            arguments = {
                **inputs,
                "policy_refs": policy_refs,
                "action": action.type.value,
                "requested_amount_cents": action.amount_cents,
            }
            result = (
                await recheck(arguments)
                if recheck is not None
                else await session.call("evaluate_policy", arguments)
            )
            checked += 1
            if not result.ok or not result.data["eligible"]:
                issue("ACTION_RULE_RECHECK_FAILED", "动作候选未通过再次计算的代码规则")
        except ToolFailure as error:
            issue(error.code.value, error.message)

    assessments = [
        json.loads(item.facts_json) for item in cited.values() if item.source_type == "assessment"
    ]
    if proposal.decision == Decision.EXISTING and not any(
        data["disposition"] == "existing_application" for data in assessments
    ):
        issue("EXISTING_APPLICATION_UNPROVEN", "没有规则证据支持已有申请")
    if proposal.decision == Decision.DECLINE and not any(
        data["disposition"] == "ineligible" for data in assessments
    ):
        issue("DECLINE_UNPROVEN", "未知资料或缺少规则证据不能自动拒绝")
    return ProposalValidation(ok=not issues, issues=tuple(issues), rechecked_actions=checked)
