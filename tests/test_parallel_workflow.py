import asyncio
from datetime import UTC, datetime

import pytest

from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.workflows.parallel import ParallelReviewRun
from after_sales.workflows.reviewed import response_envelope

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path):
    settings = Settings(
        business_db_path=tmp_path / "business.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
    )
    seed_demo(settings.business_db_path)
    return BusinessRepository(settings.business_db_path), settings


def test_parallel_refund_resume(storage):
    async def scenario():
        repo, settings = storage
        run = await ParallelReviewRun.create(repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW)
        try:
            report = await run.start()
            assert report["status"] == "paused", report["error"]
            assert len(report["graph_state"]["task_results"]) == 2
            assert report["statistics"]["actual_total_tokens"] is None
            assert report["statistics"]["monetary_cost"] is None
            envelope = response_envelope(report["pending_input"], decision="approve")
            run_id = run.run_id
            calls = report["statistics"]["model_calls"]
        finally:
            await run.aclose()
        run = await ParallelReviewRun.load(repo, run_id, settings, clock=lambda: NOW)
        try:
            report = await run.resume(envelope)
            assert report["status"] == "completed", report["error"]
            assert report["business_status"] == "resolved"
            assert len(report["executed_actions"]) == 1
            assert report["statistics"]["model_calls"] == calls
        finally:
            await run.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ticket,status",
    [
        ("T-RETURN-001", "paused"),
        ("T-DELAY-001", "completed"),
        ("T-DELAY-002", "paused"),
        ("T-MISSING-001", "paused"),
        ("T-CROSS-001", "handed_off"),
        ("T-CONFLICT-001", "handed_off"),
        ("T-RETURN-002", "completed"),
        ("T-DUPLICATE-001", "paused"),
    ],
)
def test_finite_parallel_routes_preserve_business_rules(storage, ticket, status):
    async def scenario():
        repo, settings = storage
        run = await ParallelReviewRun.create(repo, ticket, settings, clock=lambda: NOW)
        try:
            report = await run.start()
            assert report["status"] == status, report["error"] or report["proposal"]
            assert not report["executed_actions"]
            if report["graph_state"]["policy_candidates"]:
                assert (
                    report["graph_state"]["policy_candidates"]["applicability"] == "not_evaluated"
                )
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_actual_tool_calls_overlap_and_applicability_waits_for_both(storage, monkeypatch):
    from after_sales.tools.service import ToolSession

    original = ToolSession.call

    async def scenario():
        arrived = set()
        barrier = asyncio.Event()

        async def controlled(self, name, arguments):
            if name in {"get_order", "search_policy_candidates"} and len(arrived) < 2:
                arrived.add(name)
                if len(arrived) == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 5)
            return await original(self, name, arguments)

        monkeypatch.setattr(ToolSession, "call", controlled)
        repo, settings = storage
        run = await ParallelReviewRun.create(repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW)
        try:
            report = await asyncio.wait_for(run.start(), 15)
            assert report["status"] == "paused", report["error"]
            assert arrived == {"get_order", "search_policy_candidates"}
            finished = [e["sequence"] for e in report["events"] if e["kind"] == "branch_finished"]
            assessment = [
                e["sequence"]
                for e in report["events"]
                if e["kind"] == "tool_started" and e["tool"] == "evaluate_policy"
            ]
            assert max(finished) < min(assessment)
            assert report["statistics"]["peak_in_flight"] == 2
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_failed_branch_retains_successful_order_evidence(storage):
    from after_sales.workflows.parallel import parallel_model

    def factory(role, stage, payload):
        if stage == "policy_candidates":
            raise ValueError("controlled program failure")
        return parallel_model(role, stage, payload)

    async def scenario():
        repo, settings = storage
        run = await ParallelReviewRun.create(
            repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW, model_factory=factory
        )
        try:
            report = await run.start()
            assert report["status"] == "handed_off"
            tasks = {r["kind"]: r for r in report["graph_state"]["task_results"]}
            assert tasks["order"]["status"] == "completed"
            assert tasks["policy_candidates"]["status"] == "failed"
            assert len(report["graph_state"]["order_findings"]["facts"]) == 5
            assert any(e["source_type"] == "order" for e in report["evidence"])
            assert not report["executed_actions"]
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_candidate_discovery_reads_no_order_and_does_not_assert_eligibility(storage, monkeypatch):
    from after_sales.tools.service import ToolSession

    repo, _ = storage

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate discovery must not read any order")

    monkeypatch.setattr(repo, "get_order", forbidden)
    session = ToolSession.for_ticket(repo, "T-CROSS-001")
    try:
        result = asyncio.run(
            session.call("search_policy_candidates", {"intent": session.context.intent.value})
        )
        assert result.ok and "policies" in result.data
        assert all(e["source_type"] == "policy" for e in session.evidence.export(session.context))
    finally:
        session.close()


def test_candidate_schema_repair_consumes_another_model_slot(storage):
    from dataclasses import replace

    from after_sales.agents.scripted import ScriptedChatModel
    from after_sales.workflows.parallel import parallel_model

    def factory(role, stage, payload):
        model = parallel_model(role, stage, payload)
        if stage != "policy_candidates":
            return model
        steps = list(model.steps)
        original = steps[1].response

        def invalid(messages):
            message = original(messages)
            tool = message.tool_calls[0]
            return message.model_copy(
                update={
                    "tool_calls": [{**tool, "args": {**tool["args"], "applicability": "eligible"}}]
                }
            )

        steps[1] = replace(steps[1], response=invalid)
        return ScriptedChatModel(steps=tuple(steps), script_id=model.script_id)

    async def scenario():
        run = await ParallelReviewRun.create(
            storage[0], "T-NOTRECEIVED-002", storage[1], clock=lambda: NOW, model_factory=factory
        )
        try:
            report = await run.start()
            assert report["status"] == "paused", (
                report["error"] or report["graph_state"]["task_results"]
            )
            assert report["statistics"]["schema_repairs"] == 1
            candidates = next(r for r in report["agent_runs"] if r["stage"] == "policy_candidates")
            assert candidates["model_calls"] == 3
            assert report["graph_state"]["policy_candidates"]["applicability"] == "not_evaluated"
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_conflicting_evidence_at_join_handoffs_without_selecting_a_version(storage, monkeypatch):
    from after_sales.workflows.parallel import ParallelRuntime

    original = ParallelRuntime.branch

    async def conflicting(self, state, config, kind):
        update = await original(self, state, config, kind)
        if kind == "order":
            item = next(e for e in update["evidence_join"]["items"] if e["source_type"] == "order")
            update["evidence_join"]["items"].append(
                {**item, "id": "E-conflicting-test", "source_version": "999"}
            )
        return update

    monkeypatch.setattr(ParallelRuntime, "branch", conflicting)

    async def scenario():
        run = await ParallelReviewRun.create(
            storage[0], "T-NOTRECEIVED-002", storage[1], clock=lambda: NOW
        )
        try:
            report = await run.start()
            assert report["status"] == "handed_off"
            assert report["graph_state"]["evidence_join"]["conflicts"]
            assert not report["executed_actions"]
            assert not any(
                e["kind"] == "tool_started" and e["tool"] == "evaluate_policy"
                for e in report["events"]
            )
        finally:
            await run.aclose()

    asyncio.run(scenario())
