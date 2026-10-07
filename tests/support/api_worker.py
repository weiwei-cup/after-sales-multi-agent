"""Real process exits at HTTP admission, graph execution and business commit boundaries."""

import argparse
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from after_sales.api.app import create_app
from after_sales.config import Settings
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationService


def main(args):
    root = Path(args.root)
    settings = Settings(
        business_db_path=root / "business.sqlite", checkpoint_db_path=root / "checkpoints.sqlite"
    )

    def fault(stage):
        if stage == args.crash:
            os._exit(86)

    service = ApplicationService(
        BusinessRepository(settings.business_db_path),
        settings,
        clock=lambda: datetime(2026, 10, 2, 4, tzinfo=UTC),
        fault=fault,
    )
    if args.crash == "after_admission":
        service._worker = lambda: threading.Event().wait(30)
    headers = {
        "Authorization": "Bearer demo-operator" if args.response else "Bearer demo-customer-a",
        "Idempotency-Key": "process-request",
    }
    with TestClient(create_app(settings, service=service)) as client:
        if args.operation == "start":
            response = client.post("/tickets/T-NOTRECEIVED-002/runs", json={}, headers=headers)
        elif args.operation == "respond":
            response = client.post(
                f"/runs/{args.run}/responses",
                json=json.loads(Path(args.response).read_text()),
                headers=headers,
            )
        else:
            response = client.post(f"/runs/{args.run}/resume", json={}, headers=headers)
        assert response.status_code == 202, response.text
        if args.crash == "after_admission":
            os._exit(86)
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = client.get(f"/runs/{run_id}", headers=headers).json()
            if result["status"] not in ("queued", "running"):
                print(json.dumps(result))
                return
            time.sleep(0.02)
        raise AssertionError("worker deadline exceeded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("operation", choices=["start", "respond", "resume"])
    parser.add_argument("--run")
    parser.add_argument("--response")
    parser.add_argument("--crash")
    main(parser.parse_args())
