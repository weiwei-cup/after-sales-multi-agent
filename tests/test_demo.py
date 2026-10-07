"""The public demo entry point must isolate everyday data and preserve explicit state."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from after_sales.demo import demo_application

pytestmark = pytest.mark.api
HEADERS = {"Authorization": "Bearer demo-customer-a"}


def test_disposable_demo_ignores_configured_database_paths_and_cleans_up(monkeypatch, tmp_path):
    daily = tmp_path / "daily.sqlite"
    daily.write_bytes(b"daily database must remain untouched")
    monkeypatch.setenv("AFTER_SALES_BUSINESS_DB_PATH", str(daily))
    monkeypatch.setenv("AFTER_SALES_CHECKPOINT_DB_PATH", str(tmp_path / "daily-checkpoints.sqlite"))
    with demo_application() as app, TestClient(app) as client:
        root = app.state.service.repository.path.parent
        assert root != tmp_path
        assert client.get("/health").json()["version"] == "0.10.0"
        assert client.get("/").status_code == 200
        assert client.get("/openapi.json").json()["info"]["version"] == "0.10.0"
        assert client.get("/tickets/T-NOTRECEIVED-002", headers=HEADERS).status_code == 200
    assert not root.exists()
    assert daily.read_bytes() == b"daily database must remain untouched"
    assert not (tmp_path / "daily-checkpoints.sqlite").exists()


def test_persistent_demo_restart_preserves_paused_run_and_pending_approval(tmp_path):
    directory = tmp_path / "demo"
    with demo_application(directory) as app, TestClient(app) as client:
        response = client.post(
            "/tickets/T-NOTRECEIVED-002/runs",
            headers={**HEADERS, "Idempotency-Key": "demo-restart"},
            json={"workflow": "parallel"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            run = client.get(f"/runs/{run_id}", headers=HEADERS).json()
            if run["pending_input"] is not None:
                break
            time.sleep(0.02)
        else:
            pytest.fail("demo run did not pause")
        assert run["status"] == "paused"
        assert run["pending_input"]["actions"][0]["amount_cents"] == 10000
        pending_id = run["pending_input"]["pending_id"]
    assert (directory / "business.sqlite").is_file()
    assert (directory / "checkpoints.sqlite").is_file()
    with demo_application(Path(directory)) as app, TestClient(app) as client:
        run = client.get(f"/runs/{run_id}", headers=HEADERS).json()
        assert run["status"] == "paused"
        assert run["pending_input"]["pending_id"] == pending_id
        assert run["pending_input"]["actions"][0]["amount_cents"] == 10000
