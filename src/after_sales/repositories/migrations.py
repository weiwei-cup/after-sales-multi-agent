"""Versioned statements executed inside the caller's transaction."""

import hashlib
import sqlite3

from after_sales.repositories.errors import UnsafeDatabase

APPLICATION_ID = 0x41534D41

SCHEMA_V1 = (
    "CREATE TABLE customers (id TEXT PRIMARY KEY, display_name TEXT NOT NULL)",
    """CREATE TABLE products (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN ('apparel','electronics','hygiene'))
    )""",
    """CREATE TABLE orders (
        id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
        paid_cents INTEGER NOT NULL CHECK(typeof(paid_cents)='integer' AND paid_cents>=0),
        refunded_cents INTEGER NOT NULL CHECK(typeof(refunded_cents)='integer'
            AND refunded_cents>=0 AND refunded_cents<=paid_cents),
        status TEXT NOT NULL CHECK(status IN ('shipped','delivered','lost','cancelled')),
        created_at TEXT NOT NULL CHECK(substr(created_at,-1)='Z'),
        expected_delivery_at TEXT, received_at TEXT,
        version INTEGER NOT NULL CHECK(version>0), UNIQUE(id, customer_id),
        CHECK(expected_delivery_at IS NULL OR substr(expected_delivery_at,-1)='Z'),
        CHECK(received_at IS NULL OR substr(received_at,-1)='Z')
    )""",
    """CREATE TABLE order_items (
        order_id TEXT NOT NULL REFERENCES orders(id),
        product_id TEXT NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
        unit_price_cents INTEGER NOT NULL CHECK(typeof(unit_price_cents)='integer'
            AND unit_price_cents>=0),
        unopened INTEGER CHECK(unopened IS NULL OR unopened IN (0,1)),
        PRIMARY KEY(order_id,product_id)
    )""",
    """CREATE TABLE tracking_events (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
        event_type TEXT NOT NULL, occurred_at TEXT NOT NULL CHECK(substr(occurred_at,-1)='Z'),
        description TEXT NOT NULL, source TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0)
    )""",
    """CREATE TABLE delivery_proofs (
        order_id TEXT PRIMARY KEY REFERENCES orders(id),
        proof_status TEXT NOT NULL CHECK(proof_status IN ('present','missing','unknown')),
        description TEXT NOT NULL, observed_at TEXT NOT NULL CHECK(substr(observed_at,-1)='Z'),
        source TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0)
    )""",
    """CREATE TABLE policies (
        id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
        payload TEXT NOT NULL CHECK(json_valid(payload)), PRIMARY KEY(id,version)
    )""",
    """CREATE TABLE tickets (
        id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
        supplied_order_id TEXT, order_id TEXT,
        type TEXT NOT NULL CHECK(type IN
            ('logistics_delay','delivered_not_received','return_request','unknown')),
        status TEXT NOT NULL CHECK(status IN ('new','processing','waiting_customer',
            'waiting_review','waiting_return','resolved','handed_off')),
        created_at TEXT NOT NULL CHECK(substr(created_at,-1)='Z'),
        input_revision INTEGER NOT NULL CHECK(input_revision>0),
        version INTEGER NOT NULL CHECK(version>0),
        FOREIGN KEY(order_id,customer_id) REFERENCES orders(id,customer_id),
        CHECK(order_id IS NULL OR supplied_order_id IS NULL OR order_id=supplied_order_id)
    )""",
    """CREATE TABLE ticket_messages (
        id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
        role TEXT NOT NULL CHECK(role IN ('customer','staff')), content TEXT NOT NULL,
        created_at TEXT NOT NULL CHECK(substr(created_at,-1)='Z')
    )""",
    """CREATE TABLE after_sales_history (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
        type TEXT NOT NULL CHECK(type IN
            ('open_logistics_case','create_return_request','issue_mock_refund')),
        status TEXT NOT NULL CHECK(status IN ('active','completed','cancelled')),
        amount_cents INTEGER, created_at TEXT NOT NULL CHECK(substr(created_at,-1)='Z'),
        version INTEGER NOT NULL CHECK(version>0),
        CHECK((type='issue_mock_refund' AND typeof(amount_cents)='integer' AND amount_cents>0)
            OR (type!='issue_mock_refund' AND amount_cents IS NULL))
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
        thread_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN
            ('queued','running','paused','completed','failed','interrupted','cancelled')),
        as_of_time TEXT NOT NULL CHECK(substr(as_of_time,-1)='Z'),
        created_at TEXT NOT NULL CHECK(substr(created_at,-1)='Z'),
        workflow_version INTEGER NOT NULL CHECK(workflow_version>0)
    )""",
    """CREATE TABLE dataset_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), dataset_id TEXT NOT NULL,
        version TEXT NOT NULL, fixture_seed INTEGER NOT NULL,
        as_of_time TEXT NOT NULL CHECK(substr(as_of_time,-1)='Z'), dataset_hash TEXT NOT NULL
    )""",
    "CREATE INDEX idx_tracking_order_time ON tracking_events(order_id,occurred_at,id)",
    "CREATE INDEX idx_ticket_customer ON tickets(customer_id,id)",
)

SCHEMA_V2 = (
    """CREATE TABLE workflow_runtime (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), workflow_version TEXT NOT NULL,
        schema_version TEXT NOT NULL, model_mode TEXT NOT NULL,
        checkpoint_path TEXT NOT NULL, limits_json TEXT NOT NULL CHECK(json_valid(limits_json)),
        runtime_json TEXT NOT NULL CHECK(json_valid(runtime_json)), terminal_error INTEGER NOT
        NULL DEFAULT 0
    )""",
    """CREATE TABLE proposal_plans (
        run_id TEXT NOT NULL REFERENCES runs(id), proposal_revision INTEGER NOT NULL
        CHECK(proposal_revision>0),
        input_revision INTEGER NOT NULL CHECK(input_revision>0), proposal_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        active INTEGER NOT NULL CHECK(active IN (0,1)), PRIMARY KEY(run_id,proposal_revision)
    )""",
    "CREATE UNIQUE INDEX idx_active_plan ON proposal_plans(run_id) WHERE active=1",
    """CREATE TABLE pending_inputs (
        pending_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        status TEXT NOT NULL CHECK(status IN ('open','consumed','invalidated','superseded'))
    )""",
    "CREATE UNIQUE INDEX idx_open_pending ON pending_inputs(run_id) WHERE status='open'",
    """CREATE TABLE human_inputs (
        pending_id TEXT PRIMARY KEY REFERENCES pending_inputs(pending_id),
        envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)),
        ticket_json TEXT NOT NULL CHECK(json_valid(ticket_json)),
        accepted_at TEXT NOT NULL CHECK(substr(accepted_at,-1)='Z')
    )""",
    """CREATE TABLE action_ledger (
        operation_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        ticket_id TEXT NOT NULL REFERENCES tickets(id), action_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
        committed_at TEXT NOT NULL CHECK(substr(committed_at,-1)='Z')
    )""",
    """CREATE TABLE action_events (
        operation_key TEXT PRIMARY KEY REFERENCES action_ledger(operation_key),
        event_type TEXT NOT NULL CHECK(event_type='action_committed'),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
    )""",
    "ALTER TABLE after_sales_history ADD COLUMN operation_key TEXT REFERENCES "
    "action_ledger(operation_key)",
    "CREATE UNIQUE INDEX idx_history_operation ON after_sales_history(operation_key)",
)

MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2}


def ensure_application_database(connection: sqlite3.Connection) -> None:
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if application_id != APPLICATION_ID and (application_id != 0 or tables):
        raise UnsafeDatabase("refusing to modify a database owned by another application")
    if not tables and application_id == 0:
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")


def apply_migrations(connection: sqlite3.Connection) -> int:
    ensure_application_database(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
    )
    applied = dict(connection.execute("SELECT version,checksum FROM schema_migrations"))
    if set(applied) - set(MIGRATIONS):
        raise UnsafeDatabase("database schema is newer than this application")
    for number, statements in MIGRATIONS.items():
        checksum = hashlib.sha256("\n".join(statements).encode()).hexdigest()
        if number in applied:
            if applied[number] != checksum:
                raise UnsafeDatabase("migration checksum mismatch")
            continue
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (number, checksum))
    return max(MIGRATIONS)
