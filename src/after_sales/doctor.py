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
    "fastapi",
    "uvicorn",
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
        "phase": "P10",
        "python": platform.python_version(),
        "packages": packages,
        "model_mode": settings.model_mode,
        "model_connectivity": "not_checked",
        "agent_execution": "single_serial_reviewed_durable_and_parallel_multi_scripted",
        "human_input": {
            "workflow": "after-sales run --architecture multi --workflow durable --interactive",
            "resume_scope": "cross_process",
            "checkpointer": "AsyncSqliteSaver",
            "business_writes": "approved_mock_actions_via_transactional_ledger",
            "identity": "P08_fixed_demo_bearer_tokens; production_auth_deferred",
        },
        "live_smoke": "skipped_provider_deferred_by_user",
        "workbench": {
            "url": "/",
            "transport": "same_origin_HTTP_polling",
            "browser_checks": "separate_Playwright_e2e_suite",
        },
        "evaluation": {
            "command": "after-sales eval run",
            "gold": "read_after_saved_outputs",
            "single": "durable_common_guards",
            "live": "skipped_provider_deferred",
        },
        "readonly_tools": {
            "inspection": "after-sales inspect --ticket T-RETURN-001",
            "timeout_seconds": settings.tool_timeout_seconds,
            "max_result_bytes": settings.tool_max_result_bytes,
            "evidence_persistence": "durable_workflow_sqlite; inspect_and_reviewed_in_memory",
        },
        "databases": {
            "business": str(settings.business_db_path),
            "checkpoints": str(settings.checkpoint_db_path),
            "initialization": {
                "business": "after-sales seed",
                "checkpoints": "run --workflow durable",
            },
        },
        "budgets": {
            "model_calls": settings.max_model_calls,
            "tool_calls": settings.max_tool_calls,
            "review_repairs": settings.review_repair_limit,
            "concurrency": settings.max_concurrency,
            "token_reservation_limit": settings.token_budget,
            "enforcement": "P07_atomic_attempt_ledger_shared_concurrency_and_token_reservations",
            "call_limits": "P07_call_reservations_authoritative_across_restart",
            "active_time_budget_seconds": settings.active_time_budget_seconds,
            "transient_retry_limit": settings.transient_retry_limit,
            "token_usage": "unknown_keeps_reservation; monetary_price_unknown",
            "review_repair_enforcement": "P05_shared_by_code_review_and_operator_edits",
            "proposal_schema_repairs": settings.proposal_repair_limit,
            "model_timeout_seconds": settings.model_timeout_seconds,
        },
    }
