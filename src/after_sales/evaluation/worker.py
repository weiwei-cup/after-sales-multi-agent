"""Separate-process evaluation faults and temporary browser server; no gold is loaded."""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from after_sales.config import Settings
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.workflows.parallel import ParallelReviewRun
from after_sales.workflows.single import SingleReviewRun


async def crash(args, root, settings, now):
    cls = SingleReviewRun if args.architecture == "single" else ParallelReviewRun

    def fault(stage):
        if stage == "after_commit":
            os._exit(86)

    run = await cls.load(
        BusinessRepository(settings.business_db_path),
        args.run_id,
        settings,
        clock=lambda: now,
        fault=fault,
    )
    try:
        await run.resume(json.loads(Path(args.response).read_text()))
    finally:
        await run.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["crash", "serve"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--architecture", choices=["single", "multi"], default="multi")
    parser.add_argument("--run-id")
    parser.add_argument("--response")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    root, now = args.root, datetime.fromisoformat(args.as_of)
    limits = (
        json.loads((root / "worker-settings.json").read_text())
        if (root / "worker-settings.json").exists()
        else {}
    )
    settings = Settings(
        _env_file=None,
        model_mode="scripted",
        business_db_path=root / "business.sqlite",
        checkpoint_db_path=root / "checkpoints.sqlite",
        **limits,
    )
    if args.operation == "crash":
        asyncio.run(crash(args, root, settings, now))
    else:
        import uvicorn

        from after_sales.api.app import create_app
        from after_sales.services.application import ApplicationService

        service = ApplicationService(
            BusinessRepository(settings.business_db_path), settings, clock=lambda: now
        )
        uvicorn.run(
            create_app(settings, service=service),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )


if __name__ == "__main__":
    main()
