"""Observe P06 serial vs P07 parallel using the same fictional case and controlled read latency."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.tools.service import ToolSession
from after_sales.workflows.durable import PersistentReviewRun
from after_sales.workflows.parallel import ParallelReviewRun, request_cancel
from after_sales.workflows.reviewed import response_envelope

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)


async def observe(root, run_class, delay):
    settings = Settings(
        business_db_path=root / "business.sqlite", checkpoint_db_path=root / "checkpoints.sqlite"
    )
    seed_demo(settings.business_db_path)
    original = ToolSession.call
    names = {
        "get_order",
        "search_policy_candidates" if run_class == ParallelReviewRun else "search_policies",
    }

    async def delayed(self, name, arguments):
        if name in names:
            await asyncio.sleep(delay)
        return await original(self, name, arguments)

    ToolSession.call = delayed
    run = await run_class.create(
        BusinessRepository(settings.business_db_path),
        "T-NOTRECEIVED-002",
        settings,
        clock=lambda: NOW,
        run_id="learning-p07",
    )
    try:
        started = perf_counter()
        report = await run.start()
        elapsed = round((perf_counter() - started) * 1000, 3)
        if report["status"] != "paused":
            raise RuntimeError(report["error"] or report["graph_state"])
        graph = report["graph_mermaid"]
        timelines = [
            {
                k: e[k]
                for k in (
                    "sequence",
                    "kind",
                    "role",
                    "elapsed_ms",
                    "node",
                    "tool",
                    "task_id",
                    "attempt_id",
                    "duration_ms",
                )
                if k in e
            }
            for e in report["events"]
            if e["kind"]
            in {
                "node_started",
                "node_finished",
                "branch_started",
                "branch_finished",
                "join_decided",
                "model_started",
                "model_finished",
                "tool_started",
                "tool_finished",
            }
        ]
        snapshot = {
            "workflow": report["workflow_version"],
            "status": report["status"],
            "decision": report["proposal"]["decision"],
            "injected_read_delay_ms": delay * 1000,
            "measured_wall_ms": elapsed,
            "statistics": report["statistics"],
            "timeline": timelines,
        }
        if run_class == ParallelReviewRun and delay == 0:
            run_id = run.run_id
            pending = report["pending_input"]
            await run.aclose()
            run = await run_class.load(
                BusinessRepository(settings.business_db_path), run_id, settings, clock=lambda: NOW
            )
            snapshot["resume_model_calls"] = run.report()["statistics"]["model_calls"]
            completed = await run.resume(response_envelope(pending, decision="approve"))
            snapshot["approved"] = {
                k: completed[k] for k in ("status", "business_status", "executed_actions")
            }
            snapshot["cancel_completed"] = request_cancel(
                BusinessRepository(settings.business_db_path), run_id, settings
            )
        return snapshot, graph
    finally:
        ToolSession.call = original
        await run.aclose()


async def main():
    results = {
        "schema_version": "p07-observations-v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "model_mode": "scripted",
        "business_clock": NOW.isoformat(),
        "ticket_id": "T-NOTRECEIVED-002",
        "measurement_note": (
            "Single local observation per variant; 500 ms delays are injected "
            "into two independent reads. Learning traces do not measure "
            "real provider performance or statistical benchmarks."
        ),
        "runs": [],
    }
    with TemporaryDirectory(prefix="after-sales-p07-") as folder:
        for delay in (0, 0.5):
            for run_class in (PersistentReviewRun, ParallelReviewRun):
                root = Path(folder) / f"{run_class.__name__}-{delay}"
                root.mkdir()
                result, graph = await observe(root, run_class, delay)
                results["runs"].append(result)
                if run_class == ParallelReviewRun:
                    (ROOT / "docs/graphs/p07-parallel.mmd").write_text(graph.rstrip() + "\n")
    (ROOT / "docs/graphs/p07-demo-traces.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            [
                {
                    k: r[k]
                    for k in (
                        "workflow",
                        "status",
                        "decision",
                        "injected_read_delay_ms",
                        "measured_wall_ms",
                    )
                }
                for r in results["runs"]
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
