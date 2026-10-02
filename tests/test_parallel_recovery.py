import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect, transaction
from after_sales.workflows.durable import PersistentReviewRun
from after_sales.workflows.parallel import ParallelReviewRun
from after_sales.workflows.reviewed import response_envelope

pytestmark = [pytest.mark.integration, pytest.mark.recovery]
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)
WORKER = Path(__file__).parent / "support" / "parallel_worker.py"


@pytest.fixture
def storage(tmp_path):
    settings = Settings(
        business_db_path=tmp_path / "business.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
    )
    seed_demo(settings.business_db_path)
    return BusinessRepository(settings.business_db_path), settings


def child(storage, operation, *, response=None, crash=None, max_model_calls=20):
    root = storage[1].business_db_path.parent
    command = [
        sys.executable,
        str(WORKER),
        str(root),
        operation,
        "--max-model-calls",
        str(max_model_calls),
    ]
    if response:
        path = root / "response.json"
        path.write_text(json.dumps(response))
        command.extend(["--response", str(path)])
    if crash:
        command.extend(["--crash", crash])
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    assert result.returncode == (86 if crash else 0), result.stderr or result.stdout
    return None if crash else json.loads(result.stdout)


def test_process_crash_after_reservation_does_not_regain_last_model_slot(storage):
    child(storage, "start", crash="after_call_reservation", max_model_calls=1)
    report = child(storage, "resume")  # Saved max=1 wins over this process's max=20.
    assert report["status"] == "handed_off"
    assert report["statistics"]["model_calls"] == 1
    assert report["statistics"]["reservations"][0]["status"] == "reserved"
    assert report["statistics"]["unknown_usage_calls"] == 1
    assert not report["executed_actions"]


def test_fanout_join_checkpoint_recovery_keeps_successful_tasks_without_rerunning(storage):
    child(storage, "start", crash="after_node:join")
    with connect(storage[0].path, readonly=True) as db:
        reserved_before = db.execute(
            "SELECT count(*) FROM call_reservations WHERE kind='model'"
        ).fetchone()[0]
        assert reserved_before == 9  # intake 1, order 6, candidate 2
    report = child(storage, "resume")
    assert report["status"] == "paused", report["error"]
    assert len(report["graph_state"]["task_results"]) == 2
    assert sum(e["kind"] == "branch_started" for e in report["events"]) == 2
    assert report["statistics"]["model_calls"] > reserved_before


def test_process_crash_after_business_commit_replays_receipt_without_new_attempt(storage):
    paused = child(storage, "start")
    answer = response_envelope(paused["pending_input"], decision="approve")
    child(storage, "resume", response=answer, crash="after_commit")
    with connect(storage[0].path, readonly=True) as db:
        attempts = db.execute("SELECT count(*) FROM call_reservations").fetchone()[0]
    report = child(storage, "resume")
    assert report["status"] == "completed", report["error"]
    assert len(report["executed_actions"]) == 1
    assert sum(e["kind"] == "action_replayed" for e in report["events"]) == 1
    assert report["statistics"]["model_calls"] + report["statistics"]["tool_calls"] == attempts
    assert storage[0].get_order("ORD-004", customer_id="CUST-A").refunded_cents == 10000


def test_another_process_can_request_cancel_while_owner_holds_run_lock(storage):
    async def scenario():
        run = await ParallelReviewRun.create(
            storage[0], "T-NOTRECEIVED-002", storage[1], clock=lambda: NOW, run_id="parallel-worker"
        )
        try:
            report = await run.start()
            assert report["status"] == "paused"
            cancelled = child(storage, "cancel")
            assert cancelled["cancel_requested"]
            report = await run.resume(
                response_envelope(report["pending_input"], decision="approve")
            )
            assert report["status"] == "cancelled"
            assert not report["executed_actions"]
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_paused_wall_time_does_not_count_and_saved_limits_win(storage):
    async def scenario():
        repo, settings = storage
        run = await ParallelReviewRun.create(repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW)
        try:
            assert (await run.start())["status"] == "paused"
            run_id = run.run_id
        finally:
            await run.aclose()
        with transaction(repo.path) as db:
            db.execute(
                "UPDATE run_budget SET active_ms=123,segment_started=NULL WHERE run_id=?", (run_id,)
            )
        run = await ParallelReviewRun.load(
            repo,
            run_id,
            Settings(**{**settings.model_dump(), "max_concurrency": 99, "token_budget": 999999}),
            clock=lambda: NOW,
        )
        try:
            assert run.settings.max_concurrency == 2 and run.settings.token_budget == 50000
            assert run.report()["statistics"]["active_elapsed_ms"] == 123
            assert (await run.recover())["statistics"]["active_elapsed_ms"] == 123
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_crashed_open_segment_is_conservatively_charged_and_exhaustion_handoffs(storage):
    async def scenario():
        repo, settings = storage
        run = await ParallelReviewRun.create(repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW)
        try:
            run_id = run.run_id
        finally:
            await run.aclose()
        with transaction(repo.path) as db:
            db.execute(
                "UPDATE run_budget SET segment_started=? WHERE run_id=?",
                ((datetime.now(UTC) - timedelta(seconds=301)).isoformat(), run_id),
            )
        run = await ParallelReviewRun.load(repo, run_id, settings, clock=lambda: NOW)
        try:
            report = await run.recover()
            assert report["status"] == "handed_off"
            assert report["error"]["code"] == "ActiveTimeExceeded"
            assert report["statistics"]["model_calls"] == 0
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_business_v2_to_v3_preserves_a_p06_pending_run(tmp_path, monkeypatch):
    from after_sales.repositories.migrations import MIGRATIONS

    async def scenario():
        settings = Settings(
            business_db_path=tmp_path / "business.sqlite",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite",
        )
        with monkeypatch.context() as patch:
            patch.delitem(MIGRATIONS, 3)
            seed_demo(settings.business_db_path)
            repo = BusinessRepository(settings.business_db_path)
            run = await PersistentReviewRun.create(
                repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW
            )
            try:
                paused = await run.start()
                run_id = run.run_id
            finally:
                await run.aclose()
        seed_demo(settings.business_db_path)
        run = await PersistentReviewRun.load(repo, run_id, settings, clock=lambda: NOW)
        try:
            assert run.report()["pending_input"] == paused["pending_input"]
            completed = await run.resume(
                response_envelope(paused["pending_input"], decision="approve")
            )
            assert completed["phase"] == "P06" and completed["status"] == "completed"
            assert len(completed["executed_actions"]) == 1
        finally:
            await run.aclose()

    asyncio.run(scenario())
