import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import CUSTOMER, NOW, answer, post, start, wait_run
from test_api import storage as storage

from after_sales.api.app import DEMO_IDENTITIES, create_app
from after_sales.api.contracts import StartRun
from after_sales.repositories.sqlite import read_database
from after_sales.services.application import ApplicationService

pytestmark = [pytest.mark.api, pytest.mark.integration, pytest.mark.recovery]
WORKER = Path(__file__).parent / "support" / "api_worker.py"


def child(storage, operation, *, run_id=None, response=None, crash=None):
    command = [sys.executable, str(WORKER), str(storage[0].path.parent), operation]
    if run_id:
        command.extend(["--run", run_id])
    if response:
        path = storage[0].path.parent / "response.json"
        path.write_text(json.dumps(response))
        command.extend(["--response", str(path)])
    if crash:
        command.extend(["--crash", crash])
    result = subprocess.run(command, capture_output=True, text=True, timeout=35)
    assert result.returncode == (86 if crash else 0), result.stderr or result.stdout
    return None if crash else json.loads(result.stdout)


def test_real_exit_after_queued_admission_recovers_and_reuses_http_receipt(storage):
    child(storage, "start", crash="after_admission")
    with read_database(storage[0].path) as db:
        row = db.execute("SELECT id,status FROM runs").fetchone()
        assert row[1] == "queued"
        assert db.execute("SELECT status FROM execution_jobs").fetchone()[0] == "queued"
        assert db.execute("SELECT count(*) FROM request_idempotency").fetchone()[0] == 1
    assert not storage[1].checkpoint_db_path.exists()
    service = ApplicationService(*storage, clock=lambda: NOW)
    with TestClient(create_app(storage[1], service=service)) as client:
        result = wait_run(client, row[0], "paused")
        assert result["pending_input"]["kind"] == "operator_decision"
        assert start(client, key="process-request") == row[0]
        with read_database(storage[0].path) as db:
            assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_real_running_exit_marks_interrupted_then_explicit_resume(storage):
    child(storage, "start", crash="after_node:intake")
    with read_database(storage[0].path) as db:
        run_id = db.execute("SELECT id FROM runs").fetchone()[0]
        assert db.execute("SELECT status FROM execution_jobs").fetchone()[0] == "running"
    service = ApplicationService(*storage, clock=lambda: NOW)
    with TestClient(create_app(storage[1], service=service)) as client:
        recovered = client.get(f"/runs/{run_id}", headers=CUSTOMER).json()
        assert recovered["status"] == "interrupted" and recovered["pending_input"] is None
        response = post(client, f"/runs/{run_id}/resume", key="resume")
        assert response.status_code == 202, response.text
        assert post(client, f"/runs/{run_id}/resume", key="resume").json() == response.json()
        assert wait_run(client, run_id, "paused")["pending_input"]


def test_paused_restart_preserves_pending_and_acceptance_exit_replays_once(storage):
    paused = child(storage, "start")
    run_id, pending = paused["run_id"], paused["pending_input"]
    service = ApplicationService(*storage, clock=lambda: NOW)
    with TestClient(create_app(storage[1], service=service)) as client:
        assert client.get(f"/runs/{run_id}", headers=CUSTOMER).json()["pending_input"] == pending
    body = answer(pending, decision="approve")
    child(storage, "respond", run_id=run_id, response=body, crash="after_admission")
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM human_inputs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 0
    service = ApplicationService(*storage, clock=lambda: NOW)
    with TestClient(create_app(storage[1], service=service)) as client:
        completed = wait_run(client, run_id, "completed", pending=False)
        assert len(completed["result"]["receipts"]) == 1
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM human_inputs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 1


def test_real_exit_after_effect_commit_preserves_balance_and_receipt(storage):
    paused = child(storage, "start")
    run_id = paused["run_id"]
    child(
        storage,
        "respond",
        run_id=run_id,
        response=answer(paused["pending_input"], decision="approve"),
        crash="after_commit",
    )
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 1
        assert (
            db.execute("SELECT refunded_cents FROM orders WHERE id='ORD-004'").fetchone()[0]
            == 10000
        )
        calls = db.execute("SELECT count(*) FROM call_reservations").fetchone()[0]
    completed = child(storage, "resume", run_id=run_id)
    assert completed["status"] == "completed" and len(completed["result"]["receipts"]) == 1
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM call_reservations").fetchone()[0] == calls


def test_bounded_queue_rejects_without_partial_run_or_idempotency_receipt(storage):
    repo, settings = storage
    service = ApplicationService(repo, settings, capacity=1, clock=lambda: NOW)
    entered, release = threading.Event(), threading.Event()
    actual = service.execute

    async def blocked(run_id):
        entered.set()
        assert release.wait(5)
        return await actual(run_id)

    service.execute = blocked
    with TestClient(create_app(settings, service=service)) as client:
        try:
            start(client)
            assert entered.wait(5)
            start(client, "T-MISSING-001", key="second")
            response = post(client, "/tickets/T-DELAY-001/runs", key="third")
            assert response.status_code == 503 and response.json()["code"] == "QUEUE_FULL"
            with read_database(repo.path) as db:
                assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
                assert db.execute("SELECT count(*) FROM execution_jobs").fetchone()[0] == 2
                assert db.execute("SELECT count(*) FROM request_idempotency").fetchone()[0] == 2
        finally:
            release.set()


def test_queued_cancellation_never_calls_model(storage):
    repo, settings = storage
    service = ApplicationService(repo, settings, clock=lambda: NOW)
    # Exercise durable admission before the executor exists; this is the crash window above.
    accepted = service.start_run(
        "T-NOTRECEIVED-002", DEMO_IDENTITIES["demo-customer-a"], "start", StartRun()
    )
    service.cancel(accepted["run_id"], DEMO_IDENTITIES["demo-customer-a"], "cancel")
    with TestClient(create_app(settings, service=service)) as client:
        result = wait_run(client, accepted["run_id"], "cancelled", pending=False)
        assert result["result"]["model_calls"] == 0
        assert result["result"]["receipts"] == []


def test_full_queue_still_accepts_idle_cancellation_signal(storage):
    service = ApplicationService(*storage, capacity=1, clock=lambda: NOW)
    entered, release = threading.Event(), threading.Event()
    actual = service.execute

    async def blocked(run_id):
        entered.set()
        assert release.wait(5)
        return await actual(run_id)

    with TestClient(create_app(storage[1], service=service)) as client:
        first = start(client)
        wait_run(client, first, "paused")
        service.execute = blocked
        try:
            start(client, "T-MISSING-001", key="second")
            assert entered.wait(5)
            start(client, "T-DELAY-001", key="third")
            response = post(client, f"/runs/{first}/cancel", key="cancel")
            assert response.status_code == 202 and response.json()["job_id"] is None
            view = client.get(f"/runs/{first}", headers=CUSTOMER).json()
            assert view["cancel_requested"] and view["pending_input"] is None
        finally:
            release.set()
        wait_run(client, first, "cancelled", pending=False)


def test_executor_failure_is_visible_and_does_not_loop_cancel_attempts(storage):
    import time

    service = ApplicationService(*storage, clock=lambda: NOW)
    with TestClient(create_app(storage[1], service=service)) as client:
        run_id = start(client)
        wait_run(client, run_id, "paused")

        async def fail(_):
            raise RuntimeError("fake secret must not appear in DTO")

        service.execute = fail
        assert post(client, f"/runs/{run_id}/cancel", key="cancel").status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            view = client.get(f"/runs/{run_id}", headers=CUSTOMER).json()
            if view["status"] == "interrupted":
                break
            time.sleep(0.02)
        assert view["execution_error_code"] == "RuntimeError"
        assert "secret" not in json.dumps(view)
        time.sleep(0.3)  # More than one executor scan; a failed cancel is not auto-retried forever.
        with read_database(storage[0].path) as db:
            assert (
                db.execute("SELECT count(*) FROM execution_jobs WHERE intent='cancel'").fetchone()[
                    0
                ]
                == 1
            )
        assert client.get("/health").status_code == 200
