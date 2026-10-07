"""Exercise the real loopback HTTP server against a fresh temporary demo database."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from after_sales.api.app import create_app
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationService


def demo_app():
    """Demo-only fixture clock; the normal application continues to use the real UTC clock."""
    settings = Settings()
    service = ApplicationService(
        BusinessRepository(settings.business_db_path),
        settings,
        clock=lambda: datetime(2026, 10, 2, 4, tzinfo=UTC),
    )
    return create_app(settings, service=service)


def demo():
    with tempfile.TemporaryDirectory(prefix="after-sales-p08-") as directory:
        root = Path(directory)
        seed_demo(root / "business.sqlite")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = {
            **os.environ,
            "AFTER_SALES_MODEL_MODE": "scripted",
            "AFTER_SALES_BUSINESS_DB_PATH": str(root / "business.sqlite"),
            "AFTER_SALES_CHECKPOINT_DB_PATH": str(root / "checkpoints.sqlite"),
        }
        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "scripts.demo_p08:demo_app",
                    "--factory",
                    "--app-dir",
                    str(Path(__file__).resolve().parent.parent),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--workers",
                    "1",
                ],
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    timeout=5,
                    headers={"Authorization": "Bearer demo-customer-a"},
                ) as client:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            health = client.get("/health")
                            if health.status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise RuntimeError("server failed to become ready: " + log.read())
                        time.sleep(0.05)
                    trace = [
                        {
                            "operation": "health",
                            "http_status": health.status_code,
                            "body": health.json(),
                        }
                    ]

                    def post(path, body, key, *, operator=False):
                        headers = {"Idempotency-Key": key}
                        if operator:
                            headers["Authorization"] = "Bearer demo-operator"
                        response = client.post(path, json=body, headers=headers)
                        assert response.status_code in (201, 202), response.text
                        trace.append(
                            {
                                "operation": path,
                                "http_status": response.status_code,
                                "body": response.json(),
                            }
                        )
                        return response.json()

                    def wait(run_id, status):
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            response = client.get(f"/runs/{run_id}")
                            response.raise_for_status()
                            result = response.json()
                            if result["status"] == status:
                                trace.append(
                                    {"operation": "query", "http_status": 200, "body": result}
                                )
                                return result
                            time.sleep(0.02)
                        raise AssertionError("run deadline: " + json.dumps(result))

                    created = post(
                        "/tickets", {"type": "return_request", "message": "我要退货"}, "demo-create"
                    )
                    accepted = post(f"/tickets/{created['ticket_id']}/runs", {}, "demo-start")
                    run_id = accepted["run_id"]
                    pending = wait(run_id, "paused")["pending_input"]
                    response = {
                        key: pending[key]
                        for key in (
                            "pending_id",
                            "input_revision",
                            "proposal_revision",
                            "proposal_hash",
                        )
                    }
                    post(
                        f"/runs/{run_id}/responses",
                        {**response, "answers": {"order_id": "ORD-019"}},
                        "demo-answer",
                    )
                    pending = wait(run_id, "paused")["pending_input"]
                    response = {
                        key: pending[key]
                        for key in (
                            "pending_id",
                            "input_revision",
                            "proposal_revision",
                            "proposal_hash",
                        )
                    }
                    approved = post(
                        f"/runs/{run_id}/responses",
                        {
                            **response,
                            "decision": "approve",
                            "action_hashes": {
                                a["action_id"]: a["content_hash"] for a in pending["actions"]
                            },
                        },
                        "demo-approve",
                        operator=True,
                    )
                    completed = wait(run_id, "completed")
                    assert len(completed["result"]["receipts"]) == 1
                    assert approved["run_id"] == run_id
                    events = client.get(f"/runs/{run_id}/events?limit=100").json()
                    assert events["events"] and events["next_after_seq"] > 0
                    return {
                        "phase": "P08",
                        "transport": "real_loopback_HTTP",
                        "temporary_database": True,
                        "trace": trace,
                        "first_events_page": events,
                    }
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = demo()
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "phase": "P08",
                "transport": result["transport"],
                "steps": len(result["trace"]),
                "outcome": "completed",
            },
            ensure_ascii=False,
        )
    )
