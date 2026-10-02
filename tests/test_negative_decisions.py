import asyncio

import pytest

from after_sales.agents.contracts import ResolutionProposal
from after_sales.agents.runner import run_baseline
from after_sales.agents.validation import validate_proposal
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.tools.contracts import ErrorCode, ToolResult, failed
from after_sales.tools.service import ToolSession
from after_sales.workflows.serial import run_multi

pytestmark = pytest.mark.integration


@pytest.fixture
def session(tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    current = ToolSession.for_ticket(BusinessRepository(path), "T-RETURN-002")
    yield current
    current.close()


async def rejection(
    session, *, omitted_inputs=False, missing_order=False, decision="decline_request"
):
    arguments = {"action": "create_return_request"}
    refs = []
    for name, field in (
        ("get_order", "order_ref"),
        ("get_order_products", "products_ref"),
        ("get_tracking", "tracking_ref"),
        ("get_after_sales_history", "history_ref"),
    ):
        result = await session.call(name, {"order_id": session.context.supplied_order_id})
        arguments[field] = result.evidence_refs[0]
        refs.extend(result.evidence_refs)
    policies = await session.call("search_policies", {"intent": session.context.intent})
    arguments["policy_refs"] = policies.evidence_refs
    refs.extend(policies.evidence_refs)
    assessment = await session.call("evaluate_policy", arguments)
    expected = "ineligible" if decision == "decline_request" else "existing_application"
    assert assessment.data["disposition"] == expected
    refs = [refs[0]] if omitted_inputs else refs
    refs.extend(assessment.evidence_refs)
    return ResolutionProposal(
        ticket_id=session.context.ticket_id,
        order_id=None if missing_order else session.context.supplied_order_id,
        decision=decision,
        evidence_refs=refs,
        customer_reply_draft="当前退货条件不符合演示政策。",
    )


@pytest.mark.parametrize("omitted_inputs,missing_order", [(True, False), (False, True)])
def test_decline_requires_bound_order_and_complete_rule_input_references(
    session, omitted_inputs, missing_order
):
    async def scenario():
        proposal = await rejection(
            session, omitted_inputs=omitted_inputs, missing_order=missing_order
        )
        result = await validate_proposal(proposal, session)
        assert not result.ok
        assert "DECLINE_UNPROVEN" in {issue.code for issue in result.issues}

    asyncio.run(scenario())


def test_decline_rechecks_the_business_request_and_counts_its_tool_call(session):
    async def scenario():
        proposal = await rejection(session)
        calls = []

        async def recheck(arguments):
            calls.append(arguments)
            return await session.call("evaluate_policy", arguments)

        result = await validate_proposal(proposal, session, recheck=recheck)
        assert result.ok
        assert len(calls) == result.rechecked_decisions == 1
        assert result.rechecked_actions == 0
        assert calls[0]["requested_amount_cents"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("disposition", ["eligible", "needs_information", "policy_conflict", None])
def test_decline_is_blocked_when_rule_recheck_no_longer_supports_it(session, disposition):
    async def scenario():
        proposal = await rejection(session)

        async def changed_rule_result(arguments):
            return (
                failed(ErrorCode.QUERY_FAILED, "规则查询失败")
                if disposition is None
                else ToolResult(ok=True, data={"disposition": disposition})
            )

        result = await validate_proposal(proposal, session, recheck=changed_rule_result)
        assert not result.ok
        assert "DECLINE_RULE_RECHECK_FAILED" in {issue.code for issue in result.issues}
        assert result.rechecked_decisions == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("runner", [run_baseline, run_multi], ids=["single", "multi"])
@pytest.mark.parametrize(
    "ticket,decision",
    [("T-RETURN-002", "decline_request"), ("T-UI-RESUME-001", "existing_application")],
)
def test_negative_decision_recheck_cannot_bypass_the_shared_tool_budget(
    session, runner, ticket, decision
):
    repository = session.repository
    successful = asyncio.run(runner(repository, ticket, Settings()))
    assert successful["accepted_proposal"]["decision"] == decision
    assert successful["statistics"]["validation_tool_calls"] == 1
    assert successful["validation"]["rechecked_decisions"] == 1
    limited = asyncio.run(
        runner(
            repository,
            ticket,
            Settings(max_tool_calls=successful["statistics"]["agent_tool_calls"]),
        )
    )
    assert limited["status"] == "failed"
    assert limited["error"]["code"] == "CallLimitExceeded"
    assert limited["accepted_proposal"] is None
    assert limited["statistics"]["validation_tool_calls"] == 0
    assert limited["executed_actions"] == []


@pytest.mark.parametrize(
    "omitted_inputs,missing_order", [(True, False), (False, True), (False, False)]
)
def test_existing_application_requires_bound_order_and_complete_rule_inputs(
    session, omitted_inputs, missing_order
):
    existing = ToolSession.for_ticket(session.repository, "T-UI-RESUME-001")

    async def scenario():
        proposal = await rejection(
            existing,
            omitted_inputs=omitted_inputs,
            missing_order=missing_order,
            decision="existing_application",
        )
        result = await validate_proposal(proposal, existing)
        if omitted_inputs or missing_order:
            assert not result.ok
            assert "EXISTING_APPLICATION_UNPROVEN" in {issue.code for issue in result.issues}
        else:
            assert result.ok and result.rechecked_decisions == 1

    try:
        asyncio.run(scenario())
    finally:
        existing.close()
