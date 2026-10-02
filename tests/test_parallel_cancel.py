import asyncio
from datetime import UTC, datetime

import pytest

from after_sales.agents.contracts import ActionBinding, ResumeInput
from after_sales.config import Settings
from after_sales.repositories.budgets import RunCancelled
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect
from after_sales.workflows.parallel import ParallelReviewRun, request_cancel
from after_sales.workflows.reviewed import ResumeRejected, response_envelope

pytestmark = [pytest.mark.integration, pytest.mark.recovery]
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path):
    settings = Settings(
        business_db_path=tmp_path / "business.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
    )
    seed_demo(settings.business_db_path)
    return BusinessRepository(settings.business_db_path), settings


async def create(storage, **kwargs):
    repo, settings = storage
    return await ParallelReviewRun.create(
        repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW, **kwargs
    )


def counts(repo):
    with connect(repo.path, readonly=True) as db:
        return {
            name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("human_inputs", "action_ledger", "action_events")
        }


def test_cancel_queued_run_makes_no_model_or_tool_call(storage):
    async def scenario():
        run = await create(storage)
        try:
            first = request_cancel(storage[0], run.run_id, storage[1])
            assert first["cancel_requested"]
            assert not request_cancel(storage[0], run.run_id, storage[1])["new_request"]
            report = await run.start()
            assert report["status"] == "cancelled"
            assert report["statistics"]["model_calls"] == report["statistics"]["tool_calls"] == 0
            assert not report["executed_actions"] and counts(storage[0])["action_ledger"] == 0
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_cancel_paused_run_is_visible_after_reopen_and_cannot_approve(storage):
    async def scenario():
        run = await create(storage)
        try:
            paused = await run.start()
            answer = response_envelope(paused["pending_input"], decision="approve")
            run_id = run.run_id
            calls = paused["statistics"]["model_calls"]
            request_cancel(storage[0], run_id, storage[1])
        finally:
            await run.aclose()
        run = await ParallelReviewRun.load(storage[0], run_id, storage[1], clock=lambda: NOW)
        try:
            assert run.report()["status"] == "cancelled"
            with pytest.raises(ResumeRejected):
                await run.resume(answer)
            assert run.report()["statistics"]["model_calls"] == calls
            assert counts(storage[0]) == {"human_inputs": 0, "action_ledger": 0, "action_events": 0}
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_cancel_running_branch_stops_before_next_node(storage, monkeypatch):
    from after_sales.tools.service import ToolSession

    original = ToolSession.call

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def controlled(self, name, arguments):
            if name == "get_order":
                entered.set()
                await release.wait()
            return await original(self, name, arguments)

        monkeypatch.setattr(ToolSession, "call", controlled)
        run = await create(storage)
        try:
            task = asyncio.create_task(run.start())
            await asyncio.wait_for(entered.wait(), 5)
            request_cancel(storage[0], run.run_id, storage[1])
            release.set()
            report = await asyncio.wait_for(task, 10)
            assert report["status"] == "cancelled"
            assert not report["executed_actions"]
            assert not any(e["kind"] == "action_committed" for e in report["events"])
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_cancel_checked_inside_write_transaction_before_effect(storage):
    async def scenario():
        run = await create(storage)
        try:
            paused = await run.start()
            pending = paused["pending_input"]
            answer = response_envelope(pending, decision="approve")
            run.store.accept(ResumeInput.model_validate(answer), run.ticket, NOW)
            request_cancel(storage[0], run.run_id, storage[1])
            with pytest.raises(RunCancelled):
                run.actions.execute(
                    run.run_id,
                    pending["pending_id"],
                    ActionBinding.model_validate(pending["actions"][0]),
                    clock=lambda: NOW,
                )
            assert counts(storage[0])["action_ledger"] == 0
            assert storage[0].get_order("ORD-004", customer_id="CUST-A").refunded_cents == 0
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_cancellation_after_commit_preserves_completed_receipt_and_balance(storage):
    async def scenario():
        run = None

        def fault(stage):
            if stage == "after_commit":
                request_cancel(storage[0], run.run_id, storage[1])

        run = await create(storage, fault=fault)
        try:
            paused = await run.start()
            pending = paused["pending_input"]
            report = await run.resume(response_envelope(pending, decision="approve"))
            assert report["status"] == "cancelled", report["error"]
            assert not report["candidate_only"] and len(report["executed_actions"]) == 1
            assert report["business_status"] == "resolved"
            assert storage[0].get_order("ORD-004", customer_id="CUST-A").refunded_cents == 10000
            receipt = run.actions.execute(
                run.run_id,
                pending["pending_id"],
                ActionBinding.model_validate(pending["actions"][0]),
                clock=lambda: NOW,
            )
            assert receipt.model_dump(mode="json") == report["executed_actions"][0]
            assert counts(storage[0]) == {"human_inputs": 1, "action_ledger": 1, "action_events": 1}
            run_id = run.run_id
        finally:
            await run.aclose()
        run = await ParallelReviewRun.load(storage[0], run_id, storage[1], clock=lambda: NOW)
        try:
            recovered = await run.recover()
            assert (
                recovered["status"] == "cancelled"
                and recovered["executed_actions"] == report["executed_actions"]
            )
            assert recovered["business_status"] == "resolved"
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_cancel_completed_run_is_noop(storage):
    async def scenario():
        run = await create(storage)
        try:
            report = await run.start()
            report = await run.resume(
                response_envelope(report["pending_input"], decision="approve")
            )
            assert report["status"] == "completed"
            assert not request_cancel(storage[0], run.run_id, storage[1])["new_request"]
            assert run.report()["status"] == "completed"
        finally:
            await run.aclose()

    asyncio.run(scenario())
