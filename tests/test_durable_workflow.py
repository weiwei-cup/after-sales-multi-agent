import asyncio
import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from after_sales.agents.contracts import REPORT_ADAPTER, ActionBinding, ResumeInput
from after_sales.config import Settings
from after_sales.repositories.actions import ActionService, ApprovalStale, operation_payload
from after_sales.repositories.run_store import DurableInputConflict, IncompatibleRun
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect, transaction
from after_sales.workflows.durable import PersistentReviewRun
from after_sales.workflows.reviewed import ResumeRejected, response_envelope

pytestmark = [pytest.mark.integration, pytest.mark.recovery]
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)
WORKER = Path(__file__).parent / "support" / "durable_worker.py"


@pytest.fixture
def storage(tmp_path):
    settings = Settings(
        business_db_path=tmp_path / "business.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
    )
    seed_demo(settings.business_db_path)
    return BusinessRepository(settings.business_db_path), settings


def counts(repository):
    with connect(repository.path, readonly=True) as db:
        return {
            name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("action_ledger", "action_events", "human_inputs")
        } | {
            "effects": db.execute(
                "SELECT count(*) FROM after_sales_history WHERE operation_key IS NOT NULL"
            ).fetchone()[0],
            "refunded": db.execute(
                "SELECT refunded_cents FROM orders WHERE id='ORD-004'"
            ).fetchone()[0],
        }


def snapshot(path):
    with connect(path, readonly=True) as db:
        return tuple(db.iterdump())


def child(
    storage,
    operation,
    *,
    response=None,
    crash=None,
    ticket="T-NOTRECEIVED-002",
    run_id="worker-run",
):
    _, settings = storage
    args = [
        sys.executable,
        str(WORKER),
        str(settings.business_db_path.parent),
        operation,
        "--ticket",
        ticket,
        "--run",
        run_id,
    ]
    if response is not None:
        path = settings.business_db_path.parent / "response.json"
        path.write_text(json.dumps(response))
        args.extend(["--response", str(path)])
    if crash:
        args.extend(["--crash", crash])
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if crash:
        assert result.returncode == 86, result.stderr or result.stdout
        return None
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


async def create(storage, ticket="T-NOTRECEIVED-002", **kwargs):
    repo, settings = storage
    return await PersistentReviewRun.create(repo, ticket, settings, clock=lambda: NOW, **kwargs)


async def load(storage, run_id, **kwargs):
    repo, settings = storage
    return await PersistentReviewRun.load(repo, run_id, settings, clock=lambda: NOW, **kwargs)


@pytest.mark.parametrize(
    "ticket,kind", [("T-MISSING-001", "customer_info"), ("T-RETURN-001", "operator_decision")]
)
def test_pending_and_versions_survive_new_process(storage, ticket, kind):
    first = child(storage, "start", ticket=ticket)
    second = child(storage, "resume")
    assert first["status"] == second["status"] == "paused"
    assert first["pending_input"] == second["pending_input"]
    assert first["node_trace"] == second["node_trace"]
    assert first["statistics"]["model_calls"] == second["statistics"]["model_calls"]
    assert second["pending_input"]["kind"] == kind
    if kind == "customer_info":
        second = child(
            storage,
            "resume",
            response=response_envelope(second["pending_input"], answers={"order_id": "ORD-019"}),
        )
        assert second["status"] == "paused"
        assert second["input_revision"] == 2
        assert second["pending_input"]["kind"] == "operator_decision"
    third = child(
        storage, "resume", response=response_envelope(second["pending_input"], decision="approve")
    )
    fourth = child(storage, "resume")
    assert third["status"] == fourth["status"] == "completed"
    assert fourth["business_status"] == "waiting_return"
    assert third["executed_actions"] == fourth["executed_actions"]
    assert counts(storage[0])["effects"] == 1
    assert len(fourth["confirmations"]) == 1


@pytest.mark.parametrize(
    "stage",
    [
        "before_commit",
        "after_commit",
        "after_input_commit",
        "after_node:wait_operator",
        "after_node:approve",
    ],
)
def test_process_death_recovers_each_approval_commit_boundary(storage, stage):
    paused = child(storage, "start")
    child(
        storage,
        "resume",
        response=response_envelope(paused["pending_input"], decision="approve"),
        crash=stage,
    )
    before = counts(storage[0])
    committed = stage in {"after_commit", "after_node:approve"}
    assert before["refunded"] == (10000 if committed else 0)
    assert before["action_ledger"] == before["action_events"] == before["effects"] == int(committed)
    assert before["human_inputs"] == 1
    report = child(storage, "resume")
    assert report["status"] == "completed"
    assert report["business_status"] == "resolved"
    assert len(report["confirmations"]) == len(report["executed_actions"]) == 1
    assert counts(storage[0]) == {
        "action_ledger": 1,
        "action_events": 1,
        "human_inputs": 1,
        "effects": 1,
        "refunded": 10000,
    }
    assert child(storage, "resume")["executed_actions"] == report["executed_actions"]
    if committed:
        assert any(e["kind"] == "action_replayed" for e in report["events"])


@pytest.mark.parametrize(
    "stage", ["after_node:draft", "after_node:prepare_customer", "after_node:prepare_operator"]
)
def test_process_death_before_checkpoint_preserves_pending_registry(storage, stage):
    ticket = "T-MISSING-001" if stage.endswith("customer") else "T-NOTRECEIVED-002"
    child(storage, "start", ticket=ticket, crash=stage)
    recovered = child(storage, "resume")
    assert recovered["status"] == "paused"
    assert len(recovered["pending_history"]) == 1
    assert counts(storage[0])["effects"] == 0


def test_customer_input_commit_is_replayed_once(storage):
    first = child(storage, "start", ticket="T-MISSING-001")
    child(
        storage,
        "resume",
        response=response_envelope(first["pending_input"], answers={"order_id": "ORD-019"}),
        crash="after_input_commit",
    )
    recovered = child(storage, "resume")
    assert recovered["status"] == "paused"
    assert recovered["input_revision"] == 2
    ticket = storage[0].get_ticket("T-MISSING-001")
    assert ticket.input_revision == 2
    assert len([m for m in ticket.messages if m.content == "order_id: ORD-019"]) == 1
    assert counts(storage[0])["human_inputs"] == 1


@pytest.mark.parametrize(
    "ticket,status",
    [
        ("T-RETURN-001", "waiting_return"),
        ("T-NOTRECEIVED-002", "resolved"),
        ("T-DELAY-002", "processing"),
    ],
)
def test_all_mock_action_types_have_distinct_business_completion(storage, ticket, status):
    async def scenario():
        run = await create(storage, ticket)
        try:
            report = await run.start()
            assert report["status"] == "paused", report["proposal"]
            report = await run.resume(
                response_envelope(report["pending_input"], decision="approve")
            )
            assert report["status"] == "completed", report["error"]
            assert report["business_status"] == status
            assert not report["candidate_only"]
            assert len(report["executed_actions"]) == 1
            storage[0].ticket_view(
                ticket
            )  # DTO projection must tolerate the new operation_key column.
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_rejected_operator_never_writes_an_action(storage):
    async def scenario():
        run = await create(storage)
        try:
            report = await run.start()
            report = await run.resume(response_envelope(report["pending_input"], decision="reject"))
            assert report["status"] == "handed_off"
            assert report["business_status"] == "handed_off"
            assert counts(storage[0])["effects"] == 0
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_same_operation_returns_original_receipt_and_conflicting_payload_fails(storage):
    async def scenario():
        run = await create(storage)
        try:
            report = await run.start()
            pending = report["pending_input"]
            report = await run.resume(response_envelope(pending, decision="approve"))
            binding = ActionBinding.model_validate(pending["actions"][0])
            receipt = run.actions.execute(
                run.run_id, pending["pending_id"], binding, now=NOW + timedelta(days=100)
            )
            assert receipt.model_dump(mode="json") == report["executed_actions"][0]
            before = counts(storage[0])
            payload = operation_payload(run.ticket.id, 1, binding.candidate)
            payload["amount_cents"] -= 1
            with pytest.raises(DurableInputConflict, match="different payload"):
                run.actions.lookup(payload)
            assert counts(storage[0]) == before
        finally:
            await run.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_id", "WRONG"),
        ("role", "customer"),
        ("proposal_revision", 2),
        ("proposal_hash", "0" * 64),
        ("pending_id", "wrong-pending"),
    ],
)
def test_bad_resume_is_rejected_before_consumption_or_checkpoint_change(storage, field, value):
    async def scenario():
        run = await create(storage)
        try:
            report = await run.start()
            raw = response_envelope(report["pending_input"], decision="approve")
            raw[field] = value
            before = snapshot(storage[0].path)
            checkpoint = snapshot(storage[1].checkpoint_db_path)
            with pytest.raises((ResumeRejected, ValidationError)):
                await run.resume(raw)
            assert snapshot(storage[0].path) == before
            assert snapshot(storage[1].checkpoint_db_path) == checkpoint
        finally:
            await run.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change", ["order", "tracking", "proof", "product", "history", "policy", "time", "new_policy"]
)
def test_latest_facts_policy_and_execution_clock_invalidate_old_approval(storage, change):
    async def scenario():
        run = await create(storage, "T-RETURN-001" if change == "time" else "T-NOTRECEIVED-002")
        try:
            report = await run.start()
            old = report["pending_input"]
            if change == "time":
                run.clock = lambda: NOW + timedelta(days=30)
            else:
                with transaction(storage[0].path) as db:
                    if change == "order":
                        db.execute(
                            "UPDATE orders SET refunded_cents=1000,version=version+1 WHERE "
                            "id='ORD-004'"
                        )
                    elif change == "tracking":
                        db.execute(
                            "UPDATE tracking_events SET description=description||' "
                            "updated',version=version+1 WHERE order_id='ORD-004'"
                        )
                    elif change == "proof":
                        db.execute(
                            "UPDATE delivery_proofs SET description=description||' "
                            "updated',version=version+1 WHERE order_id='ORD-004'"
                        )
                    elif change == "product":
                        db.execute(
                            "UPDATE products SET name=name||' updated' WHERE id IN (SELECT "
                            "product_id FROM order_items WHERE order_id='ORD-004')"
                        )
                    elif change == "history":
                        db.execute(
                            "INSERT INTO after_sales_history VALUES "
                            "('external','ORD-004','issue_mock_refund','cancelled',1,'2026-10-02T"
                            "04:00:00Z',1,NULL)"
                        )
                    else:
                        row = db.execute(
                            "SELECT id,version,payload FROM policies WHERE payload LIKE "
                            "'%issue_mock_refund%' LIMIT 1"
                        ).fetchone()
                        payload = json.loads(row["payload"])
                        if change == "new_policy":
                            payload["id"] = "NEW-POLICY"
                            db.execute(
                                "INSERT INTO policies VALUES (?,?,?)",
                                (payload["id"], payload["version"], json.dumps(payload)),
                            )
                        else:
                            payload["title"] += " updated"
                            db.execute(
                                "UPDATE policies SET payload=? WHERE id=? AND version=?",
                                (json.dumps(payload), row["id"], row["version"]),
                            )
            report = await run.resume(response_envelope(old, decision="approve"))
            assert counts(storage[0])["effects"] == 0
            assert any(e["kind"] == "approval_invalidated" for e in report["events"]), report[
                "error"
            ]
            assert report["proposal_revision"] == old["proposal_revision"] + 1, report["error"]
            if report["status"] == "paused":
                assert report["pending_input"]["pending_id"] != old["pending_id"]
                with pytest.raises(ResumeRejected):
                    await run.resume(response_envelope(old, decision="approve"))
                final = await run.resume(
                    response_envelope(report["pending_input"], decision="approve")
                )
                assert final["status"] == "completed", final["error"]
                assert counts(storage[0])["effects"] == 1
            else:
                assert change == "time" and report["status"] == "completed", report["error"]
            rows = run.store.history()
            assert rows[0]["status"] == "invalidated"
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_process_death_during_stale_approval_refresh_is_recoverable(storage):
    first = child(storage, "start")
    with transaction(storage[0].path) as db:
        db.execute("UPDATE orders SET refunded_cents=1000,version=version+1 WHERE id='ORD-004'")
    child(
        storage,
        "resume",
        response=response_envelope(first["pending_input"], decision="approve"),
        crash="after_node:approve",
    )
    report = child(storage, "resume")
    assert report["status"] == "paused", report["error"]
    assert report["proposal_revision"] == 2
    assert counts(storage[0])["effects"] == 0


def test_per_run_process_lock_and_duplicate_run_id(storage):
    async def scenario():
        run = await create(storage, run_id="locked-run")
        try:
            await run.start()
            with pytest.raises(ResumeRejected, match="owned"):
                await load(storage, run.run_id)
            with pytest.raises(ResumeRejected, match="owned"):
                await create(storage, run_id=run.run_id)
        finally:
            await run.aclose()
        with pytest.raises(DurableInputConflict, match="already"):
            await create(storage, run_id="locked-run")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        "workflow_version",
        "schema_version",
        "checkpoint_path",
        "checkpoint_manifest",
        "checkpoint_user_version",
    ],
)
def test_incompatible_versions_are_rejected_without_modifying_data(storage, target):
    async def scenario():
        run = await create(storage, run_id="version-run")
        try:
            await run.start()
        finally:
            await run.aclose()
        if target.startswith("checkpoint_") and target != "checkpoint_path":
            with connect(storage[1].checkpoint_db_path) as db:
                if target == "checkpoint_manifest":
                    db.execute("UPDATE workflow_manifest SET schema_version='old-state'")
                else:
                    db.execute("PRAGMA user_version=999")
        else:
            with transaction(storage[0].path) as db:
                db.execute(
                    f"UPDATE workflow_runtime SET {target}='old-value' WHERE run_id='version-run'"
                )
        before = snapshot(storage[0].path)
        checkpoint = snapshot(storage[1].checkpoint_db_path)
        with pytest.raises(IncompatibleRun):
            await load(storage, "version-run")
        assert snapshot(storage[0].path) == before
        assert snapshot(storage[1].checkpoint_db_path) == checkpoint

    asyncio.run(scenario())


def test_budget_reservations_survive_restart_and_settings_cannot_reset_limits(storage):
    async def scenario():
        repo, settings = storage
        settings = settings.model_copy(update={"max_model_calls": 1})
        run = await PersistentReviewRun.create(
            repo, "T-NOTRECEIVED-002", settings, clock=lambda: NOW, run_id="budget-run"
        )
        try:
            report = await run.start()
            assert report["status"] == "handed_off"
            assert report["error"]["code"] == "CallLimitExceeded"
        finally:
            await run.aclose()
        run = await load(storage, "budget-run")
        try:
            assert run.settings.max_model_calls == 1
            report = await run.recover()
            assert report["status"] == "handed_off"
            assert report["statistics"]["model_calls"] == 1
            assert counts(repo)["effects"] == 0
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_concurrent_approved_refunds_cannot_spend_the_same_balance(storage):
    async def prepare():
        repo, settings = storage
        first = await create(storage, run_id="refund-a")
        second = None
        try:
            a = await first.start()
            # A distinct ticket is a distinct operation against the same order balance.
            with transaction(repo.path) as db:
                db.execute(
                    "INSERT INTO tickets SELECT "
                    "'T-REFUND-SECOND',customer_id,supplied_order_id,order_id,type,'new',"
                    "created_at,input_revision,version FROM tickets WHERE "
                    "id='T-NOTRECEIVED-002'"
                )
                db.execute(
                    "INSERT INTO ticket_messages VALUES "
                    "('second-msg','T-REFUND-SECOND','customer','please "
                    "refund','2026-10-02T04:00:00Z')"
                )
            second = await create(storage, "T-REFUND-SECOND", run_id="refund-b")
            b = await second.start()
            approvals = []
            for run, report in [(first, a), (second, b)]:
                pending = report["pending_input"]
                run.store.accept(
                    ResumeInput.model_validate(response_envelope(pending, decision="approve")),
                    run.ticket,
                    NOW,
                )
                approvals.append(
                    (
                        run.run_id,
                        pending["pending_id"],
                        ActionBinding.model_validate(pending["actions"][0]),
                    )
                )
            return approvals
        finally:
            await first.aclose()
            if second:
                await second.aclose()

    approvals = asyncio.run(prepare())
    barrier = Barrier(2)

    def execute(args):
        barrier.wait(timeout=10)
        try:
            return ActionService(storage[0].path).execute(*args, now=NOW)
        except ApprovalStale:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, approvals))
    assert sum(r is not None for r in results) == 1
    assert counts(storage[0])["refunded"] == 10000
    assert counts(storage[0])["effects"] == 1


@pytest.mark.parametrize(
    "mutation", ["receipt_ticket", "amount", "candidate_flag", "missing_receipt", "operation_key"]
)
def test_p06_reports_reject_inconsistent_execution_claims(storage, mutation):
    first = child(storage, "start")
    report = child(
        storage, "resume", response=response_envelope(first["pending_input"], decision="approve")
    )
    report = copy.deepcopy(report)
    if mutation == "receipt_ticket":
        report["executed_actions"][0]["ticket_id"] = "other-ticket"
    elif mutation == "amount":
        report["executed_actions"][0]["amount_cents"] -= 1
    elif mutation == "candidate_flag":
        report["candidate_only"] = True
    elif mutation == "missing_receipt":
        report["executed_actions"] = []
        report["candidate_only"] = True
    else:
        report["executed_actions"][0]["operation_key"] = "op-wrong"
    with pytest.raises(ValidationError):
        REPORT_ADAPTER.validate_python(report)


def test_resume_duplicate_approved_envelope_returns_receipt_without_new_calls(storage):
    async def scenario():
        run = await create(storage)
        try:
            first = await run.start()
            answer = response_envelope(first["pending_input"], decision="approve")
            report = await run.resume(answer)
            before = counts(storage[0])
            repeated = await run.resume(answer)
            assert repeated["executed_actions"] == report["executed_actions"]
            assert repeated["statistics"]["tool_calls"] == report["statistics"]["tool_calls"]
            assert counts(storage[0]) == before
            wrong = {**answer, "actor_id": "other"}
            with pytest.raises(ResumeRejected):
                await run.resume(wrong)
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_new_run_for_existing_registration_preserves_waiting_return(storage):
    async def scenario():
        run = await create(storage, "T-RETURN-001")
        try:
            first = await run.start()
            await run.resume(response_envelope(first["pending_input"], decision="approve"))
        finally:
            await run.aclose()
        run = await create(storage, "T-RETURN-001")
        try:
            report = await run.start()
            assert (
                report["status"] == "completed"
                and report["proposal"]["decision"] == "existing_application"
            )
            assert report["business_status"] == "waiting_return"
            assert report["executed_actions"] == []
            assert counts(storage[0])["effects"] == 1
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_same_ticket_requires_resuming_its_existing_paused_run(storage):
    async def scenario():
        run = await create(storage, run_id="active-ticket")
        try:
            await run.start()
        finally:
            await run.aclose()
        with pytest.raises(DurableInputConflict, match="resume active-ticket"):
            await create(storage, run_id="other-run")

    asyncio.run(scenario())


def test_completed_partial_refund_different_amount_same_operation_conflicts(storage):
    async def scenario():
        run = await create(storage)
        try:
            report = await run.start()
            pending = report["pending_input"]
            amount_id = pending["actions"][0]["action_id"]
            report = await run.resume(
                response_envelope(pending, decision="revise", refund_amounts={amount_id: 9000})
            )
            report = await run.resume(
                response_envelope(report["pending_input"], decision="approve")
            )
            assert report["status"] == "completed"
        finally:
            await run.aclose()
        before = counts(storage[0])
        run = await create(storage)
        try:
            report = await run.start()
            assert report["pending_input"]["actions"][0]["candidate"]["amount_cents"] == 1000
            report = await run.resume(
                response_envelope(report["pending_input"], decision="approve")
            )
            assert report["status"] == "handed_off"
            assert report["error"]["code"] == "DurableInputConflict"
            after = counts(storage[0])
            for name in ("effects", "action_ledger", "action_events", "refunded"):
                assert after[name] == before[name]
        finally:
            await run.aclose()

    asyncio.run(scenario())


def test_same_approved_operation_concurrently_returns_one_receipt(storage):
    async def prepare():
        run = await create(storage)
        try:
            report = await run.start()
            pending = report["pending_input"]
            run.store.accept(
                ResumeInput.model_validate(response_envelope(pending, decision="approve")),
                run.ticket,
                NOW,
            )
            return (
                run.run_id,
                pending["pending_id"],
                ActionBinding.model_validate(pending["actions"][0]),
            )
        finally:
            await run.aclose()

    args = asyncio.run(prepare())
    barrier = Barrier(2)

    def execute(_):
        barrier.wait(timeout=10)
        return ActionService(storage[0].path).execute(*args, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert results[0] == results[1]
    assert counts(storage[0])["effects"] == counts(storage[0])["action_events"] == 1
    assert counts(storage[0])["refunded"] == 10000


def test_business_reset_does_not_make_old_graph_a_new_run(storage):
    first = child(storage, "start")
    seed_demo(storage[0].path, reset=True)
    checkpoint = snapshot(storage[1].checkpoint_db_path)

    async def scenario():
        with pytest.raises(DurableInputConflict, match="already has checkpoints"):
            await create(storage, run_id=first["run_id"])

    asyncio.run(scenario())
    assert snapshot(storage[1].checkpoint_db_path) == checkpoint
    assert counts(storage[0])["effects"] == 0


def test_execution_clock_is_read_after_database_write_lock_and_skipped_on_replay(storage):
    async def prepare():
        run = await create(storage, "T-RETURN-001")
        try:
            report = await run.start()
            pending = report["pending_input"]
            run.store.accept(
                ResumeInput.model_validate(response_envelope(pending, decision="approve")),
                run.ticket,
                NOW,
            )
            return (
                run.run_id,
                pending["pending_id"],
                ActionBinding.model_validate(pending["actions"][0]),
            )
        finally:
            await run.aclose()

    args = asyncio.run(prepare())

    def clock():
        # Another connection cannot take the write lock while the execution clock is sampled.
        import sqlite3

        with connect(storage[0].path) as db:
            db.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        return NOW

    receipt = ActionService(storage[0].path).execute(*args, clock=clock)

    def must_not_read_clock():
        raise AssertionError("replay must return the existing receipt before current-clock checks")

    assert ActionService(storage[0].path).execute(*args, clock=must_not_read_clock) == receipt
