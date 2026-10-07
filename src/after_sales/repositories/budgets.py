"""Atomic reservations and cancellation are independent of graph checkpoints."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime

from after_sales.agents.telemetry import CallLimitExceeded, SchemaRepairExceeded
from after_sales.domain.models import utc_text
from after_sales.repositories.run_store import IncompatibleRun
from after_sales.repositories.sqlite import read_database, transaction
from after_sales.tools.evidence import canonical


class RunCancelled(Exception):
    pass


class ActiveTimeExceeded(CallLimitExceeded):
    pass


class TokenBudgetExceeded(CallLimitExceeded):
    pass


def check_cancelled(connection, run_id):
    # Schema v2 P06 runs have no control row. Existing receipts are checked first by ActionService.
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_control'"
    ).fetchone()
    if table:
        row = connection.execute(
            "SELECT cancel_requested FROM run_control WHERE run_id=?", (run_id,)
        ).fetchone()
        if row and row[0]:
            raise RunCancelled("run cancellation requested")


class BudgetStore:
    def __init__(self, path, run_id, settings):
        self.path, self.run_id, self.settings = path, run_id, settings

    def initialize(self):
        with transaction(self.path) as db:
            db.execute("INSERT INTO run_control(run_id) VALUES (?)", (self.run_id,))
            db.execute("INSERT INTO run_budget(run_id) VALUES (?)", (self.run_id,))

    def active_ms(self, row, now=None):
        value = row["active_ms"]
        if row["segment_started"]:
            now = now or datetime.now(UTC)
            value += (
                max(0, (now - datetime.fromisoformat(row["segment_started"])).total_seconds())
                * 1000
            )
        return value

    def check(self):
        with read_database(self.path) as db:
            check_cancelled(db, self.run_id)
            row = db.execute("SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)).fetchone()
            if row is None:
                raise IncompatibleRun("P07 budget ledger absent; data preserved")
            if self.active_ms(row) >= self.settings.active_time_budget_seconds * 1000:
                raise ActiveTimeExceeded("active time budget reached")

    def remaining_seconds(self):
        with read_database(self.path) as db:
            row = db.execute("SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)).fetchone()
        return max(0, self.settings.active_time_budget_seconds - self.active_ms(row) / 1000)

    def begin(self):
        self.check()
        with transaction(self.path) as db:
            row = db.execute("SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)).fetchone()
            # Charge a crashed open segment through recovery. Human pauses close their segment.
            db.execute(
                "UPDATE run_budget SET active_ms=?,segment_started=? WHERE run_id=?",
                (self.active_ms(row), utc_text(datetime.now(UTC)), self.run_id),
            )

    def end(self):
        with transaction(self.path) as db:
            row = db.execute("SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)).fetchone()
            db.execute(
                "UPDATE run_budget SET active_ms=?,segment_started=NULL WHERE run_id=?",
                (self.active_ms(row), self.run_id),
            )

    def reserve(self, kind, role):
        with transaction(self.path) as db:
            check_cancelled(db, self.run_id)
            budget = db.execute(
                "SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if self.active_ms(budget) >= self.settings.active_time_budget_seconds * 1000:
                raise ActiveTimeExceeded("active time budget reached")
            count = db.execute(
                "SELECT count(*) FROM call_reservations WHERE run_id=? AND kind=?",
                (self.run_id, kind),
            ).fetchone()[0]
            limit = (
                self.settings.max_model_calls if kind == "model" else self.settings.max_tool_calls
            )
            if count >= limit:
                raise CallLimitExceeded(f"{kind} call limit reached")
            hold = self.settings.model_token_reservation if kind == "model" else 0
            charged = db.execute(
                "SELECT coalesce(sum(coalesce(actual_tokens,token_hold)),0) "
                "FROM call_reservations WHERE run_id=?",
                (self.run_id,),
            ).fetchone()[0]
            if charged + hold > self.settings.token_budget:
                raise TokenBudgetExceeded("token reservation budget reached")
            sequence = db.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM call_reservations WHERE run_id=?",
                (self.run_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO call_reservations VALUES (?,?,?,?,'reserved',?,NULL,NULL,0)",
                (self.run_id, sequence, kind, role, hold),
            )
            return sequence

    def settle(self, sequence, status, duration_ms, usage=None):
        # Unknown usage retains its entire hold; no zero-cost assumption after failure/crash.
        actual = usage.get("total_tokens") if usage else None
        if actual is not None and (type(actual) is not int or actual < 0):
            raise ValueError("invalid provider token usage")
        with transaction(self.path) as db:
            changed = db.execute(
                "UPDATE call_reservations SET status=?,duration_ms=?,actual_tokens=?,usage_json=? "
                "WHERE run_id=? AND sequence=? AND status='reserved'",
                (
                    status,
                    duration_ms,
                    actual,
                    canonical(usage) if usage else None,
                    self.run_id,
                    sequence,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("reservation already settled or absent")

    def repair_schema(self):
        with transaction(self.path) as db:
            check_cancelled(db, self.run_id)
            row = db.execute(
                "SELECT schema_repairs FROM run_budget WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if row[0] >= self.settings.proposal_repair_limit:
                raise SchemaRepairExceeded("proposal schema repair limit reached")
            db.execute(
                "UPDATE run_budget SET schema_repairs=schema_repairs+1 WHERE run_id=?",
                (self.run_id,),
            )

    def reserve_review_repair(self):
        with transaction(self.path) as db:
            check_cancelled(db, self.run_id)
            count = db.execute(
                "SELECT review_repairs FROM run_budget WHERE run_id=?", (self.run_id,)
            ).fetchone()[0]
            if count >= self.settings.review_repair_limit:
                return None
            db.execute(
                "UPDATE run_budget SET review_repairs=review_repairs+1 WHERE run_id=?",
                (self.run_id,),
            )
            return count + 1

    def event(self, kind, *, connection=None, **data):
        with nullcontext(connection) if connection else transaction(self.path) as db:
            sequence = db.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM run_events WHERE run_id=?", (self.run_id,)
            ).fetchone()[0]
            row = db.execute("SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)).fetchone()
            value = {
                "sequence": sequence,
                "kind": kind,
                "elapsed_ms": round(self.active_ms(row), 3),
                **data,
            }
            db.execute(
                "INSERT INTO run_events VALUES (?,?,?)", (self.run_id, sequence, canonical(value))
            )
            return value

    def snapshot(self):
        with read_database(self.path) as db:
            row = db.execute("SELECT * FROM run_budget WHERE run_id=?", (self.run_id,)).fetchone()
            calls = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM call_reservations WHERE run_id=? ORDER BY sequence",
                    (self.run_id,),
                )
            ]
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
                    (self.run_id,),
                )
            ]
        return {
            "active_ms": round(self.active_ms(row), 3),
            "schema_repairs": row["schema_repairs"],
            "review_repairs": row["review_repairs"],
            "calls": calls,
            "events": events,
        }

    def cancel(self, reason="operator_requested", *, connection=None):
        with nullcontext(connection) if connection else transaction(self.path) as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (self.run_id,)).fetchone()
            if row is None:
                raise IncompatibleRun("run absent")
            if row[0] in ("completed", "failed", "cancelled"):
                return False
            control = db.execute(
                "SELECT * FROM run_control WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if control is None:
                raise IncompatibleRun("cancellation requires a P07 run")
            if control["cancel_requested"]:
                return False
            db.execute(
                "UPDATE run_control SET cancel_requested=1,cancel_reason=? WHERE run_id=?",
                (reason, self.run_id),
            )
            self.event("cancel_requested", connection=db, role="application", reason=reason)
        return True
