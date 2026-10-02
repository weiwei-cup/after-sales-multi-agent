import asyncio
import copy
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from after_sales.agents.contracts import REPORT_ADAPTER, ResumeInput
from after_sales.agents.review import create_review_model
from after_sales.agents.runner import save_run
from after_sales.agents.scripted import ScriptedChatModel
from after_sales.config import Settings
from after_sales.domain.models import TicketType
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect
from after_sales.workflows.contracts import ORDER_TOOLS, POLICY_TOOLS, Role
from after_sales.workflows.reviewed import (
    InMemoryReviewRun,
    ResumeRejected,
    response_envelope,
    run_reviewed,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def business(tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    return BusinessRepository(path)


def dump(repository):
    with connect(repository.path, readonly=True) as connection:
        return tuple(connection.iterdump())


def settings(**kwargs):
    return Settings(max_model_calls=100, max_tool_calls=100, **kwargs)


def run(repository, ticket="T-RETURN-001", **kwargs):
    return asyncio.run(
        run_reviewed(repository, ticket, kwargs.pop("settings", Settings()), **kwargs)
    )


def mutate_factory(*, draft_change=None, review_change=None, capture=None):
    counters = {"draft": 0, "review": 0}

    def factory(role, stage, payload):
        if capture is not None:
            capture.append((role, stage, copy.deepcopy(payload)))
        model = create_review_model(role, stage, payload)
        change = (
            draft_change
            if role == Role.COORDINATOR and stage == "draft"
            else review_change
            if role == Role.REVIEW
            else None
        )
        if change is None:
            return model
        counters[stage] += 1
        count = counters[stage]
        steps = []
        for step in model.steps:
            if step.name in {"draft", "review", "repair"}:
                original = step.response

                def mutate(messages, original=original):
                    message = original(messages)
                    args = copy.deepcopy(message.tool_calls[0]["args"])
                    change(args, count)
                    return message.model_copy(
                        update={"tool_calls": [{**message.tool_calls[0], "args": args}]}
                    )

                step = replace(step, response=mutate)
            steps.append(step)
        return ScriptedChatModel(script_id=model.script_id, steps=tuple(steps))

    return factory


@pytest.mark.parametrize(
    "ticket",
    [
        "T-DELAY-001",
        "T-DELAY-002",
        "T-NOTRECEIVED-001",
        "T-NOTRECEIVED-002",
        "T-RETURN-001",
        "T-RETURN-002",
        "T-CONFLICT-001",
        "T-CROSS-001",
        "T-MISSING-001",
    ],
)
def test_reviewed_real_graph_preserves_business_and_valid_report(business, ticket):
    before = dump(business)
    report = run(business, ticket)
    assert report["error"] is None
    assert report["status"] in {"completed", "paused", "handed_off"}
    assert "review" in report["node_trace"]
    assert report["executed_actions"] == [] and report["candidate_only"]
    assert report["resume_scope"] == "same_process_only"
    assert REPORT_ADAPTER.validate_python(report).schema_version == "review-run-v1"
    assert dump(business) == before
    json.dumps(report["graph_state"])


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_operator_real_interrupt_and_resume(business, decision):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        try:
            before = dump(business)
            paused = await owner.start()
            pending = paused["pending_input"]
            snapshot = await owner.graph.aget_state(owner.config)
            assert snapshot.next == ("wait_operator",)
            assert snapshot.tasks[0].interrupts[0].value == pending
            assert not any(e["kind"] == "node_failed" for e in paused["events"])
            finished = await owner.resume(response_envelope(pending, decision=decision))
            assert finished["status"] == ("completed" if decision == "approve" else "handed_off")
            assert finished["pending_input"] is None
            assert len(finished["pending_history"]) == 1 and len(finished["confirmations"]) == 1
            assert sum(e["kind"] == "pending_created" for e in finished["events"]) == 1
            assert sum(e["kind"] == "pending_consumed" for e in finished["events"]) == 1
            assert (
                sum(
                    e["kind"] == "node_started" and e["node"] == "wait_operator"
                    for e in finished["events"]
                )
                == 2
            )
            assert finished["executed_actions"] == []
            assert dump(business) == before
            with pytest.raises(ResumeRejected):
                await owner.resume(response_envelope(pending, decision=decision))
            # Returned report cannot mutate the live registry/checkpoint or older snapshots.
            finished["graph_state"]["proposal"]["customer_reply_draft"] = "changed outside"
            assert owner.report()["proposal"]["customer_reply_draft"] != "changed outside"
            assert paused["status"] == "paused" and not paused["confirmations"]
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("order_id", ["ORD-001", "ORD-019", "ORD-002", "ORD-NOTFOUND"])
def test_missing_order_answer_is_reinvestigated_and_never_guesses_identity(business, order_id):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-MISSING-001", Settings())
        try:
            before = dump(business)
            paused = await owner.start()
            assert paused["pending_input"]["kind"] == "customer_info"
            response = response_envelope(paused["pending_input"], answers={"order_id": order_id})
            report = await owner.resume(response)
            assert report["input_revision"] == 2 and report["proposal_revision"] == 2
            assert report["input"]["customer_id"] == paused["input"]["customer_id"]
            assert report["input"]["supplied_order_id"] == order_id
            assert report["node_trace"].count("intake") == 2
            if order_id == "ORD-001":
                assert report["status"] == "completed"
            elif order_id == "ORD-019":
                assert (
                    report["status"] == "paused"
                    and report["pending_input"]["kind"] == "operator_decision"
                )
                report = await owner.resume(
                    response_envelope(report["pending_input"], decision="approve")
                )
                assert report["status"] == "completed"
            else:
                assert report["status"] == "handed_off" and report["accepted_proposal"] is None
                assert not report["graph_state"]["order_findings"]["facts"]
            assert not any(
                p["kind"] == "customer_info" and p["proposal_revision"] == 2
                for p in report["pending_history"]
            )
            assert dump(business) == before
            with pytest.raises(ResumeRejected):
                await owner.resume(response)
        finally:
            owner.close()

    asyncio.run(scenario())


def test_proof_failure_researches_only_proof_and_preserves_policy_and_other_facts(
    business, monkeypatch
):
    original = business.get_delivery_proof
    calls = []

    def flaky(order_id, *, customer_id):
        calls.append(order_id)
        if len(calls) == 1:
            raise OSError("transient demo read failure")
        return original(order_id, customer_id=customer_id)

    monkeypatch.setattr(business, "get_delivery_proof", flaky)
    captures = []
    report = run(business, "T-PROOF-ERROR-001", model_factory=mutate_factory(capture=captures))
    assert report["status"] == "paused" and report["repair_count"] == 1
    assert calls == [report["proposal"]["order_id"]] * 2
    runs = report["agent_runs"]
    research = next(r for r in runs if r["stage"] == "research")
    assert research["input"]["tools"] == ["get_delivery_proof"]
    assert research["tool_calls"] == 1
    initial = next(r["output"] for r in runs if r["stage"] == "order")
    assert [f for f in initial["facts"] if f["source_type"] != "proof"] == [
        f for f in research["output"]["facts"] if f["source_type"] != "proof"
    ]
    assert sum(r["role"] == Role.POLICY.value for r in runs) == 1
    initial_policy = next(r["output"] for r in runs if r["role"] == Role.POLICY.value)
    assert initial_policy == report["graph_state"]["policy_assessment"]
    review_runs = [r for r in runs if r["role"] == Role.REVIEW.value]
    assert [r["output"]["outcome"] for r in review_runs] == ["research", "accept"]


def test_refreshing_rule_inputs_recomputes_dependent_policy(business, monkeypatch):
    original = business.get_tracking
    calls = 0

    def flaky(order_id, *, customer_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient")
        return original(order_id, customer_id=customer_id)

    monkeypatch.setattr(business, "get_tracking", flaky)
    report = run(business, "T-DELAY-002", settings=settings())
    assert report["status"] == "paused" and report["repair_count"] == 1
    assert calls == 2
    assert sum(r["role"] == Role.POLICY.value for r in report["agent_runs"]) == 2
    assert any(e["kind"] == "dependent_policy_recheck" for e in report["events"])


def test_wording_repair_runs_only_coordinator_and_reviewer(business):
    def change(args, count):
        if count == 1:
            args["customer_reply_draft"] = "退款已经到账，保证赔偿。"

    report = run(business, model_factory=mutate_factory(draft_change=change))
    assert report["status"] == "paused" and report["repair_count"] == 1
    assert sum(r["stage"] == "order" for r in report["agent_runs"]) == 1
    assert sum(r["stage"] == "policy" for r in report["agent_runs"]) == 1
    assert not any(r["stage"] == "research" for r in report["agent_runs"])
    assert "已经到账" not in report["proposal"]["customer_reply_draft"]
    repaired = [r for r in report["agent_runs"] if r["stage"] == "draft"][1]
    assert repaired["input"]["review_feedback"]["outcome"] == "revise"


@pytest.mark.parametrize("failure", ["wording", "code", "mixed", "review"])
def test_code_and_review_share_two_repair_limit_and_handoff(business, failure):
    def draft_change(args, count):
        if failure == "wording" or failure == "mixed" and count == 1:
            args["customer_reply_draft"] = "保证退款已经完成"
        if failure == "code" or failure == "mixed" and count > 1:
            args["actions"][0]["evidence_refs"] = copy.deepcopy(args["evidence_refs"])
            args["actions"][0]["evidence_refs"][0]["source_version"] = "invalid-version"

    def review_change(args, count):
        args.clear()
        if failure == "review":
            args.update(
                outcome="revise",
                issues=[{"code": "WORDING", "message": "needs revision"}],
                revision_instructions="rewrite",
            )
        else:
            args.update(outcome="accept")

    report = run(
        business,
        model_factory=mutate_factory(draft_change=draft_change, review_change=review_change),
        settings=settings(),
    )
    assert report["status"] == "handed_off" and report["repair_count"] == 2
    assert report["graph_state"]["reason"] == "REPAIR_LIMIT_REACHED"
    assert sum(r["stage"] == "draft" for r in report["agent_runs"]) == 3
    assert report["accepted_proposal"] is None and report["pending_input"] is None
    if failure in {"code", "mixed"}:
        assert not report["validation"]["ok"]
        assert all(
            r["output"]["outcome"] == "accept"
            for r in report["agent_runs"]
            if r["role"] == Role.REVIEW.value
        )


def test_persistent_query_failure_is_bounded(business, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("always unavailable")

    monkeypatch.setattr(business, "get_delivery_proof", failed)
    report = run(business, settings=settings())
    assert report["status"] == "handed_off" and report["repair_count"] == 2
    assert sum(r["stage"] == "research" for r in report["agent_runs"]) == 2
    assert len(report["node_trace"]) < 25


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pending_id", "pending-wrong"),
        ("run_id", "run-wrong"),
        ("ticket_id", "T-WRONG"),
        ("actor_id", "OP-WRONG"),
        ("input_revision", 2),
        ("proposal_revision", 2),
        ("proposal_hash", "0" * 64),
        ("action_hashes", {}),
        ("action_hashes", {"action-wrong": "0" * 64}),
    ],
)
def test_wrong_resume_bindings_are_rejected_without_mutation(business, field, value):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        try:
            report = await owner.start()
            before = owner.report()
            envelope = response_envelope(report["pending_input"], decision="approve")
            envelope[field] = value
            with pytest.raises(ResumeRejected):
                await owner.resume(envelope)
            after = owner.report()
            for name in ("graph_state", "events", "pending_history", "confirmations", "statistics"):
                if name != "statistics":
                    assert after[name] == before[name]
            assert not owner.consumed
            assert (
                await owner.resume(response_envelope(report["pending_input"], decision="reject"))
            )["status"] == "handed_off"
        finally:
            owner.close()

    asyncio.run(scenario())


def test_customer_cannot_approve_operator_pending(business):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        try:
            report = await owner.start()
            envelope = response_envelope(report["pending_input"], decision="approve")
            envelope.update(role="customer", actor_id=report["input"]["customer_id"])
            with pytest.raises(ValidationError):
                await owner.resume(envelope)
            envelope.update(decision=None, answers={"order_id": "ORD-005"}, action_hashes={})
            with pytest.raises(ResumeRejected):
                await owner.resume(envelope)
            assert not owner.consumed and not owner.confirmations
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"order_id": "ORD-001", "product_state": "unopened"},
        {"order_id": "../order"},
        {"order_id": " "},
    ],
)
def test_customer_answer_fields_and_order_format_checked_before_consume(business, answers):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-MISSING-001", Settings())
        try:
            report = await owner.start()
            with pytest.raises((ResumeRejected, ValidationError)):
                await owner.resume(response_envelope(report["pending_input"], answers=answers))
            assert not owner.consumed and owner.ticket.input_revision == 1
        finally:
            owner.close()

    asyncio.run(scenario())


def test_refund_amount_revision_invalidates_old_approval_and_keeps_stable_action_id(business):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-NOTRECEIVED-002", Settings())
        try:
            before = dump(business)
            first = await owner.start()
            old = first["pending_input"]
            identifier = old["actions"][0]["action_id"]
            revised = await owner.resume(
                response_envelope(old, decision="revise", refund_amounts={identifier: 9000})
            )
            new = revised["pending_input"]
            assert revised["status"] == "paused" and revised["repair_count"] == 1
            assert new["pending_id"] != old["pending_id"]
            assert new["proposal_revision"] == old["proposal_revision"] + 1
            assert new["input_revision"] == old["input_revision"]
            assert new["actions"][0]["action_id"] == identifier
            assert new["actions"][0]["content_hash"] != old["actions"][0]["content_hash"]
            assert new["actions"][0]["candidate"]["amount_cents"] == 9000
            assert revised["accepted_proposal"] is None
            with pytest.raises(ResumeRejected):
                await owner.resume(response_envelope(old, decision="approve"))
            report = await owner.resume(response_envelope(new, decision="approve"))
            assert report["status"] == "completed"
            assert [c["decision"] for c in report["confirmations"]] == ["revise", "approve"]
            assert report["proposal"]["actions"][0]["amount_cents"] == 9000
            assert dump(business) == before
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("amount", [0, -1, 10001, 10000, 1.5, True, "9000"])
def test_invalid_refund_edit_not_consumed(business, amount):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-NOTRECEIVED-002", Settings())
        try:
            first = await owner.start()
            pending = first["pending_input"]
            identifier = pending["actions"][0]["action_id"]
            with pytest.raises((ResumeRejected, ValidationError)):
                await owner.resume(
                    response_envelope(
                        pending, decision="revise", refund_amounts={identifier: amount}
                    )
                )
            assert not owner.consumed and not owner.confirmations
        finally:
            owner.close()

    asyncio.run(scenario())


def test_concurrent_duplicate_resume_consumes_once(business):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        try:
            paused = await owner.start()
            envelope = response_envelope(paused["pending_input"], decision="approve")
            results = await asyncio.gather(
                owner.resume(envelope), owner.resume(envelope), return_exceptions=True
            )
            assert sum(isinstance(r, ResumeRejected) for r in results) == 1
            assert sum(isinstance(r, dict) and r["status"] == "completed" for r in results) == 1
            assert len(owner.confirmations) == len(owner.consumed) == 1
        finally:
            owner.close()

    asyncio.run(scenario())


def test_customer_statement_is_not_verified_fact_and_no_repeat_question(business, monkeypatch):
    original = business.get_order

    def missing_received(order_id, *, customer_id):
        order = original(order_id, customer_id=customer_id)
        return order.model_copy(update={"received_at": None})

    monkeypatch.setattr(business, "get_order", missing_received)

    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        try:
            paused = await owner.start()
            pending = paused["pending_input"]
            assert pending["kind"] == "customer_info"
            findings = paused["graph_state"]["order_findings"]
            report = await owner.resume(
                response_envelope(
                    pending,
                    answers={q["field"]: "我昨天收货，商品未拆封" for q in pending["questions"]},
                )
            )
            assert report["status"] == "handed_off"
            assert report["graph_state"]["reason"] == "ANSWER_STILL_UNVERIFIED"
            assert report["graph_state"]["order_findings"] == findings
            assert len(report["pending_history"]) == 1 and report["accepted_proposal"] is None
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["max_model_calls", "max_tool_calls"])
def test_shared_budget_failure_hands_off_without_authorization(business, budget):
    report = run(business, settings=Settings(**{budget: 1}))
    assert report["status"] == "handed_off" and report["accepted_proposal"] is None
    assert report["error"]["code"] == "CallLimitExceeded"
    assert report["statistics"]["model_calls" if budget == "max_model_calls" else "tool_calls"] == 1


def test_live_deferred_and_close_prevents_resume(business):
    report = run(business, mode="live")
    assert report["status"] == "skipped" and report["statistics"]["model_calls"] == 0
    assert report["events"][0]["kind"] == "run_started"

    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        paused = await owner.start()
        owner.close()
        with pytest.raises(ResumeRejected):
            await owner.resume(response_envelope(paused["pending_input"], decision="approve"))

    asyncio.run(scenario())


def test_review_tools_and_handoffs_have_independent_histories(business):
    report = run(business)
    calls = [e for e in report["events"] if e["kind"] == "tool_started"]
    allowlists = {
        Role.ORDER.value: ORDER_TOOLS,
        Role.POLICY.value: POLICY_TOOLS,
        Role.REVIEW.value: ("validate_evidence_refs", "get_evidence"),
        "validator": ("evaluate_policy",),
    }
    assert all(e["tool"] in allowlists[e["role"]] for e in calls)
    assert any(e["role"] == Role.REVIEW.value for e in calls)
    for agent_run in report["agent_runs"]:
        assert "messages" not in agent_run["input"]
    assert any(
        m.get("agent_role") == Role.REVIEW.value and m["type"] == "tool" for m in report["messages"]
    )


def test_saved_report_is_static_and_no_overwrite(business, tmp_path):
    report = run(business)
    target = tmp_path / "paused.json"
    save_run(report, target)
    assert REPORT_ADAPTER.validate_json(target.read_text()).status == "paused"
    with pytest.raises(FileExistsError):
        save_run(report, target)
    fresh = InMemoryReviewRun(business, report["ticket_id"], Settings())
    try:
        with pytest.raises(ResumeRejected):
            asyncio.run(
                fresh.resume(response_envelope(report["pending_input"], decision="approve"))
            )
    finally:
        fresh.close()


@pytest.mark.parametrize(
    "tamper",
    [
        "status",
        "proposal_hash",
        "content_hash",
        "proposal_revision",
        "candidate",
        "duplicate_history",
    ],
)
def test_saved_paused_report_internal_consistency(business, tamper):
    report = json.loads(json.dumps(run(business)))
    if tamper == "status":
        report["status"] = "completed"
    elif tamper == "duplicate_history":
        report["pending_history"].append(report["pending_history"][0])
    elif tamper == "candidate":
        report["pending_input"]["actions"][0]["candidate"]["order_id"] = "ORD-OTHER"
    elif tamper == "content_hash":
        report["pending_input"]["actions"][0]["content_hash"] = "0" * 64
    else:
        report["pending_input"][tamper] = 2 if tamper == "proposal_revision" else "0" * 64
    with pytest.raises(ValidationError):
        REPORT_ADAPTER.validate_python(report)


def test_resume_model_instance_is_revalidated_before_use(business):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-MISSING-001", Settings())
        try:
            paused = await owner.start()
            request = ResumeInput.model_construct(
                **response_envelope(paused["pending_input"], answers={"order_id": ""})
            )
            with pytest.raises(ValidationError):
                await owner.resume(request)
            assert not owner.consumed
        finally:
            owner.close()

    asyncio.run(scenario())


def test_unknown_declared_intent_hands_off_without_business_reads(business, monkeypatch):
    original = business.get_ticket
    monkeypatch.setattr(
        business,
        "get_ticket",
        lambda identifier: original(identifier).model_copy(update={"type": TicketType.UNKNOWN}),
    )
    report = run(business)
    assert report["status"] == "handed_off" and report["error"] is None
    assert report["statistics"]["tool_calls"] == 0
    assert report["node_trace"] == ["intake", "draft", "validate", "review", "handoff"]


def test_order_read_recovers_from_unavailable_before_other_reads(business, monkeypatch):
    original = business.get_order
    calls = 0

    def flaky(identifier, *, customer_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("first order query failed")
        return original(identifier, customer_id=customer_id)

    monkeypatch.setattr(business, "get_order", flaky)
    report = run(business)
    assert report["status"] == "paused" and report["repair_count"] == 1
    first = next(r for r in report["agent_runs"] if r["stage"] == "order")
    assert first["tool_calls"] == 1 and first["output"]["outcome"] == "unavailable"
    research = next(r for r in report["agent_runs"] if r["stage"] == "research")
    assert research["input"]["tools"] == list(ORDER_TOOLS)
    assert research["output"]["outcome"] == "ready"
    assert report["graph_state"]["policy_assessment"] is not None


def test_policy_query_retry_does_not_repeat_order_reads(business, monkeypatch):
    original = business.list_policies
    calls = 0

    def flaky(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("first policy read failed")
        return original(**kwargs)

    monkeypatch.setattr(business, "list_policies", flaky)
    report = run(business)
    assert report["status"] == "paused" and report["repair_count"] == 1
    assert sum(r["role"] == Role.ORDER.value for r in report["agent_runs"]) == 1
    assert sum(r["role"] == Role.POLICY.value for r in report["agent_runs"]) == 2


def test_over_balance_refund_cannot_be_authorized_by_accepting_reviewer(business):
    def change(args, count):
        args["actions"][0]["amount_cents"] = 10001

    def accept(args, count):
        args.clear()
        args["outcome"] = "accept"

    report = run(
        business,
        "T-NOTRECEIVED-002",
        settings=settings(),
        model_factory=mutate_factory(draft_change=change, review_change=accept),
    )
    assert report["status"] == "handed_off" and report["repair_count"] == 2
    assert not report["validation"]["ok"] and report["pending_input"] is None
    assert report["accepted_proposal"] is None and not report["confirmations"]


def test_ignoring_operator_amount_edit_cannot_restore_old_amount(business):
    def change(args, count):
        args["actions"][0]["amount_cents"] = 10000

    async def scenario():
        owner = InMemoryReviewRun(
            business,
            "T-NOTRECEIVED-002",
            settings(),
            model_factory=mutate_factory(draft_change=change),
        )
        try:
            report = await owner.start()
            pending = report["pending_input"]
            identifier = pending["actions"][0]["action_id"]
            report = await owner.resume(
                response_envelope(pending, decision="revise", refund_amounts={identifier: 9000})
            )
            assert report["status"] == "handed_off" and report["repair_count"] == 2
            assert report["accepted_proposal"] is None
            assert len(report["pending_history"]) == 1 and not any(
                c["decision"] == "approve" for c in report["confirmations"]
            )
        finally:
            owner.close()

    asyncio.run(scenario())


def test_repair_limit_zero_hands_off_on_first_feedback(business):
    def change(args, count):
        args["customer_reply_draft"] = "已经完成退款"

    report = run(
        business,
        settings=Settings(review_repair_limit=0),
        model_factory=mutate_factory(draft_change=change),
    )
    assert report["status"] == "handed_off" and report["repair_count"] == 0
    assert sum(r["stage"] == "draft" for r in report["agent_runs"]) == 1


def test_waiting_for_operator_exhaustion_blocks_edits_but_allows_reject(business):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-NOTRECEIVED-002", Settings(review_repair_limit=0))
        try:
            paused = await owner.start()
            pending = paused["pending_input"]
            identifier = pending["actions"][0]["action_id"]
            with pytest.raises(ResumeRejected):
                await owner.resume(
                    response_envelope(pending, decision="revise", refund_amounts={identifier: 9000})
                )
            assert not owner.consumed
            assert (await owner.resume(response_envelope(pending, decision="reject")))[
                "status"
            ] == "handed_off"
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tamper", ["missing_approval", "wrong_actor", "old_version", "hash", "duplicate", "raw_changed"]
)
def test_completed_report_requires_consistent_current_approval(business, tamper):
    async def scenario():
        owner = InMemoryReviewRun(business, "T-RETURN-001", Settings())
        try:
            paused = await owner.start()
            return await owner.resume(
                response_envelope(paused["pending_input"], decision="approve")
            )
        finally:
            owner.close()

    report = asyncio.run(scenario())
    if tamper == "missing_approval":
        report["confirmations"] = []
    elif tamper == "wrong_actor":
        report["confirmations"][0]["actor_id"] = "OP-WRONG"
    elif tamper == "old_version":
        report["confirmations"][0]["proposal_revision"] += 1
    elif tamper == "hash":
        report["confirmations"][0]["proposal_hash"] = "0" * 64
    elif tamper == "duplicate":
        report["confirmations"].append(copy.deepcopy(report["confirmations"][0]))
    else:
        report["proposal"]["customer_reply_draft"] = "different contents"
    with pytest.raises(ValidationError):
        REPORT_ADAPTER.validate_python(report)
