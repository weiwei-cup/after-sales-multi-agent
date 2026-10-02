"""Transactions and read-only, customer-scoped business queries."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from after_sales.domain.models import (
    AfterSalesRecord,
    DeliveryProof,
    Order,
    Policy,
    Product,
    Ticket,
    TicketType,
    TrackingEvent,
)
from after_sales.repositories.errors import (
    DatabaseNotInitialized,
    OrderAccessDenied,
    RecordNotFound,
    UnsafeDatabase,
)
from after_sales.repositories.migrations import APPLICATION_ID, MIGRATIONS, apply_migrations


@contextmanager
def connect(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = path.resolve()
    if readonly:
        if not path.is_file():
            raise DatabaseNotInitialized("business database is absent; run after-sales seed")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def migrate(path: Path) -> int:
    with transaction(path) as connection:
        return apply_migrations(connection)


@contextmanager
def read_database(path: Path) -> Iterator[sqlite3.Connection]:
    with connect(path, readonly=True) as connection:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise UnsafeDatabase("file is not an after-sales business database")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if "schema_migrations" not in tables:
            raise DatabaseNotInitialized("schema is absent; run after-sales seed")
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        if (
            not versions
            or min(versions) != 1
            or versions != set(range(1, max(versions) + 1))
            or not versions.issubset(MIGRATIONS)
        ):
            raise UnsafeDatabase("unsupported business database schema")
        connection.execute("BEGIN")
        yield connection


def _ticket(connection: sqlite3.Connection, ticket_id: str) -> Ticket:
    row = connection.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        raise RecordNotFound("ticket not found")
    messages = [
        dict(message)
        for message in connection.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at,id", (ticket_id,)
        )
    ]
    return Ticket.model_validate({**dict(row), "messages": messages})


def _order(connection: sqlite3.Connection, order_id: str, customer_id: str) -> Order:
    row = connection.execute(
        "SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, customer_id)
    ).fetchone()
    if row is None:
        if connection.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            raise OrderAccessDenied("order unavailable for this customer")
        raise RecordNotFound("order not found")
    items = [
        dict(item)
        for item in connection.execute(
            "SELECT product_id,quantity,unit_price_cents,unopened FROM order_items "
            "WHERE order_id=? ORDER BY product_id",
            (order_id,),
        )
    ]
    return Order.model_validate({**dict(row), "items": items})


def _policies(
    connection: sqlite3.Connection,
    *,
    intent: TicketType | None = None,
    as_of_time: datetime | None = None,
    product_ids: tuple[str, ...] | None = None,
) -> list[Policy]:
    policies = [
        Policy.model_validate_json(row[0])
        for row in connection.execute("SELECT payload FROM policies ORDER BY id,version")
    ]
    return [
        policy
        for policy in policies
        if (intent is None or intent in policy.scope)
        and (
            as_of_time is None
            or (
                policy.effective_from <= as_of_time
                and (policy.effective_to is None or as_of_time < policy.effective_to)
            )
        )
        and (
            product_ids is None
            or not policy.scope_product_ids
            or bool(set(product_ids) & set(policy.scope_product_ids))
        )
    ]


class BusinessRepository:
    def __init__(self, path: Path):
        self.path = path

    def metadata(self) -> dict[str, object]:
        with read_database(self.path) as connection:
            row = connection.execute("SELECT * FROM dataset_metadata WHERE singleton=1").fetchone()
            if row is None:
                raise DatabaseNotInitialized("demo data is absent; run after-sales seed")
            return dict(row)

    def list_tickets(self, customer_id: str | None = None) -> list[Ticket]:
        with read_database(self.path) as connection:
            if customer_id is None:
                rows = connection.execute("SELECT id FROM tickets ORDER BY id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT id FROM tickets WHERE customer_id=? ORDER BY id", (customer_id,)
                ).fetchall()
            return [_ticket(connection, row[0]) for row in rows]

    def get_ticket(self, ticket_id: str) -> Ticket:
        with read_database(self.path) as connection:
            return _ticket(connection, ticket_id)

    def get_order(self, order_id: str, *, customer_id: str) -> Order:
        with read_database(self.path) as connection:
            return _order(connection, order_id, customer_id)

    def get_order_products(self, order_id: str, *, customer_id: str) -> list[Product]:
        with read_database(self.path) as connection:
            _order(connection, order_id, customer_id)
            return [
                Product.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT p.* FROM products p JOIN order_items i ON i.product_id=p.id "
                    "WHERE i.order_id=? ORDER BY p.id",
                    (order_id,),
                )
            ]

    def get_tracking(self, order_id: str, *, customer_id: str) -> list[TrackingEvent]:
        with read_database(self.path) as connection:
            _order(connection, order_id, customer_id)
            return [
                TrackingEvent.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT * FROM tracking_events WHERE order_id=? ORDER BY occurred_at,id",
                    (order_id,),
                )
            ]

    def get_delivery_proof(self, order_id: str, *, customer_id: str) -> DeliveryProof | None:
        with read_database(self.path) as connection:
            _order(connection, order_id, customer_id)
            row = connection.execute(
                "SELECT * FROM delivery_proofs WHERE order_id=?", (order_id,)
            ).fetchone()
            return DeliveryProof.model_validate(dict(row)) if row else None

    def get_history(self, order_id: str, *, customer_id: str) -> list[AfterSalesRecord]:
        with read_database(self.path) as connection:
            _order(connection, order_id, customer_id)
            return [
                AfterSalesRecord.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT id,order_id,type,status,amount_cents,created_at,version "
                    "FROM after_sales_history WHERE order_id=? ORDER BY created_at,id",
                    (order_id,),
                )
            ]

    def get_policy(self, policy_id: str, version: int) -> Policy:
        with read_database(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM policies WHERE id=? AND version=?", (policy_id, version)
            ).fetchone()
            if row is None:
                raise RecordNotFound("policy version not found")
            return Policy.model_validate_json(row[0])

    def list_policies(
        self,
        *,
        intent: TicketType | None = None,
        as_of_time: datetime | None = None,
        product_ids: tuple[str, ...] | None = None,
    ) -> list[Policy]:
        with read_database(self.path) as connection:
            return _policies(
                connection, intent=intent, as_of_time=as_of_time, product_ids=product_ids
            )

    def ticket_view(self, ticket_id: str) -> dict[str, object]:
        """Operator demo view: resolve only the ticket's own verified order."""
        with read_database(self.path) as connection:
            ticket = _ticket(connection, ticket_id)
            metadata = connection.execute(
                "SELECT * FROM dataset_metadata WHERE singleton=1"
            ).fetchone()
            if metadata is None:
                raise DatabaseNotInitialized("demo data is absent; run after-sales seed")
            order = None
            reference_status = "missing_reference"
            if ticket.supplied_order_id:
                try:
                    candidate = _order(connection, ticket.supplied_order_id, ticket.customer_id)
                    order = candidate if ticket.order_id == candidate.id else None
                    reference_status = "verified" if order else "not_verified"
                except OrderAccessDenied:
                    reference_status = "not_owned"
                except RecordNotFound:
                    reference_status = "not_found"
            order_id = order.id if order else None
            products = [
                Product.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT p.* FROM products p JOIN order_items i ON i.product_id=p.id "
                    "WHERE i.order_id=? ORDER BY p.id",
                    (order_id,),
                )
            ]
            tracking = [
                TrackingEvent.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT * FROM tracking_events WHERE order_id=? ORDER BY occurred_at,id",
                    (order_id,),
                )
            ]
            proof_row = connection.execute(
                "SELECT * FROM delivery_proofs WHERE order_id=?", (order_id,)
            ).fetchone()
            history = [
                AfterSalesRecord.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT id,order_id,type,status,amount_cents,created_at,version "
                    "FROM after_sales_history WHERE order_id=? ORDER BY created_at,id",
                    (order_id,),
                )
            ]
            as_of = datetime.fromisoformat(metadata["as_of_time"])
            policies = _policies(
                connection,
                intent=ticket.type,
                as_of_time=as_of,
                product_ids=tuple(product.id for product in products),
            )
            return {
                "dataset_version": metadata["version"],
                "as_of_time": metadata["as_of_time"],
                "display_timezone": "Asia/Shanghai",
                "ticket": ticket.model_dump(mode="json"),
                "order_reference_status": reference_status,
                "order": order.model_dump(mode="json") if order else None,
                "products": [product.model_dump(mode="json") for product in products],
                "tracking_events": [event.model_dump(mode="json") for event in tracking],
                "delivery_proof": DeliveryProof.model_validate(dict(proof_row)).model_dump(
                    mode="json"
                )
                if proof_row
                else None,
                "after_sales_history": [record.model_dump(mode="json") for record in history],
                "policy_candidates": [policy.model_dump(mode="json") for policy in policies],
            }
