"""Packaged, repeatable workbench demo with isolated data and a fixed business clock."""

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from after_sales.api.app import create_app
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationService

DEMO_TIME = datetime(2026, 10, 2, 4, tzinfo=UTC)


@contextmanager
def demo_application(data_dir: Path | None = None):
    """Preserve explicitly selected data; otherwise clean up a fresh temporary directory."""
    directory = (
        nullcontext(data_dir.resolve())
        if data_dir is not None
        else TemporaryDirectory(prefix="after-sales-demo-")
    )
    with directory as root:
        settings = Settings(
            model_mode="scripted",
            business_db_path=Path(root) / "business.sqlite",
            checkpoint_db_path=Path(root) / "checkpoints.sqlite",
        )
        seed_demo(settings.business_db_path)
        service = ApplicationService(
            BusinessRepository(settings.business_db_path),
            settings,
            workers=settings.api_workers,
            capacity=settings.api_queue_capacity,
            clock=lambda: DEMO_TIME,
        )
        yield create_app(settings, service=service)


def run_demo(*, port: int = 8000, data_dir: Path | None = None) -> None:
    with demo_application(data_dir) as app:
        print(f"售后协作：http://127.0.0.1:{port} · 接口文档：/docs", flush=True)
        print("演示业务时间：2026-10-02 12:00 Asia/Shanghai；离线模型、模拟业务动作。", flush=True)
        if data_dir is None:
            print("使用临时资料，正常停止后清理；--data-dir 可保留运行记录。", flush=True)
        else:
            print(f"演示资料保存在：{data_dir.resolve()}；重启保留工单与审批。", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port, workers=1)
