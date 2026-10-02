import sqlite3
from datetime import UTC, datetime

import pytest

from after_sales.domain.models import TicketType
from after_sales.repositories import seed as seed_module
from after_sales.repositories.errors import (
    DatabaseNotInitialized,
    DatasetConflict,
    OrderAccessDenied,
    RecordNotFound,
    UnsafeDatabase,
)
from after_sales.repositories.seed import load_demo_dataset, seed_demo
from after_sales.repositories.sqlite import BusinessRepository, connect, migrate, transaction

pytestmark = pytest.mark.integration


@pytest.fixture
def business_db(tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    return path


def snapshot(path):
    with connect(path, readonly=True) as connection:
        return "\n".join(connection.iterdump())


def test_migrations_are_repeatable_and_foreign_keys_are_enabled(tmp_path):
    path = tmp_path / "business.sqlite"
    assert migrate(path) == 3
    assert migrate(path) == 3
    with connect(path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO order_items VALUES (?,?,?,?,?)", ("absent", "absent", 1, 1, 1)
            )


def test_seed_is_repeatable_and_preserves_business_changes(business_db):
    with transaction(business_db) as connection:
        connection.execute("UPDATE tickets SET status='processing' WHERE id='T-DELAY-001'")
    before = snapshot(business_db)
    report = seed_demo(business_db)
    assert report["changed"] is False
    assert report["counts"]["orders"] == 30
    assert report["counts"]["tickets"] == 20
    assert snapshot(business_db) == before
    assert BusinessRepository(business_db).get_ticket("T-DELAY-001").status == "processing"


def test_two_fresh_seeds_are_identical(tmp_path):
    first, second = tmp_path / "one.sqlite", tmp_path / "two.sqlite"
    assert seed_demo(first) == seed_demo(second)
    assert snapshot(first) == snapshot(second)


def test_seed_failure_rolls_back_schema_and_rows(tmp_path, monkeypatch):
    path = tmp_path / "business.sqlite"
    original = seed_module._insert

    def fail_midway(connection, table, values):
        if table == "orders" and values["id"] == "ORD-002":
            raise RuntimeError("injected initialization failure")
        original(connection, table, values)

    monkeypatch.setattr(seed_module, "_insert", fail_midway)
    with pytest.raises(RuntimeError, match="injected"):
        seed_demo(path)
    with connect(path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[
                0
            ]
            == 0
        )
    monkeypatch.setattr(seed_module, "_insert", original)
    assert seed_demo(path)["counts"]["orders"] == 30


def test_database_enforces_customer_order_link_and_money_constraints(business_db):
    with connect(business_db) as connection:
        for sql, parameters in (
            ("UPDATE tickets SET order_id=? WHERE id=?", ("ORD-008", "T-CROSS-001")),
            ("UPDATE orders SET refunded_cents=paid_cents+1 WHERE id=?", ("ORD-001",)),
            ("UPDATE orders SET paid_cents=? WHERE id=?", (-1, "ORD-001")),
            ("UPDATE orders SET paid_cents=? WHERE id=?", (10.5, "ORD-001")),
            ("UPDATE orders SET created_at=? WHERE id=?", ("2026-10-02T12:00:00", "ORD-001")),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, parameters)


@pytest.mark.parametrize(
    "method", ["get_order", "get_tracking", "get_delivery_proof", "get_history"]
)
def test_all_order_related_queries_require_customer_ownership(business_db, method):
    repository = BusinessRepository(business_db)
    with pytest.raises(OrderAccessDenied):
        getattr(repository, method)("ORD-008", customer_id="CUST-A")


def test_missing_order_differs_from_cross_customer_order(business_db):
    with pytest.raises(RecordNotFound):
        BusinessRepository(business_db).get_order("ORD-DOES-NOT-EXIST", customer_id="CUST-A")


def test_ticket_view_does_not_expose_another_customers_order(business_db):
    view = BusinessRepository(business_db).ticket_view("T-CROSS-001")
    assert view["order_reference_status"] == "not_owned"
    assert view["order"] is None
    assert view["tracking_events"] == []
    assert view["products"] == []
    assert view["delivery_proof"] is None
    assert view["after_sales_history"] == []


def test_three_ticket_types_and_missing_reference_have_queryable_facts(business_db):
    repository = BusinessRepository(business_db)
    delay = repository.ticket_view("T-DELAY-001")
    missing = repository.ticket_view("T-NOTRECEIVED-001")
    returns = repository.ticket_view("T-RETURN-001")
    assert delay["order"]["status"] == "shipped"
    assert missing["delivery_proof"]["proof_status"] == "missing"
    assert returns["order"]["items"][0]["unopened"] is True
    assert repository.ticket_view("T-MISSING-001")["order_reference_status"] == "missing_reference"
    assert repository.get_delivery_proof("ORD-014", customer_id="CUST-B").proof_status == "unknown"


def test_policy_periods_use_fixed_time_and_preserve_conflicts(business_db):
    repository = BusinessRepository(business_db)
    as_of = datetime(2026, 10, 2, 4, tzinfo=UTC)
    active = repository.list_policies(intent=TicketType.RETURN, as_of_time=as_of)
    assert {(p.id, p.version) for p in active} == {
        ("RETURN-STANDARD", 1),
        ("RETURN-CONFLICT", 1),
    }
    at_next_version = repository.list_policies(
        intent=TicketType.RETURN, as_of_time=datetime(2026, 11, 1, tzinfo=UTC)
    )
    assert ("RETURN-STANDARD", 2) in {(p.id, p.version) for p in at_next_version}
    assert ("RETURN-STANDARD", 1) not in {(p.id, p.version) for p in at_next_version}
    regular = repository.ticket_view("T-RETURN-001")["policy_candidates"]
    conflict = repository.ticket_view("T-CONFLICT-001")["policy_candidates"]
    assert [p["id"] for p in regular] == ["RETURN-STANDARD"]
    assert {p["id"] for p in conflict} == {"RETURN-STANDARD", "RETURN-CONFLICT"}
    assert repository.get_policy("RETURN-STANDARD", 2).conditions.window_hours == 120
    with pytest.raises(RecordNotFound):
        repository.get_policy("RETURN-STANDARD", 99)


def test_existing_history_and_partial_refund_are_persisted(business_db):
    repository = BusinessRepository(business_db)
    assert (
        repository.get_history("ORD-020", customer_id="CUST-B")[0].type == "create_return_request"
    )
    assert repository.get_history("ORD-023", customer_id="CUST-B")[0].amount_cents == 1000


def test_read_before_seed_does_not_create_a_database(tmp_path):
    path = tmp_path / "absent" / "business.sqlite"
    with pytest.raises(DatabaseNotInitialized):
        BusinessRepository(path).list_tickets()
    assert not path.parent.exists()


def test_reset_restores_only_a_marked_demo_database(business_db):
    before = snapshot(business_db)
    with transaction(business_db) as connection:
        connection.execute("UPDATE tickets SET status='processing' WHERE id='T-DELAY-001'")
    report = seed_demo(business_db, reset=True)
    assert report["reset"] is True
    assert snapshot(business_db) == before


def test_reset_does_not_create_or_modify_unrelated_database(tmp_path):
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(UnsafeDatabase):
        seed_demo(absent, reset=True)
    assert not absent.exists()
    unrelated = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE notes (content TEXT)")
        connection.execute("INSERT INTO notes VALUES ('keep me')")
    before = unrelated.read_bytes()
    with pytest.raises(UnsafeDatabase):
        seed_demo(unrelated, reset=True)
    with pytest.raises(UnsafeDatabase):
        seed_demo(unrelated)
    assert unrelated.read_bytes() == before


def test_reset_refuses_unmarked_or_extended_database(tmp_path, business_db):
    unmarked = tmp_path / "unmarked.sqlite"
    migrate(unmarked)
    with pytest.raises(UnsafeDatabase):
        seed_demo(unmarked, reset=True)
    with transaction(business_db) as connection:
        connection.execute("CREATE TABLE notes (content TEXT)")
        connection.execute("INSERT INTO notes VALUES ('keep me')")
    before = snapshot(business_db)
    with pytest.raises(UnsafeDatabase, match="unrecognized"):
        seed_demo(business_db, reset=True)
    assert snapshot(business_db) == before


def test_reset_failure_rolls_back_existing_data(business_db, monkeypatch):
    before = snapshot(business_db)

    def fail_write(*args):
        raise RuntimeError("injected reset failure")

    monkeypatch.setattr(seed_module, "_write_dataset", fail_write)
    with pytest.raises(RuntimeError, match="injected"):
        seed_demo(business_db, reset=True)
    assert snapshot(business_db) == before


def test_changed_fixture_and_migration_checksum_cannot_be_silently_applied(business_db):
    data = load_demo_dataset().model_dump(mode="json")
    data["version"] = "demo-v2"
    changed = type(load_demo_dataset()).model_validate(data)
    before = snapshot(business_db)
    with pytest.raises(DatasetConflict):
        seed_demo(business_db, dataset=changed)
    assert snapshot(business_db) == before
    with transaction(business_db) as connection:
        connection.execute("UPDATE schema_migrations SET checksum='changed'")
    with pytest.raises(UnsafeDatabase, match="checksum"):
        migrate(business_db)


def test_v1_migration_preserves_existing_business_data(tmp_path, monkeypatch):
    from after_sales.repositories.migrations import MIGRATIONS

    path = tmp_path / "legacy.sqlite"
    with monkeypatch.context() as patch:
        patch.delitem(MIGRATIONS, 2)
        patch.delitem(MIGRATIONS, 3)
        seed_demo(path)
    repository = BusinessRepository(path)
    original = repository.get_ticket("T-RETURN-001")
    assert repository.get_order("ORD-005", customer_id="CUST-B").id == "ORD-005"
    assert migrate(path) == 3
    assert repository.get_ticket("T-RETURN-001") == original
    assert seed_demo(path)["changed"] is False
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM workflow_runtime").fetchone()[0] == 0
        assert "operation_key" in {
            r[1] for r in db.execute("PRAGMA table_info(after_sales_history)")
        }
