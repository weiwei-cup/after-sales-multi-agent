import asyncio
import copy
import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from after_sales.agents.baseline_script import baseline_steps
from after_sales.agents.runner import run_baseline, save_run
from after_sales.agents.scripted import ScriptedChatModel, ScriptStep
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect
from after_sales.tools.service import ToolSession

pytestmark = pytest.mark.integration


@pytest.fixture
def business(tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    return BusinessRepository(path)


def model_for(repository, ticket_id, *, transform=None, repair_transform=None):
    session = ToolSession.for_ticket(repository, ticket_id)
    try:
        steps = list(baseline_steps(repository.get_ticket(ticket_id), session.context))
    finally:
        session.close()
    for index, step in enumerate(steps):
        change = (
            repair_transform
            if step.name == "repair"
            else transform
            if step.name == "proposal"
            else None
        )
        if change is not None:
            original = step.response

            def mutate(messages, original=original, change=change):
                response = original(messages)
                payload = copy.deepcopy(response.tool_calls[0]["args"])
                change(payload)
                return response.model_copy(
                    update={"tool_calls": [{**response.tool_calls[0], "args": payload}]}
                )

            steps[index] = replace(step, response=mutate)
    return ScriptedChatModel(steps=tuple(steps))


def run(repository, ticket="T-RETURN-001", **kwargs):
    return asyncio.run(
        run_baseline(repository, ticket, kwargs.pop("settings", Settings()), **kwargs)
    )


@pytest.mark.parametrize(
    ("ticket", "decision"),
    [
        ("T-DELAY-001", "inform_progress"),
        ("T-DELAY-002", "propose_logistics_investigation"),
        ("T-NOTRECEIVED-001", "propose_logistics_investigation"),
        ("T-NOTRECEIVED-002", "propose_refund"),
        ("T-RETURN-001", "propose_return"),
        ("T-RETURN-002", "decline_request"),
        ("T-MISSING-001", "request_information"),
        ("T-CROSS-001", "human_review"),
        ("T-CONFLICT-001", "human_review"),
        ("T-INVALID-EVIDENCE-001", "propose_refund"),
        ("T-REVIEW-LOOP-001", "propose_return"),
        ("T-DUPLICATE-001", "propose_return"),
        ("T-TOOL-TIMEOUT-001", "propose_logistics_investigation"),
        ("T-PROOF-ERROR-001", "propose_logistics_investigation"),
        ("T-STALE-APPROVAL-001", "propose_return"),
        ("T-CRASH-001", "propose_refund"),
        ("T-BUDGET-001", "propose_return"),
        ("T-PROMPT-INJECTION-001", "propose_logistics_investigation"),
        ("T-API-DUPLICATE-001", "propose_return"),
        ("T-UI-RESUME-001", "existing_application"),
    ],
)
def test_existing_ticket_facts_pass_the_real_agent_loop_and_code_validation(
    business, ticket, decision
):
    with connect(business.path, readonly=True) as connection:
        before = "\n".join(connection.iterdump())
    report = run(business, ticket)
    assert report["status"] == "completed", report["error"] or report["validation"]
    assert report["accepted_proposal"]["decision"] == decision
    assert report["validation"]["ok"] is True
    assert report["validation"]["executable"] is False
    assert report["executed_actions"] == []
    assert report["statistics"]["model_calls"] <= 20
    assert report["statistics"]["tool_calls"] <= 30
    assert report["statistics"]["token_usage"] is None
    assert [event["sequence"] for event in report["events"]] == list(
        range(1, len(report["events"]) + 1)
    )
    with connect(business.path, readonly=True) as connection:
        assert "\n".join(connection.iterdump()) == before


def test_model_receives_actual_tool_results_and_matching_call_ids(business):
    model = model_for(business, "T-RETURN-001")
    report = run(business, model=model)
    assert report["status"] == "completed"
    observed = model.observed_messages
    assert len(observed) == report["statistics"]["model_calls"]
    order_message = next(
        message
        for batch in observed
        for message in batch
        if isinstance(message, ToolMessage) and message.name == "get_order"
    )
    assert json.loads(order_message.content)["data"]["paid_cents"] == 10000
    emitted = {
        call["id"]
        for message in report["messages"]
        if message["type"] == "ai"
        for call in message["tool_calls"]
    }
    returned = {
        message["tool_call_id"] for message in report["messages"] if message["type"] == "tool"
    }
    assert emitted == returned


def test_script_uses_changed_repository_facts_rather_than_case_labels(business):
    with connect(business.path) as connection:
        connection.execute("UPDATE orders SET paid_cents=8700,version=2 WHERE id='ORD-004'")
    report = run(business, "T-NOTRECEIVED-002")
    assert report["accepted_proposal"]["actions"][0]["amount_cents"] == 8700
    assert (
        next(
            claim["value"]
            for claim in report["accepted_proposal"]["claims"]
            if claim["fact"] == "refund_remaining_cents"
        )
        == 8700
    )


def test_missing_order_does_not_guess_or_invoke_business_tools(business):
    report = run(business, "T-MISSING-001")
    assert report["statistics"]["agent_tool_calls"] == 0
    assert report["accepted_proposal"]["unresolved_questions"][0]["field"] == "order_id"


def test_schema_error_gets_one_real_feedback_and_repair(business):
    def invalid(payload):
        payload["decision"] = "invented-decision"

    model = model_for(business, "T-RETURN-001", transform=invalid)
    report = run(business, model=model)
    assert report["status"] == "completed"
    assert report["statistics"]["schema_repairs"] == 1
    feedback = [
        message
        for batch in model.observed_messages
        for message in batch
        if isinstance(message, ToolMessage) and message.name == "ResolutionProposal"
    ]
    assert feedback and "不符合" in feedback[-1].content


def test_persistent_bad_schema_stops_without_unbounded_retry(business):
    def invalid(payload):
        payload["decision"] = "invented-decision"

    model = model_for(business, "T-RETURN-001", transform=invalid, repair_transform=invalid)
    report = run(business, model=model)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "SchemaRepairExceeded"
    assert report["statistics"]["schema_repairs"] == 1
    assert report["accepted_proposal"] is None
    assert report["executed_actions"] == []


def test_schema_repair_can_be_disabled(business):
    def invalid(payload):
        payload["customer_reply_draft"] = ""

    report = run(
        business,
        model=model_for(business, "T-RETURN-001", transform=invalid),
        settings=Settings(proposal_repair_limit=0),
    )
    assert report["error"]["code"] == "SchemaRepairExceeded"
    assert report["statistics"]["schema_repairs"] == 0


def test_over_balance_refund_is_blocked_by_code_after_valid_schema(business):
    def excessive(payload):
        payload["actions"][0]["amount_cents"] = 10001

    report = run(
        business,
        "T-NOTRECEIVED-002",
        model=model_for(business, "T-NOTRECEIVED-002", transform=excessive),
    )
    assert report["status"] == "validation_failed"
    assert report["proposal"]["actions"][0]["amount_cents"] == 10001
    assert report["accepted_proposal"] is None
    assert "ACTION_RULE_RECHECK_FAILED" in {
        issue["code"] for issue in report["validation"]["issues"]
    }
    assert report["executed_actions"] == []


@pytest.mark.parametrize("forgery", ["claim", "reference", "policy", "order"])
def test_forged_proposal_is_not_accepted(business, forgery):
    def forge(payload):
        if forgery == "claim":
            payload["claims"][0]["value"] = "lost"
        elif forgery == "reference":
            payload["evidence_refs"][0]["evidence_id"] = "E-fake"
        elif forgery == "policy":
            payload["actions"][0]["policy_refs"][0]["version"] = 99
        else:
            payload["actions"][0]["order_id"] = "ORD-008"

    report = run(business, model=model_for(business, "T-RETURN-001", transform=forge))
    assert report["status"] == "validation_failed"
    assert report["accepted_proposal"] is None
    assert report["validation"]["issues"]


@pytest.mark.parametrize(
    ("settings", "models", "tools"),
    [
        (Settings(max_model_calls=1), 1, 1),
        (Settings(max_tool_calls=1), 2, 1),
        (Settings(max_tool_calls=7), 8, 7),
    ],
)
def test_call_limits_include_code_recheck_and_stop_safely(business, settings, models, tools):
    report = run(business, settings=settings)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "CallLimitExceeded"
    assert report["statistics"]["model_calls"] == models
    assert report["statistics"]["tool_calls"] == tools
    assert report["accepted_proposal"] is None
    assert report["executed_actions"] == []


def test_script_exception_is_diagnostic_and_does_not_persist_sensitive_error(business):
    model = ScriptedChatModel(
        steps=(ScriptStep("error", error=RuntimeError("secret-placeholder")),)
    )
    report = run(business, model=model)
    assert report["status"] == "failed"
    assert report["error"]["exception_type"] == "RuntimeError"
    assert "secret-placeholder" not in json.dumps(report)


def test_model_deadline_stops_the_agent(business):
    class SlowModel(ScriptedChatModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.sleep(1)
            return await super()._agenerate(*args, **kwargs)

    model = SlowModel(steps=(ScriptStep("slow", response=AIMessage(content="late")),))
    report = run(business, model=model, settings=Settings(model_timeout_seconds=0.01))
    assert report["error"]["code"] == "TimeoutError"
    assert report["statistics"]["model_calls"] == 1
    assert report["statistics"]["model_elapsed_ms"] > 0


def test_query_failure_remains_a_gap_not_a_business_rejection(business, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("backend problem")

    monkeypatch.setattr(business, "get_delivery_proof", fail)
    report = run(business)
    assert report["status"] == "completed"
    assert report["accepted_proposal"]["decision"] == "request_information"
    assert report["accepted_proposal"]["actions"] == []


def test_live_is_skipped_without_provider_calls(business):
    report = run(business, mode="live")
    assert report["status"] == "skipped"
    assert report["error"]["code"] == "LIVE_PROVIDER_DEFERRED"
    assert report["statistics"]["model_calls"] == report["statistics"]["tool_calls"] == 0


def test_run_artifacts_are_complete_and_cannot_be_overwritten(business, tmp_path):
    first = run(business)
    second = run(business)
    assert first["run_id"] != second["run_id"]
    assert first["accepted_proposal"]["decision"] == second["accepted_proposal"]["decision"]
    target = tmp_path / "reports" / "result.json"
    assert save_run(first, target) == target
    assert json.loads(target.read_text())["events"] == first["events"]
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        save_run(second, target)
    assert target.read_bytes() == before
    assert list(target.parent.iterdir()) == [target]
