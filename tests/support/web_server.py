"""Only browser tests use the executor interruption injection in this factory."""

import os
import sys
from datetime import UTC, datetime

import uvicorn

from after_sales.api.app import create_app
from after_sales.config import Settings
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationService


def test_app():
    settings = Settings()
    interrupted = False

    def fault(stage):
        nonlocal interrupted
        if (
            os.environ.get("P09_TEST_INTERRUPT_ONCE")
            and not interrupted
            and stage == "after_node:intake"
        ):
            interrupted = True
            raise RuntimeError("P09_TEST_INTERRUPT")

    service = ApplicationService(
        BusinessRepository(settings.business_db_path),
        settings,
        clock=lambda: datetime(2026, 10, 2, 4, tzinfo=UTC),
        fault=fault,
    )
    return create_app(settings, service=service)


if __name__ == "__main__":
    uvicorn.run(test_app(), host="127.0.0.1", port=int(sys.argv[1]))
