"""Initialize fictional data atomically; repeated seeding preserves business changes."""

import hashlib
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

from after_sales.domain.models import DemoDataset, utc_text
from after_sales.repositories.errors import DatasetConflict, UnsafeDatabase
from after_sales.repositories.migrations import APPLICATION_ID, apply_migrations
from after_sales.repositories.sqlite import connect, transaction

SEED_TABLES = (
    "customers",
    "products",
    "orders",
    "order_items",
    "tracking_events",
    "delivery_proofs",
    "policies",
    "tickets",
    "ticket_messages",
    "after_sales_history",
)
RESET_TABLES = (
    "action_events",
    "human_inputs",
    "pending_inputs",
    "proposal_plans",
    "workflow_runtime",
    "after_sales_history",
    "action_ledger",
    "runs",
    "ticket_messages",
    "tickets",
    "delivery_proofs",
    "tracking_events",
    "order_items",
    "orders",
    "products",
    "policies",
    "customers",
    "dataset_metadata",
)


def load_demo_dataset() -> DemoDataset:
    return DemoDataset.model_validate_json(
        files("after_sales.data").joinpath("demo-v1.json").read_text()
    )


def dataset_hash(dataset: DemoDataset) -> str:
    canonical = json.dumps(dataset.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    if table not in SEED_TABLES:
        raise ValueError("unsupported seed table")
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
    )


def _write_dataset(connection: sqlite3.Connection, dataset: DemoDataset) -> None:
    for customer in dataset.customers:
        _insert(connection, "customers", customer.model_dump(mode="json"))
    for product in dataset.products:
        _insert(connection, "products", product.model_dump(mode="json"))
    for order in dataset.orders:
        _insert(connection, "orders", order.model_dump(mode="json", exclude={"items"}))
        for item in order.items:
            _insert(
                connection, "order_items", {"order_id": order.id, **item.model_dump(mode="json")}
            )
    for table, records in (
        ("tracking_events", dataset.tracking_events),
        ("delivery_proofs", dataset.delivery_proofs),
        ("after_sales_history", dataset.after_sales_history),
    ):
        for record in records:
            _insert(connection, table, record.model_dump(mode="json"))
    for policy in dataset.policies:
        _insert(
            connection,
            "policies",
            {
                "id": policy.id,
                "version": policy.version,
                "payload": policy.model_dump_json(),
            },
        )
    for ticket in dataset.tickets:
        _insert(connection, "tickets", ticket.model_dump(mode="json", exclude={"messages"}))
        for message in ticket.messages:
            _insert(connection, "ticket_messages", message.model_dump(mode="json"))
    connection.execute(
        "INSERT INTO dataset_metadata VALUES (1,?,?,?,?,?)",
        (
            dataset.dataset_id,
            dataset.version,
            dataset.fixture_seed,
            utc_text(dataset.as_of_time),
            dataset_hash(dataset),
        ),
    )


def _verify_reset_target(path: Path, dataset: DemoDataset) -> None:
    if not path.is_file():
        raise UnsafeDatabase("reset requires an existing, marked demo business database")
    with connect(path, readonly=True) as connection:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise UnsafeDatabase("reset refused: file is not an after-sales demo database")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if "dataset_metadata" not in tables:
            raise UnsafeDatabase("reset refused: demo dataset marker is absent")
        row = connection.execute(
            "SELECT dataset_id FROM dataset_metadata WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != dataset.dataset_id:
            raise UnsafeDatabase("reset refused: demo dataset marker does not match")


def seed_demo(
    path: Path, *, reset: bool = False, dataset: DemoDataset | None = None
) -> dict[str, object]:
    dataset = dataset or load_demo_dataset()
    if reset:
        _verify_reset_target(path, dataset)
    with transaction(path) as connection:
        schema_version = apply_migrations(connection)
        metadata = connection.execute("SELECT * FROM dataset_metadata WHERE singleton=1").fetchone()
        changed = metadata is None or reset
        if reset:
            # Recheck ownership inside the write transaction before deleting known demo rows.
            if metadata is None or metadata["dataset_id"] != dataset.dataset_id:
                raise UnsafeDatabase("reset refused: demo dataset marker changed")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables != set(RESET_TABLES) | {"schema_migrations"}:
                raise UnsafeDatabase("reset refused: database contains unrecognized tables")
            for table in RESET_TABLES:
                connection.execute(f"DELETE FROM {table}")
        elif metadata is not None:
            if metadata["dataset_hash"] != dataset_hash(dataset):
                raise DatasetConflict(
                    "fixture version differs; reset the marked demo database explicitly"
                )
        elif any(
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in SEED_TABLES
        ):
            raise DatasetConflict("unmarked business data exists; refusing to overwrite it")
        if changed:
            _write_dataset(connection, dataset)
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in SEED_TABLES
        }
    return {
        "ok": True,
        "dataset": dataset.dataset_id,
        "version": dataset.version,
        "fixture_seed": dataset.fixture_seed,
        "as_of_time": utc_text(dataset.as_of_time),
        "schema_version": schema_version,
        "changed": changed,
        "reset": reset,
        "counts": counts,
    }
