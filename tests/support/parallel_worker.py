"""Separate-process P07 recovery harness. Faults and fixed clocks stay out of the CLI."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from after_sales.config import Settings
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.workflows.parallel import ParallelReviewRun, request_cancel


async def work(args):
    root = Path(args.root)
    settings = Settings(
        business_db_path=root / "business.sqlite",
        checkpoint_db_path=root / "checkpoints.sqlite",
        max_model_calls=args.max_model_calls,
    )
    repo = BusinessRepository(settings.business_db_path)

    def fault(stage):
        if stage == args.crash:
            os._exit(86)

    kwargs = {"clock": lambda: datetime(2026, 10, 2, 4, tzinfo=UTC), "fault": fault}
    if args.operation == "cancel":
        print(json.dumps(request_cancel(repo, args.run, settings)))
        return
    if args.operation == "start":
        run = await ParallelReviewRun.create(repo, args.ticket, settings, run_id=args.run, **kwargs)
    else:
        run = await ParallelReviewRun.load(repo, args.run, settings, **kwargs)
    try:
        report = await run.start() if args.operation == "start" else await run.recover()
        if args.response:
            report = await run.resume(json.loads(Path(args.response).read_text()))
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await run.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("operation", choices=["start", "resume", "cancel"])
    parser.add_argument("--run", default="parallel-worker")
    parser.add_argument("--ticket", default="T-NOTRECEIVED-002")
    parser.add_argument("--response")
    parser.add_argument("--crash")
    parser.add_argument("--max-model-calls", type=int, default=20)
    asyncio.run(work(parser.parse_args()))
