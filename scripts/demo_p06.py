"""Reproduce P06 learning traces in temporary databases and separate Python processes."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import connect
from after_sales.workflows.reviewed import response_envelope

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "tests/support/durable_worker.py"


def worker(folder, operation, *, ticket="T-NOTRECEIVED-002", response=None, crash=None):
    command = [sys.executable, str(WORKER), str(folder), operation, "--ticket", ticket]
    if response:
        path = folder / "input.json"
        path.write_text(json.dumps(response))
        command.extend(["--response", str(path)])
    if crash:
        command.extend(["--crash", crash])
    result = subprocess.run(command, text=True, capture_output=True, timeout=40)
    if crash:
        if result.returncode != 86:
            raise RuntimeError(result.stderr)
        return None
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def snapshot(report, step):
    return {
        "step": step,
        **{
            name: report[name]
            for name in (
                "status",
                "business_status",
                "input_revision",
                "proposal_revision",
                "repair_count",
                "node_trace",
                "executed_actions",
            )
        },
        "pending_kind": report["pending_input"]["kind"] if report["pending_input"] else None,
        "statistics": {
            name: report["statistics"][name]
            for name in (
                "model_calls",
                "tool_calls",
                "validation_tool_calls",
                "schema_repairs",
                "token_usage",
            )
        },
        "confirmation_decisions": [c["decision"] for c in report["confirmations"]],
    }


def main():
    observed = {
        "schema_version": "p06-observations-v1",
        "dataset_version": "demo-v1",
        "model_mode": "scripted",
        "resume_scope": "cross_process",
        "clock": "2026-10-02T04:00:00Z (trusted test injection)",
        "runs": [],
    }
    with TemporaryDirectory(prefix="p06-demo-") as temporary:
        root = Path(temporary)
        for number, ticket in enumerate(
            ["T-DELAY-002", "T-RETURN-001", "T-MISSING-001", "T-NOTRECEIVED-002"]
        ):
            folder = root / str(number)
            seed_demo(folder / "business.sqlite")
            first = worker(folder, "start", ticket=ticket)
            (ROOT / "docs/graphs/p06-durable.mmd").write_text(
                first["graph_mermaid"].rstrip() + "\n"
            )
            steps = [snapshot(first, "start_process")]
            report = worker(folder, "resume")
            steps.append(snapshot(report, "reopen_process"))
            if ticket == "T-MISSING-001":
                report = worker(
                    folder,
                    "resume",
                    response=response_envelope(
                        report["pending_input"], answers={"order_id": "ORD-019"}
                    ),
                )
                steps.append(snapshot(report, "customer_order_answer_new_process"))
            approved = response_envelope(report["pending_input"], decision="approve")
            report = worker(folder, "resume", response=approved)
            steps.append(snapshot(report, "operator_approval_new_process"))
            replay = worker(folder, "resume")
            assert replay["executed_actions"] == report["executed_actions"]
            observed["runs"].append(
                {"ticket_id": ticket, "run_id": report["run_id"], "snapshots": steps}
            )
        folder = root / "crash"
        seed_demo(folder / "business.sqlite")
        first = worker(folder, "start")
        worker(
            folder,
            "resume",
            response=response_envelope(first["pending_input"], decision="approve"),
            crash="after_commit",
        )
        with connect(folder / "business.sqlite", readonly=True) as db:
            before = {
                "ledger_count": db.execute("SELECT count(*) FROM action_ledger").fetchone()[0],
                "refunded_cents": db.execute(
                    "SELECT refunded_cents FROM orders WHERE id='ORD-004'"
                ).fetchone()[0],
            }
        final = worker(folder, "resume")
        assert final["status"] == "completed" and len(final["executed_actions"]) == 1
        observed["crash_recovery"] = {
            "stage": "after_commit",
            "process_exit_code": 86,
            "before_recovery": before,
            "after_recovery": snapshot(final, "resume_from_ledger"),
            "ledger_replay_observed": any(e["kind"] == "action_replayed" for e in final["events"]),
        }
    path = ROOT / "docs/graphs/p06-demo-traces.json"
    path.write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(path),
                "business_databases": "temporary_only",
                "runs": 4,
                "crash_recovery": "passed",
            }
        )
    )


if __name__ == "__main__":
    main()
