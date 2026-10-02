"""Separate-process P06 harness; trusted fault hooks never enter the product CLI."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from after_sales.config import Settings
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.workflows.durable import PersistentReviewRun


async def work(args):
    root = Path(args.root)
    settings = Settings(
        business_db_path=root / "business.sqlite", checkpoint_db_path=root / "checkpoints.sqlite"
    )
    repo = BusinessRepository(settings.business_db_path)

    def fault(stage):
        if stage == args.crash:
            os._exit(86)

    kwargs = {"clock": lambda: datetime(2026, 10, 2, 4, tzinfo=UTC), "fault": fault}
    if args.operation == "start":
        run = await PersistentReviewRun.create(
            repo, args.ticket, settings, run_id=args.run, **kwargs
        )
    else:
        run = await PersistentReviewRun.load(repo, args.run, settings, **kwargs)
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
    parser.add_argument("operation", choices=["start", "resume"])
    parser.add_argument("--run", default="worker-run")
    parser.add_argument("--ticket", default="T-NOTRECEIVED-002")
    parser.add_argument("--response")
    parser.add_argument("--crash")
    asyncio.run(work(parser.parse_args()))
