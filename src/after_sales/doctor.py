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
        "phase": "P04",
        "python": platform.python_version(),
        "packages": packages,
        "model_mode": settings.model_mode,
        "model_connectivity": "not_checked",
        "agent_execution": "single_and_serial_multi_agent_scripted",
        "live_smoke": "skipped_provider_deferred_by_user",
        "readonly_tools": {
            "inspection": "after-sales inspect --ticket T-RETURN-001",
            "timeout_seconds": settings.tool_timeout_seconds,
            "max_result_bytes": settings.tool_max_result_bytes,
            "evidence_persistence": "in_memory_until_P06",
        },
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
            "call_limits": "enforced_in_P03_including_rule_rechecks",
            "proposal_schema_repairs": settings.proposal_repair_limit,
            "model_timeout_seconds": settings.model_timeout_seconds,
        },
    }
