"""Open a disposable P09 workbench; never seed or reset the daily database."""

import argparse
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from after_sales.api.app import create_app
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationService


def main(port):
    with tempfile.TemporaryDirectory(prefix="after-sales-p09-") as directory:
        root = Path(directory)
        settings = Settings(
            model_mode="scripted",
            business_db_path=root / "business.sqlite",
            checkpoint_db_path=root / "checkpoints.sqlite",
        )
        seed_demo(settings.business_db_path)
        service = ApplicationService(
            BusinessRepository(settings.business_db_path),
            settings,
            clock=lambda: datetime(2026, 10, 2, 4, tzinfo=UTC),
        )
        print(f"P09 工作台：http://127.0.0.1:{port}", flush=True)
        print("临时演示库，停止后清理；演示业务时间 2026-10-02 12:00 Asia/Shanghai。", flush=True)
        uvicorn.run(create_app(settings, service=service), host="127.0.0.1", port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    main(parser.parse_args().port)
