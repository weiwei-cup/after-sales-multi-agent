import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from after_sales.agents.bounded import error_category
from after_sales.agents.scripted import ScriptError
from after_sales.agents.telemetry import CallLimitExceeded, SchemaRepairExceeded
from after_sales.config import Settings
from after_sales.repositories.budgets import BudgetStore, TokenBudgetExceeded
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect, transaction
from after_sales.tools.contracts import ErrorCode, ToolResult, failed
from after_sales.workflows.parallel import ParallelReviewRun

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


async def create(storage, **limits):
    repo, settings = storage
    settings = Settings(**{**settings.model_dump(), **limits})
    return await ParallelReviewRun.create(repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW)


def response(tokens=None):
    usage = (
        None
        if tokens is None
        else {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}
    )
    return SimpleNamespace(result=[AIMessage(content="offline", usage_metadata=usage)])


def test_last_model_slot_is_atomic_across_competing_owners(storage):
    async def scenario():
        run = await create(storage, max_model_calls=1)
        try:
            barrier = Barrier(2)

            def reserve(index):
                barrier.wait(timeout=5)
                try:
                    return BudgetStore(run.repository.path, run.run_id, run.settings).reserve(
                        "model", str(index)
                    )
                except CallLimitExceeded:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(reserve, range(2)))
            assert sum(r is not None for r in results) == 1
            assert run.trace.statistics()["model_calls"] == 1
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_shared_slots_bound_actual_operations_and_count_waiting_reservations(storage):
    async def scenario():
        run = await create(storage, max_concurrency=2)
        try:
            entered = 0
            two = asyncio.Event()
            release = asyncio.Event()

            async def operation():
                nonlocal entered
                entered += 1
                if entered == 2:
                    two.set()
                await release.wait()
                return ToolResult(ok=True, data={"read": True})

            calls = [
                asyncio.create_task(
                    run.trace.tool("read", str(i), operation, role="order_specialist")
                )
                for i in range(5)
            ]
            await asyncio.wait_for(two.wait(), 5)
            assert entered == run.trace.in_flight == 2
            assert len(run.trace.ledger.snapshot()["calls"]) == 5
            release.set()
            await asyncio.wait_for(asyncio.gather(*calls), 5)
            stats = run.trace.statistics()
            assert stats["tool_calls"] == 5 and stats["peak_in_flight"] == 2
            assert all(c["status"] == "succeeded" for c in stats["reservations"])
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_usage_reconciliation_retains_unknown_holds_and_unknown_price(storage):
    async def scenario():
        run = await create(storage)
        try:

            async def known():
                return response(17)

            async def unknown():
                return response()

            await run.trace.model(known, role="coordinator")
            await run.trace.model(unknown, role="order_specialist")
            stats = run.trace.statistics()
            assert stats["known_total_tokens"] == 17
            assert stats["actual_total_tokens"] is None and stats["monetary_cost"] is None
            assert stats["unknown_usage_calls"] == 1
            assert stats["token_budget_charged"] == 17 + run.settings.model_token_reservation
            assert stats["token_usage_note"] == "partially_reported"
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_unknown_usage_exhausts_reservations_without_extra_call(storage):
    async def scenario():
        run = await create(storage, token_budget=2048)
        try:

            async def unknown():
                return response()

            await run.trace.model(unknown, role="coordinator")
            with pytest.raises(TokenBudgetExceeded):
                await run.trace.model(unknown, role="order_specialist")
            assert run.trace.statistics()["model_calls"] == 1
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_reported_usage_over_reservation_blocks_next_attempt(storage):
    async def scenario():
        run = await create(storage, token_budget=2048)
        try:

            async def large():
                return response(3000)

            await run.trace.model(large, role="coordinator")
            with pytest.raises(TokenBudgetExceeded):
                run.trace.reserve("tool", "validator")
            assert run.trace.statistics()["actual_total_tokens"] == 3000
        finally:
            await run.aclose()

    asyncio.run(scenario())


class ProviderError(Exception):
    def __init__(self, code):
        self.status_code = code


@pytest.mark.parametrize(
    "error,category,attempts",
    [
        (ProviderError(429), "transient", 2),
        (TimeoutError(), "transient", 2),
        (ProviderError(503), "transient", 2),
        (ProviderError(403), "business", 1),
        (ProviderError(401), "business", 1),
        (ValueError("bug"), "program", 1),
        (ScriptError("contract"), "model_contract", 1),
    ],
)
def test_finite_model_retries_charge_every_attempt(storage, error, category, attempts):
    async def scenario():
        run = await create(storage, retry_backoff_seconds=0)
        try:
            invoked = 0

            async def fail():
                nonlocal invoked
                invoked += 1
                raise error

            with pytest.raises(type(error)):
                await run.trace.model(fail, role="policy_specialist")
            assert invoked == run.trace.statistics()["model_calls"] == attempts
            assert error_category(error) == category
            assert all(c["status"] == "failed" for c in run.trace.ledger.snapshot()["calls"])
        finally:
            await run.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,retryable,attempts",
    [
        (ErrorCode.TOOL_TIMEOUT, True, 2),
        (ErrorCode.TOOL_BUSY, True, 2),
        (ErrorCode.NOT_OWNED, False, 1),
        (ErrorCode.INVALID_ARGUMENT, False, 1),
        (ErrorCode.QUERY_FAILED, True, 1),
    ],
)
def test_tool_failure_classification_retries_only_known_transients(
    storage, code, retryable, attempts
):
    async def scenario():
        run = await create(storage, retry_backoff_seconds=0)
        try:
            invoked = 0

            async def fail():
                nonlocal invoked
                invoked += 1
                return failed(code, "controlled failure", retryable=retryable)

            result = await run.trace.tool("read", "test", fail, role="order_specialist")
            assert not result.ok
            assert invoked == run.trace.statistics()["tool_calls"] == attempts
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_retry_succeeds_once_and_backoff_is_visible(storage):
    async def scenario():
        run = await create(storage, retry_backoff_seconds=0)
        try:
            invoked = 0

            async def recover():
                nonlocal invoked
                invoked += 1
                if invoked == 1:
                    raise ProviderError(429)
                return response(4)

            await run.trace.model(recover, role="coordinator")
            assert run.trace.statistics()["model_calls"] == 2
            assert any(e["kind"] == "retry_scheduled" for e in run.trace.events)
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_stale_runtime_counters_cannot_refund_budget_or_schema_repairs(storage):
    async def scenario():
        run = await create(storage, max_model_calls=1, proposal_repair_limit=1)
        repo, settings = run.repository, run.settings
        try:
            run.trace.reserve("model", "coordinator")
            run.trace.repair_schema(ScriptError("bad"), role="coordinator")
            run_id = run.run_id
            with transaction(repo.path) as db:
                db.execute(
                    "UPDATE workflow_runtime SET runtime_json=json_set(runtime_json,"
                    "'$.trace.model_calls',0,'$.trace.schema_repairs',0,"
                    "'$.trace.events',json('[]')) WHERE run_id=?",
                    (run_id,),
                )
        finally:
            await run.aclose()
        run = await ParallelReviewRun.load(repo, run_id, settings, clock=lambda: NOW)
        try:
            assert run.trace.model_calls == run.trace.schema_repairs == 1
            with pytest.raises(CallLimitExceeded):
                run.trace.reserve("model", "coordinator")
            with pytest.raises(SchemaRepairExceeded):
                run.trace.repair_schema(ScriptError("bad"), role="coordinator")
            assert [e["sequence"] for e in run.trace.events] == list(
                range(1, len(run.trace.events) + 1)
            )
            with connect(repo.path, readonly=True) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM call_reservations WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                    == 1
                )
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_active_deadline_interrupts_wait_and_charges_the_attempt(storage):
    async def scenario():
        run = await create(storage, active_time_budget_seconds=1)
        try:
            run.trace.ledger.begin()

            async def slow():
                await asyncio.Event().wait()

            with pytest.raises(CallLimitExceeded):
                await asyncio.wait_for(run.trace.model(slow, role="coordinator"), 4)
            assert run.trace.statistics()["model_calls"] == 1
            assert run.trace.ledger.snapshot()["calls"][0]["status"] == "failed"
        finally:
            run.trace.ledger.end()
            await run.aclose()

    asyncio.run(scenario())


def test_review_repair_ledger_prevents_stale_checkpoint_from_regaining_edit(storage):
    from after_sales.workflows.reviewed import ResumeRejected, response_envelope

    async def scenario():
        run = await create(storage, review_repair_limit=1)
        try:
            paused = await run.start()
            pending = paused["pending_input"]
            assert run.trace.ledger.reserve_review_repair() == 1
            assert run.state["repair_count"] == 0  # Simulate death before the graph repair update.
            amount = {pending["actions"][0]["action_id"]: 9000}
            with pytest.raises(ResumeRejected, match="durable"):
                await run.resume(
                    response_envelope(pending, decision="revise", refund_amounts=amount)
                )
            assert run.trace.ledger.reserve_review_repair() is None
            assert not run.store.confirmations()
            assert run.trace.statistics()["review_repairs_reserved"] == 1
        finally:
            await run.aclose()

    asyncio.run(scenario())
