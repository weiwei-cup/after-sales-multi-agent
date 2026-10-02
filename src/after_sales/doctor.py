"""Read-only local checks, deliberately without provider or network calls."""

import platform
from importlib.metadata import PackageNotFoundError, version

from after_sales.config import Settings

PACKAGES = (
    "after-sales-multi-agent",
    "langchain",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "pydantic",
    "pydantic-settings",
)


def build_report(settings: Settings) -> dict[str, object]:
    packages: dict[str, str] = {}
    missing: list[str] = []
    for package in PACKAGES:
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "missing"
            missing.append(package)
    python_ok = platform.python_version_tuple()[:2] == ("3", "12")
    return {
        "ok": python_ok and not missing,
        "phase": "P01",
        "python": platform.python_version(),
        "packages": packages,
        "model_mode": settings.model_mode,
        "model_connectivity": "not_checked",
        "agent_execution": "available_from_P03",
        "databases": {
            "business": str(settings.business_db_path),
            "checkpoints": str(settings.checkpoint_db_path),
            "initialization": {"business": "after-sales seed", "checkpoints": "available_from_P06"},
        },
        "budgets": {
            "model_calls": settings.max_model_calls,
            "tool_calls": settings.max_tool_calls,
            "review_repairs": settings.review_repair_limit,
            "concurrency": settings.max_concurrency,
            "tokens_soft_limit": settings.token_budget,
            "enforcement": "available_from_P05_and_P07",
        },
    }
