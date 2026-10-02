import asyncio
import json
from threading import Event, Lock

import pytest
from langchain_core.messages import ToolMessage

from after_sales.domain.models import ActionType
from after_sales.repositories.errors import OrderAccessDenied
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect
from after_sales.tools.contracts import ErrorCode, EvidenceRef, ToolResult
from after_sales.tools.evidence import EvidenceStore
from after_sales.tools.inspection import inspect_ticket
from after_sales.tools.service import ToolSession

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions(tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    repository = BusinessRepository(path)
    opened = []

    def create(ticket="T-RETURN-001", **kwargs):
        session = ToolSession.for_ticket(repository, ticket, **kwargs)
        opened.append(session)
        return session

    yield create
    for session in opened:
        session.close()


async def facts(session, *, action=ActionType.RETURN_REQUEST):
    order_id = session.context.supplied_order_id
    result = {}
    arguments = {"action": action}
    for name, field in (
        ("get_order", "order_ref"),
        ("get_order_products", "products_ref"),
        ("get_tracking", "tracking_ref"),
        ("get_after_sales_history", "history_ref"),
    ):
        result[name] = await session.call(name, {"order_id": order_id})
        assert result[name].ok
        arguments[field] = result[name].evidence_refs[0]
    policies = await session.call("search_policies", {"intent": session.context.intent})
    assert policies.ok
    arguments["policy_refs"] = tuple(
        ref
        for p, ref in zip(policies.data["policies"], policies.evidence_refs, strict=True)
        if action in p["allowed_actions"]
    )
    return arguments, result


@pytest.mark.parametrize(
    "name",
    [
        "get_order",
        "get_order_products",
        "get_tracking",
        "get_delivery_proof",
        "get_after_sales_history",
    ],
)
def test_cross_customer_order_never_exposes_facts_or_evidence(sessions, name):
    session = sessions("T-CROSS-001")
    result = asyncio.run(session.call(name, {"order_id": "ORD-008"}))
    assert result.error.code == ErrorCode.NOT_OWNED
    assert result.data is None and result.evidence_refs == ()
    assert session.evidence.export(session.context) == []


def test_trusted_context_must_match_ticket(sessions):
    with pytest.raises(OrderAccessDenied):
        sessions(customer_id="CUST-A")


def test_own_order_can_be_read_and_other_orders_of_same_customer_cannot_be_guessed(sessions):
    session = sessions()
    good = asyncio.run(session.call("get_order", {"order_id": "ORD-005"}))
    assert good.ok and good.data["customer_id"] == "CUST-B"
    bad = asyncio.run(session.call("get_tracking", {"order_id": "ORD-002"}))
    assert bad.error.code == ErrorCode.ORDER_SCOPE_MISMATCH


@pytest.mark.parametrize(
    "extra",
    [{"customer_id": "CUST-B"}, {"paid_cents": 999999}, {"received_at": "2026-10-02T00:00:00Z"}],
)
def test_model_cannot_inject_identity_or_facts_into_query_schema(sessions, extra):
    session = sessions()
    result = asyncio.run(session.call("get_order", {"order_id": "ORD-005", **extra}))
    assert result.error.code == ErrorCode.INVALID_ARGUMENT


def test_langchain_tool_call_returns_schema_and_preserves_call_id(sessions):
    session = sessions()
    tools = {tool.name: tool for tool in session.langchain_tools()}
    tool = tools["get_order"]
    assert "customer_id" not in tool.args_schema.model_fields
    assert tool.metadata["result_schema"]["title"] == "ToolResult"
    message = asyncio.run(
        tool.ainvoke(
            {
                "type": "tool_call",
                "id": "call-test-1",
                "name": "get_order",
                "args": {"order_id": "ORD-005"},
            }
        )
    )
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-test-1"
    assert ToolResult.model_validate_json(message.content).ok
    invalid = asyncio.run(tool.ainvoke({"order_id": "ORD-005", "customer_id": "CUST-A"}))
    assert ToolResult.model_validate_json(invalid).error.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("ticket", "expected"),
    [
        ("T-RETURN-001", "eligible"),
        ("T-RETURN-002", "ineligible"),
        ("T-API-DUPLICATE-001", "eligible"),
        ("T-UI-RESUME-001", "existing_application"),
        ("T-CONFLICT-001", "policy_conflict"),
    ],
)
def test_inspection_uses_real_tools_and_rules_without_mutating_database(sessions, ticket, expected):
    session = sessions(ticket)
    with connect(session.repository.path, readonly=True) as connection:
        before = "\n".join(connection.iterdump())
    report = asyncio.run(inspect_ticket(session))
    assessment = report["results"]["evaluate_policy:create_return_request"]
    assert assessment["ok"]
    assert assessment["data"]["disposition"] == expected
    assert report["model_calls"] == 0 and report["candidate_only"]
    assert report["evidence"]
    with connect(session.repository.path, readonly=True) as connection:
        assert "\n".join(connection.iterdump()) == before


def test_conflict_cannot_be_hidden_by_omitting_one_policy(sessions):
    session = sessions("T-CONFLICT-001")

    async def scenario():
        arguments, _ = await facts(session)
        standard = await session.call("get_policy", {"policy_id": "RETURN-STANDARD", "version": 1})
        arguments["policy_refs"] = standard.evidence_refs
        result = await session.call("evaluate_policy", arguments)
        assert result.data["disposition"] == "policy_conflict"
        assert result.data["eligible"] is False
        assert result.data["missing_policy_refs"] == [
            {"policy_id": "RETURN-CONFLICT", "version": 1}
        ]
        refs = result.data["input_evidence_refs"]
        assert (await session.call("validate_evidence_refs", {"refs": refs})).ok

    asyncio.run(scenario())


def test_missing_history_is_unknown_and_cannot_get_approved_or_rejected(sessions):
    session = sessions("T-RETURN-002")

    async def scenario():
        arguments, _ = await facts(session)
        arguments.pop("history_ref")
        result = await session.call("evaluate_policy", arguments)
        assert result.data["disposition"] == "needs_information"
        assert result.data["eligible"] is False

    asyncio.run(scenario())


def test_fake_factual_arguments_cannot_override_rule_evidence(sessions):
    session = sessions("T-RETURN-002")

    async def scenario():
        arguments, raw = await facts(session)
        raw["get_order"].data["received_at"] = session.context.as_of_time.isoformat()
        result = await session.call("evaluate_policy", arguments)
        assert result.data["disposition"] == "ineligible"
        forged = await session.call("evaluate_policy", {**arguments, "received_at": "now"})
        assert forged.error.code == ErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_proof_explicitly_missing_is_distinct_from_no_record_and_query_error(sessions, monkeypatch):
    missing = sessions("T-NOTRECEIVED-001")
    absent = sessions()
    first = asyncio.run(missing.call("get_delivery_proof", {"order_id": "ORD-003"}))
    second = asyncio.run(absent.call("get_delivery_proof", {"order_id": "ORD-005"}))
    assert first.ok and first.data["proof_status"] == "missing"
    assert second.ok and second.data["proof_status"] == "unknown"
    assert second.data["availability"] == "not_collected"

    def fail(*args, **kwargs):
        raise OSError("sensitive backend detail")

    monkeypatch.setattr(absent.repository, "get_delivery_proof", fail)
    failed = asyncio.run(absent.call("get_delivery_proof", {"order_id": "ORD-005"}))
    assert failed.error.code == ErrorCode.QUERY_FAILED
    assert failed.data is None and not failed.evidence_refs
    assert "sensitive" not in failed.model_dump_json()


def test_timeout_has_no_fact_and_timed_out_reads_have_bounded_capacity(sessions, monkeypatch):
    session = sessions(timeout_seconds=0.02)
    release, finished = Event(), Event()
    lock = Lock()
    completed = 0

    def blocked(*args, **kwargs):
        nonlocal completed
        release.wait(timeout=2)
        with lock:
            completed += 1
            if completed == 2:
                finished.set()
        return None

    monkeypatch.setattr(session.repository, "get_delivery_proof", blocked)

    async def scenario():
        try:
            for _ in range(2):
                result = await session.call("get_delivery_proof", {"order_id": "ORD-005"})
                assert result.error.code == ErrorCode.TOOL_TIMEOUT
                assert result.data is None and not result.evidence_refs
            busy = await session.call("get_delivery_proof", {"order_id": "ORD-005"})
            assert busy.error.code == ErrorCode.TOOL_BUSY
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.sleep(0.01)
        assert session.evidence.export(session.context) == []

    asyncio.run(scenario())


def test_result_limit_does_not_truncate_json_or_register_invisible_evidence(sessions):
    session = sessions(max_result_bytes=512)
    result = asyncio.run(session.call("search_policies", {"intent": session.context.intent}))
    assert result.error.code == ErrorCode.RESULT_TOO_LARGE
    assert len(result.model_dump_json().encode()) <= 512
    assert not session.evidence.export(session.context)


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("fake", ErrorCode.EVIDENCE_NOT_FOUND),
        ("version", ErrorCode.EVIDENCE_VERSION_MISMATCH),
        ("other_ticket", ErrorCode.EVIDENCE_SCOPE_MISMATCH),
        ("other_session", ErrorCode.EVIDENCE_SCOPE_MISMATCH),
    ],
)
def test_evidence_validation_rejects_fabricated_or_out_of_scope_references(sessions, variant, code):
    shared = EvidenceStore()
    session = sessions(evidence_store=shared)
    original = asyncio.run(session.call("get_order", {"order_id": "ORD-005"})).evidence_refs[0]
    target, ref = session, original
    if variant == "fake":
        ref = EvidenceRef(evidence_id="E-fake", source_version="1")
    elif variant == "version":
        ref = original.model_copy(update={"source_version": "99"})
    elif variant == "other_ticket":
        target = sessions("T-RETURN-002", evidence_store=shared)
    else:
        target = sessions(evidence_store=shared)
    result = asyncio.run(target.call("validate_evidence_refs", {"refs": [ref]}))
    assert result.error.code == code


def test_evidence_deduplicates_and_wrong_source_type_is_rejected(sessions):
    session = sessions()

    async def scenario():
        arguments, _ = await facts(session)
        count = len(session.evidence.export(session.context))
        repeated = await session.call("get_order", {"order_id": "ORD-005"})
        assert repeated.evidence_refs[0] == arguments["order_ref"]
        assert len(session.evidence.export(session.context)) == count
        arguments["history_ref"] = arguments["order_ref"]
        result = await session.call("evaluate_policy", arguments)
        assert result.error.code == ErrorCode.EVIDENCE_SOURCE_MISMATCH

    asyncio.run(scenario())


def test_explicit_future_policy_version_is_readable_but_not_applicable(sessions):
    session = sessions()

    async def scenario():
        arguments, _ = await facts(session)
        future = await session.call("get_policy", {"policy_id": "RETURN-STANDARD", "version": 2})
        assert future.data["conditions"]["window_hours"] == 120
        arguments["policy_refs"] = future.evidence_refs
        result = await session.call("evaluate_policy", arguments)
        assert result.error.code == ErrorCode.INVALID_ARGUMENT
        assert result.data is None and result.evidence_refs == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("amount", [-1, 0, 1])
def test_non_refund_assessment_cannot_create_a_rejection_using_an_amount(sessions, amount):
    session = sessions()

    async def scenario():
        arguments, _ = await facts(session)
        result = await session.call(
            "evaluate_policy", {**arguments, "requested_amount_cents": amount}
        )
        assert result.error.code == ErrorCode.INVALID_ARGUMENT
        assert result.data is None and not result.evidence_refs

    asyncio.run(scenario())


@pytest.mark.parametrize("policy_id,version", [("RETURN-STANDARD", 2), ("LOST-REFUND", 1)])
def test_extra_inapplicable_policy_cannot_turn_an_eligible_return_into_a_rejection(
    sessions, policy_id, version
):
    session = sessions()

    async def scenario():
        arguments, _ = await facts(session)
        valid = await session.call("evaluate_policy", arguments)
        assert valid.data["eligible"] is True
        unrelated = await session.call("get_policy", {"policy_id": policy_id, "version": version})
        arguments["policy_refs"] = (*arguments["policy_refs"], *unrelated.evidence_refs)
        before = session.evidence.export(session.context)
        result = await session.call("evaluate_policy", arguments)
        assert not result.ok and result.error.code == ErrorCode.INVALID_ARGUMENT
        assert result.data is None and not result.evidence_refs
        assert session.evidence.export(session.context) == before

    asyncio.run(scenario())


def test_prompt_injection_in_tracking_is_data_and_cannot_authorize_refund(sessions):
    session = sessions("T-PROMPT-INJECTION-001")
    report = asyncio.run(inspect_ticket(session))
    assert "忽略" in json.dumps(report["results"]["get_tracking"]["data"], ensure_ascii=False)
    assert report["results"]["evaluate_policy:issue_mock_refund"]["data"]["eligible"] is False


def test_snapshot_read_is_versioned_immutable_and_scoped(sessions):
    shared = EvidenceStore()
    session = sessions(evidence_store=shared)
    other = sessions("T-RETURN-002", evidence_store=shared)

    async def scenario():
        result = await session.call("get_order", {"order_id": "ORD-005"})
        ref = result.evidence_refs[0]
        result.data["paid_cents"] = 1
        snapshot = await session.call("get_evidence", {"ref": ref})
        assert snapshot.data["facts"]["paid_cents"] == 10000
        assert snapshot.data["source_id"] == "ORD-005"
        assert snapshot.data["observed_at"] == "2026-10-02T04:00:00Z"
        assert (
            await other.call("get_evidence", {"ref": ref})
        ).error.code == ErrorCode.EVIDENCE_SCOPE_MISMATCH

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("ticket", "number", "amount", "remaining", "disposition"),
    [
        ("T-RETURN-001", 23, 8900, 8900, "eligible"),
        ("T-RETURN-001", 23, 8901, 8900, "ineligible"),
        ("T-RETURN-001", 23, -1, 8900, "ineligible"),
        ("T-RETURN-002", 30, 1, 0, "ineligible"),
    ],
)
def test_refund_assessment_uses_repository_balance(
    sessions, ticket, number, amount, remaining, disposition
):
    initial = sessions(ticket)
    order_id = f"ORD-{number:03d}"
    with connect(initial.repository.path) as connection:
        connection.execute(
            "UPDATE tickets SET supplied_order_id=?,order_id=?,type='delivered_not_received' "
            "WHERE id=?",
            (order_id, order_id, ticket),
        )
    session = sessions(ticket)

    async def scenario():
        arguments, _ = await facts(session, action=ActionType.MOCK_REFUND)
        arguments["requested_amount_cents"] = amount
        result = await session.call("evaluate_policy", arguments)
        assert result.data["disposition"] == disposition
        assert result.data["evaluations"][0]["remaining_refund_cents"] == remaining
        assert result.data["evaluations"][0]["requested_amount_cents"] == amount

    asyncio.run(scenario())


def test_missing_order_differs_from_missing_reference_and_cross_customer(sessions):
    initial = sessions()
    with connect(initial.repository.path) as connection:
        connection.execute(
            "UPDATE tickets SET supplied_order_id='ORD-ABSENT',order_id=NULL "
            "WHERE id='T-RETURN-001'"
        )
    session = sessions()
    result = asyncio.run(session.call("get_order", {"order_id": "ORD-ABSENT"}))
    assert result.error.code == ErrorCode.NOT_FOUND
    missing = sessions("T-MISSING-001")
    assert (
        asyncio.run(missing.call("get_order", {"order_id": "ORD-007"})).error.code
        == ErrorCode.MISSING_REFERENCE
    )


def test_missing_receipt_time_stays_unknown_through_tool_assessment(sessions):
    initial = sessions("T-DELAY-001")
    with connect(initial.repository.path) as connection:
        connection.execute(
            "UPDATE tickets SET supplied_order_id='ORD-028',order_id='ORD-028',"
            "type='return_request' "
            "WHERE id='T-DELAY-001'"
        )
    session = sessions("T-DELAY-001")
    report = asyncio.run(inspect_ticket(session))
    assert (
        report["results"]["evaluate_policy:create_return_request"]["data"]["disposition"]
        == "needs_information"
    )


def test_policy_search_does_not_allow_scope_or_clock_override(sessions):
    session = sessions()
    wrong_intent = asyncio.run(session.call("search_policies", {"intent": "logistics_delay"}))
    wrong_clock = asyncio.run(
        session.call(
            "search_policies",
            {"intent": session.context.intent, "as_of_time": "2026-11-01T00:00:00Z"},
        )
    )
    assert wrong_intent.error.code == wrong_clock.error.code == ErrorCode.INVALID_ARGUMENT
    cross = sessions("T-CROSS-001")
    denied = asyncio.run(cross.call("search_policies", {"intent": cross.context.intent}))
    assert denied.error.code == ErrorCode.NOT_OWNED


def test_invalid_backend_fact_is_a_query_failure_not_a_model_argument_error(sessions, monkeypatch):
    from after_sales.domain.models import Order

    session = sessions()

    def malformed(*args, **kwargs):
        return Order.model_validate({"paid_cents": -1})

    monkeypatch.setattr(session.repository, "get_order", malformed)
    result = asyncio.run(session.call("get_order", {"order_id": "ORD-005"}))
    assert result.error.code == ErrorCode.QUERY_FAILED
    assert result.data is None and result.evidence_refs == ()
