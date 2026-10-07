import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from after_sales.api.app import create_app
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, read_database, transaction
from after_sales.services.application import ApplicationService

pytestmark = [pytest.mark.api, pytest.mark.integration]
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)
CUSTOMER = {"Authorization": "Bearer demo-customer-a"}
OTHER = {"Authorization": "Bearer demo-customer-b"}
OPERATOR = {"Authorization": "Bearer demo-operator"}


@pytest.fixture
def storage(tmp_path):
    settings = Settings(
        business_db_path=tmp_path / "business.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
    )
    seed_demo(settings.business_db_path)
    return BusinessRepository(settings.business_db_path), settings


@pytest.fixture
def client(storage):
    repo, settings = storage
    service = ApplicationService(repo, settings, clock=lambda: NOW)
    with TestClient(create_app(settings, service=service)) as client:
        yield client


def post(client, path, body=None, *, key="test-key", headers=CUSTOMER):
    return client.post(path, json=body or {}, headers={**headers, "Idempotency-Key": key})


def start(client, ticket="T-NOTRECEIVED-002", *, key="start"):
    response = post(client, f"/tickets/{ticket}/runs", key=key)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def wait_run(client, run_id, status, *, pending=True, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}", headers=CUSTOMER)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] == status and (
            result["pending_input"]
            if pending
            else result["result"] and result["result"]["outcome"] in (status, "handed_off")
        ):
            return result
        time.sleep(0.02)
    raise AssertionError(f"run did not reach {status}: {result}")


def answer(pending, **payload):
    return {
        **{
            key: pending[key]
            for key in (
                "pending_id",
                "input_revision",
                "proposal_revision",
                "proposal_hash",
            )
        },
        "action_hashes": {a["action_id"]: a["content_hash"] for a in pending["actions"]},
        **payload,
    }


def test_health_no_model_and_openapi_contract(client, storage):
    assert client.get("/health").json()["phase"] == "P08"
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM call_reservations").fetchone()[0] == 0
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert "202" in schema["paths"]["/tickets/{ticket_id}/runs"]["post"]["responses"]
    assert "422" in schema["paths"]["/runs/{run_id}/responses"]["post"]["responses"]


def test_create_answer_approve_result_and_exact_duplicates(client, storage):
    response = post(
        client, "/tickets", {"type": "return_request", "message": "我要退货"}, key="create"
    )
    assert response.status_code == 201, response.text
    ticket_id = response.json()["ticket_id"]
    assert (
        post(
            client, "/tickets", {"type": "return_request", "message": "我要退货"}, key="create"
        ).json()["ticket_id"]
        == ticket_id
    )
    run_id = start(client, ticket_id)
    pending = wait_run(client, run_id, "paused")["pending_input"]
    assert pending["kind"] == "customer_info" and pending["can_respond"]
    body = answer(pending, answers={"order_id": "ORD-019"})
    accepted = post(client, f"/runs/{run_id}/responses", body, key="answer")
    assert accepted.status_code == 202, accepted.text
    assert post(client, f"/runs/{run_id}/responses", body, key="answer").json() == accepted.json()
    pending = wait_run(client, run_id, "paused")["pending_input"]
    assert pending["kind"] == "operator_decision" and not pending["can_respond"]
    operator_view = client.get(f"/runs/{run_id}", headers=OPERATOR).json()
    assert operator_view["pending_input"]["can_respond"]
    body = answer(pending, decision="approve")
    assert post(client, f"/runs/{run_id}/responses", body, key="approve").status_code == 403
    accepted = post(client, f"/runs/{run_id}/responses", body, key="approve", headers=OPERATOR)
    assert accepted.status_code == 202, accepted.text
    result = wait_run(client, run_id, "completed", pending=False)
    assert len(result["result"]["receipts"]) == 1
    assert (
        post(client, f"/runs/{run_id}/responses", body, key="approve", headers=OPERATOR).json()
        == accepted.json()
    )
    assert (
        post(
            client, f"/runs/{run_id}/responses", body, key="another-approve", headers=OPERATOR
        ).status_code
        == 202
    )
    with read_database(storage[0].path) as db:
        assert (
            db.execute("SELECT count(*) FROM action_ledger WHERE run_id=?", (run_id,)).fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT input_revision FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
            == 2
        )


def test_start_key_conflicts_and_active_ticket_race(client, storage):
    barrier = threading.Barrier(2)

    def attempt(key):
        barrier.wait(5)
        return post(client, "/tickets/T-NOTRECEIVED-002/runs", key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt, ["start-a", "start-b"]))
    assert sorted(r.status_code for r in responses) == [202, 409]
    winner = "start-a" if responses[0].status_code == 202 else "start-b"
    original = next(r for r in responses if r.status_code == 202)
    assert post(client, "/tickets/T-NOTRECEIVED-002/runs", key=winner).json() == original.json()
    changed = post(
        client, "/tickets/T-NOTRECEIVED-002/runs", {"expected_input_revision": 1}, key=winner
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM execution_jobs").fetchone()[0] == 1


def test_identity_scope_missing_identity_and_forged_fields(client):
    assert client.get("/tickets").status_code == 401
    assert client.get("/tickets", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/tickets", headers={b"Authorization": b"Bearer \xff"}).status_code == 401
    assert client.get("/tickets/T-NOTRECEIVED-002", headers=OTHER).status_code == 404
    assert client.get("/tickets/missing", headers=OTHER).status_code == 404
    assert client.get("/tickets/T-NOTRECEIVED-002", headers=OPERATOR).status_code == 200
    mine = client.get("/tickets", headers=OTHER).json()
    assert all(t["ticket_id"] != "T-NOTRECEIVED-002" for t in mine)
    assert (
        post(
            client, "/tickets", {"type": "unknown", "message": "问题", "customer_id": "CUST-B"}
        ).status_code
        == 422
    )
    run_id = start(client)
    for suffix in ("", "/events"):
        assert client.get(f"/runs/{run_id}{suffix}", headers=OTHER).status_code == 404
    for suffix in ("/resume", "/cancel"):
        assert post(client, f"/runs/{run_id}{suffix}", headers=OTHER).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"type": "unknown", "message": " "},
        {"type": "unknown", "message": "x", "supplied_order_id": "../bad"},
        {"type": "invalid", "message": "x"},
        {"type": "unknown", "message": "x", "actor_id": "OP-DEMO"},
    ],
)
def test_invalid_creation_clear_error_and_no_input_echo(client, body):
    response = post(client, "/tickets", body)
    assert response.status_code == 422
    assert set(response.json()) == {"code", "message", "request_id"}
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_stale_operator_revision_and_changed_confirmation(client, storage):
    run_id = start(client)
    pending = wait_run(client, run_id, "paused")["pending_input"]
    body = answer(pending, decision="approve")
    stale = {**body, "proposal_revision": pending["proposal_revision"] + 1}
    assert post(client, f"/runs/{run_id}/responses", stale, headers=OPERATOR).status_code == 409
    assert (
        post(
            client, f"/runs/{run_id}/responses", {**body, "actor_id": "OP-DEMO"}, headers=OPERATOR
        ).status_code
        == 422
    )
    assert (
        post(
            client,
            f"/runs/{run_id}/responses",
            {
                **body,
                "decision": "revise",
                "refund_amounts": {pending["actions"][0]["action_id"]: 0},
            },
            headers=OPERATOR,
        ).status_code
        == 409
    )
    assert post(client, f"/runs/{run_id}/responses", body, headers=OPERATOR).status_code == 202
    wait_run(client, run_id, "completed", pending=False)
    assert post(client, f"/runs/{run_id}/cancel", key="after-completed").status_code == 202
    assert client.get(f"/runs/{run_id}", headers=CUSTOMER).json()["status"] == "completed"
    assert (
        post(
            client,
            f"/runs/{run_id}/responses",
            {**body, "decision": "reject"},
            key="different",
            headers=OPERATOR,
        ).status_code
        == 409
    )
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 1
        assert (
            db.execute("SELECT refunded_cents FROM orders WHERE id='ORD-004'").fetchone()[0]
            == 10000
        )


def test_incremental_events_projection_and_free_text_redaction(client, storage):
    response = post(
        client,
        "/tickets",
        {"type": "unknown", "message": "手机13800138000 邮箱a@example.com sk-secret123"},
    )
    assert "13800138000" not in response.text and "a@example.com" not in response.text
    assert "secret123" not in response.text
    run_id = start(client)
    wait_run(client, run_id, "paused")
    with transaction(storage[0].path) as db:
        db.execute(
            "UPDATE run_events SET payload_json=json_set(payload_json,'$.secret', "
            "'sk-secret123','$.contact','13800138000') WHERE run_id=?",
            (run_id,),
        )
    cursor, collected = 0, []
    while True:
        response = client.get(f"/runs/{run_id}/events?after_seq={cursor}&limit=7", headers=CUSTOMER)
        page = response.json()
        assert "secret123" not in response.text and "13800138000" not in response.text
        collected.extend(e["sequence"] for e in page["events"])
        cursor = page["next_after_seq"]
        if not page["has_more"]:
            break
    assert len(collected) > 7 and collected == sorted(set(collected))
    assert (
        client.get(f"/runs/{run_id}/events?after_seq={cursor}", headers=CUSTOMER).json()["events"]
        == []
    )
    public = client.get(f"/runs/{run_id}", headers=CUSTOMER).text
    assert all(
        field not in public for field in ("graph_state", "evidence_refs", "messages", "usage")
    )


def test_health_remains_responsive_during_blocked_executor(storage):
    entered, release = threading.Event(), threading.Event()
    repo, settings = storage
    service = ApplicationService(repo, settings, clock=lambda: NOW)
    actual = service.execute

    async def blocked(run_id):
        entered.set()
        assert release.wait(5)
        return await actual(run_id)

    service.execute = blocked
    with TestClient(create_app(settings, service=service)) as client:
        try:
            start(client)
            assert entered.wait(5)
            before = time.monotonic()
            assert client.get("/health").status_code == 200
            assert client.get("/tickets", headers=CUSTOMER).status_code == 200
            assert time.monotonic() - before < 2
        finally:
            release.set()


def test_paused_cancel_is_applied_and_completed_cancel_preserves_receipt(client, storage):
    run_id = start(client, "T-MISSING-001")
    wait_run(client, run_id, "paused")
    assert post(client, f"/runs/{run_id}/resume").status_code == 409
    assert post(client, f"/runs/{run_id}/cancel", key="cancel").status_code == 202
    result = wait_run(client, run_id, "cancelled", pending=False)
    assert result["pending_input"] is None and not result["result"]["receipts"]
    assert post(client, f"/runs/{run_id}/cancel", key="cancel").status_code == 202
    assert post(client, f"/runs/{run_id}/resume", key="resume").status_code == 409


@pytest.mark.parametrize(
    "path", ["/tickets?limit=0", "/tickets?offset=-1", "/runs/missing/events?after_seq=-1"]
)
def test_invalid_query_parameters(client, path):
    assert client.get(path, headers=CUSTOMER).status_code == 422


def test_missing_key_and_role_creation(client):
    assert client.post("/tickets/T-MISSING-001/runs", json={}, headers=CUSTOMER).status_code == 422
    assert (
        post(
            client, "/tickets", {"type": "unknown", "message": "问题"}, headers=OPERATOR
        ).status_code
        == 403
    )
    response = client.get("/does-not-exist")
    assert response.status_code == 404 and response.json()["request_id"]


def test_service_lease_rejects_second_owner_without_disturbing_first(client, storage):
    second = ApplicationService(*storage)
    with pytest.raises(BlockingIOError):
        second.start()
    assert client.get("/health").status_code == 200


def test_same_key_concurrent_start_returns_one_run(client):
    barrier = threading.Barrier(2)

    def attempt(_):
        barrier.wait(5)
        return post(client, "/tickets/T-NOTRECEIVED-002/runs", key="identical")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt, range(2)))
    assert [r.status_code for r in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()


def test_customer_answer_requires_exact_fields_and_pending_bindings(client, storage):
    run_id = start(client, "T-MISSING-001")
    pending = wait_run(client, run_id, "paused")["pending_input"]
    for body in (
        answer(pending, answers={"order_id": "ORD-019", "other": "bad"}),
        {**answer(pending, answers={"order_id": "ORD-019"}), "proposal_hash": "a" * 64},
    ):
        assert post(client, f"/runs/{run_id}/responses", body).status_code == 409
    assert (
        post(
            client, f"/runs/{run_id}/responses", answer(pending, answers={"order_id": "../bad"})
        ).status_code
        == 422
    )
    with read_database(storage[0].path) as db:
        assert db.execute("SELECT count(*) FROM human_inputs").fetchone()[0] == 0
        assert (
            db.execute("SELECT input_revision FROM tickets WHERE id='T-MISSING-001'").fetchone()[0]
            == 1
        )


def test_executor_worker_bound_with_three_independent_tickets(storage):
    repo, settings = storage
    service = ApplicationService(repo, settings, workers=2, clock=lambda: NOW)
    lock, entered, release = threading.Lock(), threading.Event(), threading.Event()
    actual = service.execute
    active = set()

    async def blocked(run_id):
        with lock:
            active.add(run_id)
            if len(active) == 2:
                entered.set()
        assert release.wait(5)
        return await actual(run_id)

    service.execute = blocked
    with TestClient(create_app(settings, service=service)) as client:
        try:
            start(client, key="one")
            start(client, "T-MISSING-001", key="two")
            assert entered.wait(5)
            third = start(client, "T-DELAY-001", key="three")
            with read_database(repo.path) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM execution_jobs WHERE status='running'"
                    ).fetchone()[0]
                    == 2
                )
                assert (
                    db.execute(
                        "SELECT status FROM execution_jobs WHERE run_id=?", (third,)
                    ).fetchone()[0]
                    == "queued"
                )
            with lock:
                assert len(active) == 2
        finally:
            release.set()
