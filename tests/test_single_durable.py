import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from after_sales.agents.contracts import REPORT_ADAPTER
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, read_database
from after_sales.services.workflows import WorkflowService
from after_sales.workflows.reviewed import response_envelope
from after_sales.workflows.single import SingleReviewRun

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    "ticket,status",
    [
        ("T-DELAY-001", "completed"),
        ("T-DELAY-002", "paused"),
        ("T-NOTRECEIVED-002", "paused"),
        ("T-RETURN-001", "paused"),
        ("T-RETURN-002", "completed"),
        ("T-MISSING-001", "paused"),
        ("T-CROSS-001", "handed_off"),
        ("T-CONFLICT-001", "handed_off"),
    ],
)
def test_single_uses_one_agent_and_the_common_guards(tmp_path, ticket, status):
    async def scenario():
        settings = Settings(
            business_db_path=tmp_path / "business.sqlite",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite",
        )
        seed_demo(settings.business_db_path)
        run = await SingleReviewRun.create(
            BusinessRepository(settings.business_db_path), ticket, settings, clock=lambda: NOW
        )
        try:
            report = await run.start()
            assert report["status"] == status, report["error"] or report["review"]
            assert report["architecture"] == "single"
            assert {entry["role"] for entry in report["agent_runs"]} == {"single_agent"}
            assert not report["executed_actions"]
            assert report["statistics"]["model_calls"] <= settings.max_model_calls
            with pytest.raises(ValidationError):
                REPORT_ADAPTER.validate_python({**report, "architecture": "multi"})
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_single_cross_restart_approval_and_duplicate_effect(tmp_path):
    async def scenario():
        settings = Settings(
            business_db_path=tmp_path / "business.sqlite",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite",
        )
        seed_demo(settings.business_db_path)
        repo = BusinessRepository(settings.business_db_path)
        service = WorkflowService(repo, settings, clock=lambda: NOW)
        run = await service.create("T-NOTRECEIVED-002", workflow="single")
        first = await run.start()
        envelope = response_envelope(first["pending_input"], decision="approve")
        run_id = run.run_id
        await run.aclose()
        run = await service.load(run_id)
        try:
            final = await run.resume(envelope)
            assert final["status"] == "completed", final["error"]
            assert final["statistics"]["model_calls"] == first["statistics"]["model_calls"]
            assert (await run.resume(envelope))["executed_actions"] == final["executed_actions"]
            assert repo.get_order("ORD-004", customer_id="CUST-A").refunded_cents == 10000
            with read_database(repo.path) as db:
                assert db.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 1
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_single_customer_answer_amount_revision_and_cancellation(tmp_path):
    async def scenario():
        settings = Settings(
            business_db_path=tmp_path / "business.sqlite",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite",
        )
        seed_demo(settings.business_db_path)
        repo = BusinessRepository(settings.business_db_path)
        service = WorkflowService(repo, settings, clock=lambda: NOW)
        run = await service.create("T-MISSING-001", workflow="single")
        try:
            missing = await run.start()
            answer = response_envelope(missing["pending_input"], answers={"order_id": "ORD-019"})
            pending = await run.resume(answer)
            assert pending["input_revision"] == 2
            assert pending["pending_input"]["kind"] == "operator_decision"
            assert not pending["executed_actions"]
            service.cancel(run.run_id)
            run_id = run.run_id
            await run.aclose()
            run = await service.load(run_id)
            cancelled = await run.recover()
            assert cancelled["status"] == "cancelled" and not cancelled["executed_actions"]
        finally:
            await run.aclose()
        run = await service.create("T-NOTRECEIVED-002", workflow="single")
        try:
            first = await run.start()
            binding = first["pending_input"]["actions"][0]
            revised = await run.resume(
                response_envelope(
                    first["pending_input"],
                    decision="revise",
                    refund_amounts={binding["action_id"]: 9000},
                )
            )
            assert revised["pending_input"]["actions"][0]["candidate"]["amount_cents"] == 9000
            final = await run.resume(
                response_envelope(revised["pending_input"], decision="approve")
            )
            assert final["status"] == "completed"
            assert repo.get_order("ORD-004", customer_id="CUST-A").refunded_cents == 9000
        finally:
            await run.aclose()

    asyncio.run(scenario())
