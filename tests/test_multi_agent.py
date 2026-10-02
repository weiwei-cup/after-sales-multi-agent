import asyncio
import copy
import json
from dataclasses import replace

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from after_sales.agents.contracts import REPORT_ADAPTER, ResolutionProposal
from after_sales.agents.roles import call, create_role_model
from after_sales.agents.runner import run_baseline, save_run
from after_sales.agents.scripted import ScriptedChatModel, ScriptStep
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect
from after_sales.tools.service import ToolSession
from after_sales.workflows.contracts import ORDER_TOOLS, POLICY_TOOLS, Role
from after_sales.workflows.serial import run_multi

pytestmark = pytest.mark.integration


@pytest.fixture
def business(tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    return BusinessRepository(path)


def run(repository, ticket="T-RETURN-001", **kwargs):
    return asyncio.run(run_multi(repository, ticket, kwargs.pop("settings", Settings()), **kwargs))


def factory_with_change(stage, transform, *, repair_transform=None, capture=None):
    def factory(role, current_stage, payload):
        model = create_role_model(role, current_stage, payload)
        if capture is not None:
            capture.append((role, current_stage, copy.deepcopy(payload), model))
        if current_stage != stage:
            return model
        steps = list(model.steps)
        output_step = {
            "intake": "intake",
            "order": "order_output",
            "policy": "policy_output",
            "draft": "draft",
        }[stage]
        for index, step in enumerate(steps):
            change = (
                repair_transform
                if step.name == "repair"
                else transform
                if step.name == output_step
                else None
            )
            if change is None:
                continue
            original = step.response

            def mutate(messages, original=original, change=change):
                message = original(messages)
                arguments = copy.deepcopy(message.tool_calls[0]["args"])
                change(arguments)
                return message.model_copy(
                    update={"tool_calls": [{**message.tool_calls[0], "args": arguments}]}
                )

            steps[index] = replace(step, response=mutate)
        return ScriptedChatModel(steps=tuple(steps), script_id=model.script_id)

    return factory


@pytest.mark.parametrize(
    ("ticket", "decision"),
    [
        ("T-DELAY-001", "inform_progress"),
        ("T-DELAY-002", "propose_logistics_investigation"),
        ("T-NOTRECEIVED-001", "propose_logistics_investigation"),
        ("T-NOTRECEIVED-002", "propose_refund"),
        ("T-RETURN-001", "propose_return"),
        ("T-RETURN-002", "decline_request"),
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
def test_supported_ticket_facts_follow_the_serial_graph_without_business_writes(
    business, ticket, decision
):
    with connect(business.path, readonly=True) as connection:
        before = "\n".join(connection.iterdump())
    report = run(business, ticket)
    assert report["status"] == "completed", report["error"] or report["validation"]
    assert report["accepted_proposal"]["decision"] == decision
    assert report["node_trace"] == ["intake", "order", "policy", "draft", "validate"]
    assert report["candidate_only"] and report["validation"]["executable"] is False
    assert report["executed_actions"] == []
    assert report["statistics"]["model_calls"] <= 20
    assert report["statistics"]["tool_calls"] <= 30
    assert report["statistics"]["token_usage"] is None
    assert REPORT_ADAPTER.validate_python(report).architecture == "multi"
    with connect(business.path, readonly=True) as connection:
        assert "\n".join(connection.iterdump()) == before


@pytest.mark.parametrize(
    ("ticket", "trace", "decision", "tool_count"),
    [
        ("T-MISSING-001", ["intake", "draft", "validate"], "request_information", 0),
        ("T-CROSS-001", ["intake", "order", "draft", "validate"], "human_review", 1),
    ],
)
def test_missing_and_inaccessible_orders_skip_unrelated_specialists(
    business, ticket, trace, decision, tool_count
):
    report = run(business, ticket)
    assert report["status"] == "completed", report["error"]
    assert report["node_trace"] == trace
    assert report["accepted_proposal"]["decision"] == decision
    assert report["statistics"]["tool_calls"] == tool_count
    assert report["statistics"]["roles"]["policy_specialist"]["model_calls"] == 0
    assert report["accepted_proposal"]["order_id"] is None
    assert report["evidence"] == []


def test_unknown_intent_cannot_start_order_or_policy_investigation(business):
    with connect(business.path) as connection:
        connection.execute("UPDATE tickets SET type='unknown' WHERE id='T-RETURN-001'")
    report = run(business)
    assert report["status"] == "completed"
    assert report["node_trace"] == ["intake", "draft", "validate"]
    assert report["accepted_proposal"]["decision"] == "human_review"
    assert report["statistics"]["tool_calls"] == 0


def test_specialists_receive_projected_context_and_independent_messages(business):
    captured = []
    factory = factory_with_change("unused", lambda _: None, capture=captured)
    report = run(business, model_factory=factory)
    assert report["status"] == "completed"
    assert [stage for _, stage, _, _ in captured] == ["intake", "order", "policy", "draft"]
    _, _, order_input, order_model = captured[1]
    _, _, policy_input, policy_model = captured[2]
    assert set(order_input) == {"ticket_id", "order_id", "question"}
    assert set(policy_input) == {"ticket_id", "order_id", "intent", "order_findings"}
    assert "ticket_messages" not in json.dumps(policy_input)
    assert "received_at" in json.dumps(policy_input)
    assert "source_version" in json.dumps(policy_input)
    for _, _, payload, model in captured:
        first = model.observed_messages[0]
        assert len([m for m in first if isinstance(m, HumanMessage)]) == 1
        assert not any(isinstance(m, ToolMessage) for m in first)
        assert json.loads(next(m.content for m in first if isinstance(m, HumanMessage))) == payload
        assert "T-NOTRECEIVED-002" not in json.dumps(payload)
    order_tools = {
        c["name"]
        for batch in order_model.observed_messages
        for m in batch
        if m.type == "ai"
        for c in m.tool_calls
    }
    policy_tools = {
        c["name"]
        for batch in policy_model.observed_messages
        for m in batch
        if m.type == "ai"
        for c in m.tool_calls
    }
    assert order_tools <= {*ORDER_TOOLS, "OrderInvestigation"}
    assert policy_tools <= {*POLICY_TOOLS, "PolicyAssessment"}
    assert not any(
        name in json.dumps(order_model.observed_messages, default=str)
        for name in ("search_policies", "issue_mock_refund")
    )


def test_order_and_policy_tools_are_actually_allowlisted(business):
    session = ToolSession.for_ticket(business, "T-RETURN-001")
    try:
        assert [t.name for t in session.langchain_tools(ORDER_TOOLS)] == list(ORDER_TOOLS)
        assert [t.name for t in session.langchain_tools(POLICY_TOOLS)] == list(POLICY_TOOLS)
        assert session.langchain_tools(()) == []
        with pytest.raises(ValueError):
            session.langchain_tools(("issue_mock_refund",))
        with pytest.raises(ValueError):
            session.langchain_tools(("get_order", "get_order"))
    finally:
        session.close()


def test_graph_state_serializes_without_runtime_objects_credentials_or_agent_messages(
    business, tmp_path
):
    report = run(business, settings=Settings(model_api_key="unused-secret-placeholder"))
    state = report["graph_state"]
    assert json.loads(json.dumps(state)) == state
    serializer = JsonPlusSerializer()
    assert serializer.loads_typed(serializer.dumps_typed(state)) == state
    assert set(state) == {
        "schema_version",
        "run_id",
        "ticket_id",
        "input",
        "intake",
        "order_findings",
        "policy_assessment",
        "proposal",
        "validation",
        "route",
        "status",
        "node_trace",
    }
    for forbidden in (
        "unused-secret-placeholder",
        "model_api_key",
        "BusinessRepository",
        "ToolSession",
        "tool_call_id",
        '"messages"',
    ):
        assert forbidden not in json.dumps(state)
    assert "unused-secret-placeholder" not in json.dumps(report)
    target = tmp_path / "multi.json"
    save_run(report, target)
    stored = REPORT_ADAPTER.validate_json(target.read_text())
    assert stored.graph_state == state
    assert "order" in stored.graph_mermaid and "policy" in stored.graph_mermaid


@pytest.mark.parametrize("ticket", ["T-DELAY-001", "T-NOTRECEIVED-002", "T-RETURN-001"])
def test_single_and_multi_use_the_same_proposal_contract_and_business_result(business, ticket):
    single = asyncio.run(run_baseline(business, ticket, Settings()))
    multi = run(business, ticket)
    left = ResolutionProposal.model_validate(single["accepted_proposal"])
    right = ResolutionProposal.model_validate(multi["accepted_proposal"])
    assert left.decision == right.decision
    assert [(a.type, a.order_id, a.amount_cents, a.policy_refs) for a in left.actions] == [
        (a.type, a.order_id, a.amount_cents, a.policy_refs) for a in right.actions
    ]
    assert multi["statistics"]["model_calls"] > single["statistics"]["model_calls"]


@pytest.mark.parametrize("stage", ["order", "policy"])
def test_forged_specialist_facts_are_rejected_before_drafting(business, stage):
    def forge(payload):
        findings = payload["facts"] if stage == "order" else payload["assessments"]
        findings[0]["facts"]["invented"] = "untrusted"

    report = run(business, model_factory=factory_with_change(stage, forge))
    assert report["status"] == "failed"
    assert report["error"]["code"] == "HandoffRejected"
    assert report["error"]["node"] == stage
    assert report["accepted_proposal"] is None and report["proposal"] is None
    assert "draft" not in report["node_trace"]


def test_coordinator_cannot_guess_or_replace_the_supplied_order(business):
    report = run(
        business,
        model_factory=factory_with_change(
            "intake", lambda p: p["slots"].update(order_id="ORD-008")
        ),
    )
    assert report["error"]["code"] == "HandoffRejected"
    assert report["statistics"]["tool_calls"] == 0


def test_schema_valid_excess_refund_fails_the_parent_code_validation(business):
    def excessive(payload):
        payload["actions"][0]["amount_cents"] = 10001

    report = run(
        business, "T-NOTRECEIVED-002", model_factory=factory_with_change("draft", excessive)
    )
    assert report["status"] == "validation_failed"
    assert report["proposal"]["actions"][0]["amount_cents"] == 10001
    assert report["accepted_proposal"] is None
    assert report["validation"]["executable"] is False
    assert report["executed_actions"] == []


def test_multi_schema_repair_and_persistent_failure_are_bounded(business):
    def invalid(payload):
        payload["decision"] = "unknown-output"

    repaired = run(business, model_factory=factory_with_change("draft", invalid))
    assert repaired["status"] == "completed"
    assert repaired["statistics"]["schema_repairs"] == 1
    failed = run(
        business, model_factory=factory_with_change("draft", invalid, repair_transform=invalid)
    )
    assert failed["error"]["code"] == "SchemaRepairExceeded"
    assert failed["statistics"]["schema_repairs"] == 1
    assert failed["accepted_proposal"] is None


@pytest.mark.parametrize(
    ("settings", "failed_node"),
    [
        (Settings(max_model_calls=1), "order"),
        (Settings(max_tool_calls=1), "order"),
        (Settings(max_tool_calls=7), "validate"),
    ],
)
def test_shared_budget_is_not_reset_between_roles_and_includes_validation(
    business, settings, failed_node
):
    report = run(business, settings=settings)
    assert report["error"]["code"] == "CallLimitExceeded"
    assert report["error"]["node"] == failed_node
    assert report["statistics"]["model_calls"] <= settings.max_model_calls
    assert report["statistics"]["tool_calls"] <= settings.max_tool_calls
    assert report["accepted_proposal"] is None


def test_query_failure_remains_a_gap_across_role_handoffs(business, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private-backend-placeholder")

    monkeypatch.setattr(business, "get_delivery_proof", fail)
    report = run(business)
    assert report["status"] == "completed"
    assert report["accepted_proposal"]["decision"] == "request_information"
    assert report["accepted_proposal"]["actions"] == []
    assert (
        report["graph_state"]["order_findings"]["tool_errors"][0]["error"]["code"] == "QUERY_FAILED"
    )
    assert "private-backend-placeholder" not in json.dumps(report)


def test_multi_live_remains_explicitly_skipped_without_any_role_call(business):
    report = run(business, mode="live")
    assert report["status"] == "skipped"
    assert report["error"]["code"] == "LIVE_PROVIDER_DEFERRED"
    assert report["node_trace"] == [] and report["agent_runs"] == []
    assert report["statistics"]["model_calls"] == report["statistics"]["tool_calls"] == 0


def early_invalid_factory(stage, schema, change):
    def factory(role, current_stage, payload):
        model = create_role_model(role, current_stage, payload)
        if current_stage != stage:
            return model
        steps = list(model.steps)
        original = steps[1].response

        def mutate(messages):
            message = original(messages)
            assert message.tool_calls[0]["name"] == schema
            arguments = copy.deepcopy(message.tool_calls[0]["args"])
            change(arguments)
            return message.model_copy(
                update={"tool_calls": [{**message.tool_calls[0], "args": arguments}]}
            )

        steps[1] = replace(steps[1], response=mutate)
        return ScriptedChatModel(steps=tuple(steps))

    return factory


def test_unavailable_order_can_repair_its_early_summary_without_more_business_reads(business):
    factory = early_invalid_factory(
        "order", "OrderInvestigation", lambda p: p.update(outcome="invalid-outcome")
    )
    report = run(business, "T-CROSS-001", model_factory=factory)
    assert report["status"] == "completed", report["error"]
    assert report["statistics"]["schema_repairs"] == 1
    assert report["statistics"]["tool_calls"] == 1
    assert "policy" not in report["node_trace"]


def test_failed_policy_search_can_repair_its_early_summary(business, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("controlled query failure")

    monkeypatch.setattr(business, "list_policies", fail)
    factory = early_invalid_factory("policy", "PolicyAssessment", lambda p: p.update(order_id=None))
    report = run(business, model_factory=factory)
    assert report["status"] == "completed", report["error"]
    assert report["accepted_proposal"]["decision"] == "request_information"
    assert report["statistics"]["schema_repairs"] == 1
    assert report["statistics"]["tool_calls"] == 6


@pytest.mark.parametrize(
    ("role", "tool"), [(Role.ORDER, "search_policies"), (Role.POLICY, "get_order")]
)
def test_specialist_cannot_call_another_roles_tool(business, role, tool):
    def factory(current_role, stage, payload):
        if current_role == role:
            return ScriptedChatModel(
                steps=(ScriptStep("forbidden", response=call(tool, {}, "forbidden-call")),)
            )
        return create_role_model(current_role, stage, payload)

    report = run(business, model_factory=factory)
    assert report["error"]["code"] == "ScriptError"
    assert report["error"]["node"] == ("order" if role == Role.ORDER else "policy")
    assert not any(
        e["kind"] == "tool_started" and e["role"] == role.value for e in report["events"]
    )
    assert report["accepted_proposal"] is None


def test_forged_handoff_reference_has_an_explicit_diagnostic(business):
    def forge(payload):
        payload["facts"][0]["ref"]["evidence_id"] = "E-other-session"

    report = run(business, model_factory=factory_with_change("order", forge))
    assert report["error"]["code"] == "HandoffRejected"
    assert report["accepted_proposal"] is None


def test_role_exception_retains_partial_state_without_persisting_raw_error(business):
    def factory(role, stage, payload):
        if role == Role.POLICY:
            return ScriptedChatModel(
                steps=(ScriptStep("error", error=RuntimeError("private-secret-placeholder")),)
            )
        return create_role_model(role, stage, payload)

    report = run(business, model_factory=factory)
    assert report["status"] == "failed"
    assert report["graph_state"]["order_findings"]["outcome"] == "ready"
    assert report["error"]["node"] == "policy"
    assert "private-secret-placeholder" not in json.dumps(report)


def test_schema_repair_budget_is_shared_between_specialists(business):
    order_factory = factory_with_change("order", lambda p: p.update(outcome="invalid-outcome"))
    policy_factory = factory_with_change("policy", lambda p: p.update(order_id=None))

    def factory(role, stage, payload):
        return (policy_factory if role == Role.POLICY else order_factory)(role, stage, payload)

    report = run(business, model_factory=factory)
    assert report["error"]["code"] == "SchemaRepairExceeded"
    assert report["error"]["node"] == "policy"
    assert report["statistics"]["schema_repairs"] == 1
    assert report["accepted_proposal"] is None
