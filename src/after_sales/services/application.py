"""Durable HTTP admission, scoped queries and a bounded local thread executor."""

import asyncio
import fcntl
import json
import logging
import threading
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter

from after_sales.agents.contracts import PendingInput, ResumeInput
from after_sales.agents.review import digest
from after_sales.api.contracts import Accepted, EventPage, EventView, RunView, TicketView
from after_sales.domain.models import Identifier, Ticket, utc_text
from after_sales.repositories.budgets import BudgetStore, RunCancelled
from after_sales.repositories.run_store import DurableInputConflict, validate_bindings
from after_sales.repositories.sqlite import _ticket, migrate, read_database, transaction
from after_sales.services.public import pending_view, redact, result_view
from after_sales.services.workflows import WorkflowService
from after_sales.tools.evidence import canonical
from after_sales.workflows.parallel import ParallelReviewRun


class ApplicationError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def conflict(message="当前任务、待办或版本不允许此操作。"):
    return ApplicationError(409, "CONFLICT", message)


class ApplicationService:
    def __init__(
        self,
        repository,
        settings,
        *,
        workers=1,
        capacity=32,
        clock=None,
        execute=None,
        **runtime_options,
    ):
        if not 1 <= workers <= 4 or not 1 <= capacity <= 1000:
            raise ValueError("executor workers must be 1–4 and queue capacity 1–1000")
        if settings.model_mode != "scripted":
            raise ValueError("P08 supports scripted model only")
        self.repository, self.settings = repository, settings
        self.workers, self.capacity = workers, capacity
        self.clock = clock or (lambda: datetime.now(UTC))
        self.workflow = WorkflowService(repository, settings, clock=self.clock, **runtime_options)
        self.execute = execute or self.workflow.execute
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        self.lease = None

    def start(self):
        path = self.repository.path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lease = path.with_suffix(path.suffix + ".http.lock").open("a")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            migrate(path)
            self.repository.metadata()  # Require explicit seed; never reset existing data.
            with transaction(path) as db:
                # Only HTTP-owned executions are reconciled; CLI ownership is independent.
                db.execute(
                    "UPDATE runs SET status='interrupted' WHERE status='running' "
                    "AND id IN (SELECT run_id FROM execution_jobs WHERE status='running')"
                )
                db.execute(
                    "UPDATE execution_jobs SET status='interrupted', "
                    "error_code='PROCESS_INTERRUPTED' WHERE status='running'"
                )
            self.stop.clear()
            for i in range(self.workers):
                thread = threading.Thread(target=self._worker, name=f"after-sales-job-{i}")
                self.threads.append(thread)
                thread.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        # Stop admission/claiming, allow finite current work to finish, then release ownership.
        self.stop.set()
        self.wake.set()
        for thread in self.threads:
            thread.join()
        self.threads.clear()
        if self.lease:
            self.lease.close()
            self.lease = None

    def _worker(self):
        while not self.stop.is_set():
            with transaction(self.repository.path) as db:
                # A cancellation signal is admitted even when the work queue is full.
                # Once a slot is available, project idle paused/interrupted cancellation.
                idle_cancel = db.execute(
                    "SELECT r.id FROM runs r JOIN run_control c ON c.run_id=r.id "
                    "WHERE c.cancel_requested=1 AND r.status IN ('paused','interrupted') "
                    "AND NOT EXISTS (SELECT 1 FROM execution_jobs j WHERE j.run_id=r.id "
                    "AND j.status IN ('queued','running')) "
                    "AND NOT EXISTS (SELECT 1 FROM execution_jobs j WHERE j.run_id=r.id "
                    "AND j.intent='cancel' AND j.status='interrupted') LIMIT 1"
                ).fetchone()
                queued = db.execute(
                    "SELECT count(*) FROM execution_jobs WHERE status='queued'"
                ).fetchone()[0]
                if idle_cancel and queued < self.capacity and not self.stop.is_set():
                    self._queue(db, idle_cancel[0], intent="cancel")
                row = db.execute(
                    "SELECT id,run_id FROM execution_jobs WHERE status='queued' "
                    "ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row and not self.stop.is_set():
                    db.execute("UPDATE execution_jobs SET status='running' WHERE id=?", (row[0],))
                else:
                    row = None
            if not row:
                self.wake.wait(0.2)
                self.wake.clear()
                continue
            try:
                report = asyncio.run(self.execute(row[1]))
                projection = result_view(report).model_dump(mode="json")
                with transaction(self.repository.path) as db:
                    db.execute(
                        "INSERT INTO api_run_results VALUES (?,?) ON CONFLICT(run_id) "
                        "DO UPDATE SET payload_json=excluded.payload_json",
                        (row[1], canonical(projection)),
                    )
                    db.execute("UPDATE execution_jobs SET status='done' WHERE id=?", (row[0],))
            except Exception as error:
                logging.getLogger(__name__).error(
                    "job_interrupted job_id=%s run_id=%s code=%s",
                    row[0],
                    row[1],
                    type(error).__name__,
                )
                # Preserve checkpoints and accepted input. No stack, secret or raw prompt in DTO.
                with transaction(self.repository.path) as db:
                    db.execute(
                        "UPDATE execution_jobs SET status='interrupted',error_code=? WHERE id=?",
                        (type(error).__name__, row[0]),
                    )
                    db.execute(
                        "UPDATE runs SET status='interrupted' WHERE id=? AND "
                        "status NOT IN ('completed','failed','cancelled')",
                        (row[1],),
                    )

    def _ticket(self, db, ticket_id, principal):
        row = db.execute("SELECT customer_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row or (principal.role == "customer" and row[0] != principal.actor_id):
            raise ApplicationError(404, "NOT_ACCESSIBLE", "工单不存在或不可访问。")
        return _ticket(db, ticket_id)

    def _run(self, db, run_id, principal):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ApplicationError(404, "NOT_ACCESSIBLE", "运行不存在或不可访问。")
        self._ticket(db, row["ticket_id"], principal)
        return row

    def _idempotent(self, principal, scope, key, payload, operation):
        fingerprint = digest(payload)
        try:
            with transaction(self.repository.path) as db:
                old = db.execute(
                    "SELECT payload_hash,response_json FROM request_idempotency "
                    "WHERE actor_id=? AND scope=? AND request_key=?",
                    (principal.actor_id, scope, key),
                ).fetchone()
                if old:
                    if old[0] != fingerprint:
                        raise ApplicationError(
                            409, "IDEMPOTENCY_CONFLICT", "同一幂等键已用于不同请求内容。"
                        )
                    return json.loads(old[1])
                result = operation(db)
                db.execute(
                    "INSERT INTO request_idempotency VALUES (?,?,?,?,?)",
                    (principal.actor_id, scope, key, fingerprint, canonical(result)),
                )
        except (DurableInputConflict, RunCancelled) as error:
            raise conflict() from error
        self.wake.set()
        return result

    def _queue(self, db, run_id, *, intent="recover"):
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE run_id=? AND status IN ('queued','running')",
            (run_id,),
        ).fetchone():
            raise conflict("该运行已有等待或正在执行的任务。")
        if (
            db.execute("SELECT count(*) FROM execution_jobs WHERE status='queued'").fetchone()[0]
            >= self.capacity
        ):
            raise ApplicationError(503, "QUEUE_FULL", "执行队列已满，请稍后重试。")
        job_id = "job-" + uuid4().hex
        db.execute(
            "INSERT INTO execution_jobs VALUES (?,?,?,'queued',?,NULL)",
            (job_id, run_id, intent, utc_text(self.clock())),
        )
        return Accepted(run_id=run_id, job_id=job_id).model_dump(mode="json")

    def create_ticket(self, principal, key, request):
        if principal.role != "customer":
            raise ApplicationError(403, "ROLE_FORBIDDEN", "仅客户身份可以创建工单。")

        def operation(db):
            ticket_id, now = "T-" + uuid4().hex, utc_text(self.clock())
            db.execute(
                "INSERT INTO tickets VALUES (?,?,?,NULL,?,'new',?,1,1)",
                (ticket_id, principal.actor_id, request.supplied_order_id, request.type, now),
            )
            db.execute(
                "INSERT INTO ticket_messages VALUES (?,?,'customer',?,?)",
                ("msg-" + uuid4().hex, ticket_id, request.message, now),
            )
            return {"ticket_id": ticket_id}

        result = self._idempotent(
            principal, "POST /tickets", key, request.model_dump(mode="json"), operation
        )
        return self.ticket(result["ticket_id"], principal)

    def start_run(self, ticket_id, principal, key, request):
        def operation(db):
            ticket = self._ticket(db, ticket_id, principal)
            if (
                request.expected_input_revision is not None
                and request.expected_input_revision != ticket.input_revision
            ):
                raise conflict("工单输入版本已改变。")
            run = ParallelReviewRun(self.repository, ticket_id, self.settings, clock=self.clock)
            try:
                run.store.register(
                    ticket,
                    run.runtime.session.context,
                    self.settings.checkpoint_db_path,
                    {n: getattr(self.settings, n) for n in run.limit_fields},
                    run.payload(),
                    connection=db,
                )
                return self._queue(db, run.run_id)
            finally:
                run.runtime.session.close()

        return self._idempotent(
            principal,
            f"POST /tickets/{ticket_id}/runs",
            key,
            request.model_dump(mode="json"),
            operation,
        )

    def respond(self, run_id, principal, key, request):
        def operation(db):
            row = self._run(db, run_id, principal)
            saved = db.execute(
                "SELECT * FROM pending_inputs WHERE run_id=? AND pending_id=?",
                (run_id, request.pending_id),
            ).fetchone()
            if not saved:
                raise conflict("待办不存在或已经过期。")
            pending = PendingInput.model_validate_json(saved["payload_json"])
            if (principal.role, principal.actor_id) != (
                pending.expected_role,
                pending.expected_actor,
            ):
                raise ApplicationError(403, "ROLE_FORBIDDEN", "当前身份不能答复此待办。")
            envelope = ResumeInput(
                **request.model_dump(),
                run_id=run_id,
                ticket_id=row["ticket_id"],
                role=principal.role,
                actor_id=principal.actor_id,
            )
            validate_bindings(envelope, pending)
            old = db.execute(
                "SELECT envelope_json FROM human_inputs WHERE pending_id=?", (envelope.pending_id,)
            ).fetchone()
            if old:
                if old[0] != canonical(envelope.model_dump(mode="json")):
                    raise conflict("已消费的待办不能接受不同答复。")
                return Accepted(run_id=run_id, job_id=None).model_dump(mode="json")
            if saved["status"] != "open" or row["status"] != "paused":
                raise conflict("待办尚未就绪或已失效。")
            if "order_id" in envelope.answers:
                TypeAdapter(Identifier).validate_python(envelope.answers["order_id"])
            if envelope.decision == "revise":
                self._validate_revision(db, run_id, envelope, pending)
            stored = db.execute(
                "SELECT runtime_json FROM workflow_runtime WHERE run_id=?", (run_id,)
            ).fetchone()
            ticket = Ticket.model_validate(json.loads(stored[0])["ticket"])
            # Input and execution intent commit together with the HTTP idempotency receipt.
            ParallelReviewRun.store_class(self.repository.path, run_id).accept(
                envelope, ticket, self.clock(), connection=db
            )
            return self._queue(db, run_id)

        return self._idempotent(
            principal,
            f"POST /runs/{run_id}/responses",
            key,
            request.model_dump(mode="json"),
            operation,
        )

    def _validate_revision(self, db, run_id, request, pending):
        budget = db.execute(
            "SELECT review_repairs FROM run_budget WHERE run_id=?", (run_id,)
        ).fetchone()
        limits = json.loads(
            db.execute(
                "SELECT limits_json FROM workflow_runtime WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        if not budget or budget[0] >= limits["review_repair_limit"]:
            raise conflict("共享返工次数已用完。")
        actions = {a.action_id: a.candidate for a in pending.actions}
        for identifier, amount in request.refund_amounts.items():
            action = actions.get(identifier)
            if not action or action.type != "issue_mock_refund":
                raise conflict("只能修改当前退款候选。")
            order = db.execute(
                "SELECT paid_cents,refunded_cents FROM orders WHERE id=?", (action.order_id,)
            ).fetchone()
            if (
                not order
                or amount <= 0
                or amount > order[0] - order[1]
                or amount == action.amount_cents
            ):
                raise conflict("新金额必须不同于原金额且不超过可退余额。")

    def resume(self, run_id, principal, key):
        def operation(db):
            row = self._run(db, run_id, principal)
            if row["status"] != "interrupted":
                raise conflict("此接口仅恢复 interrupted；paused 请答复待办。")
            return self._queue(db, run_id)

        return self._idempotent(principal, f"POST /runs/{run_id}/resume", key, {}, operation)

    def cancel(self, run_id, principal, key):
        def operation(db):
            row = self._run(db, run_id, principal)
            if not db.execute("SELECT 1 FROM run_control WHERE run_id=?", (run_id,)).fetchone():
                raise conflict("取消仅支持 P07 parallel 运行。")
            BudgetStore(self.repository.path, run_id, self.settings).cancel(connection=db)
            if (
                row["status"] in ("paused", "interrupted")
                and not db.execute(
                    "SELECT 1 FROM execution_jobs WHERE run_id=? "
                    "AND status IN ('queued','running')",
                    (run_id,),
                ).fetchone()
            ):
                try:
                    return self._queue(db, run_id, intent="cancel")
                except ApplicationError as error:
                    if error.code != "QUEUE_FULL":
                        raise
            return Accepted(run_id=run_id, job_id=None).model_dump(mode="json")

        return self._idempotent(principal, f"POST /runs/{run_id}/cancel", key, {}, operation)

    def _view_run(self, db, row, principal):
        job = db.execute(
            "SELECT status FROM execution_jobs WHERE run_id=? AND status IN ('queued','running')",
            (row["id"],),
        ).fetchone()
        pending = db.execute(
            "SELECT payload_json FROM pending_inputs WHERE run_id=? AND status='open'", (row["id"],)
        ).fetchone()
        control = db.execute(
            "SELECT cancel_requested FROM run_control WHERE run_id=?", (row["id"],)
        ).fetchone()
        result = db.execute(
            "SELECT payload_json FROM api_run_results WHERE run_id=?", (row["id"],)
        ).fetchone()
        last_job = db.execute(
            "SELECT error_code FROM execution_jobs WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        return RunView(
            run_id=row["id"],
            ticket_id=row["ticket_id"],
            status=job[0] if job else row["status"],
            cancel_requested=bool(control and control[0]),
            pending_input=pending_view(json.loads(pending[0]), principal)
            if pending and not job and row["status"] == "paused" and not (control and control[0])
            else None,
            result=json.loads(result[0]) if result and not job else None,
            execution_error_code=last_job[0] if last_job and not job else None,
        )

    def run(self, run_id, principal):
        with read_database(self.repository.path) as db:
            return self._view_run(db, self._run(db, run_id, principal), principal)

    def _view_ticket(self, db, ticket, principal):
        latest = db.execute(
            "SELECT * FROM runs WHERE ticket_id=? ORDER BY rowid DESC LIMIT 1", (ticket.id,)
        ).fetchone()
        activity = [ticket.created_at, *(m.created_at for m in ticket.messages)]
        # This is the latest recorded activity, not an invented ticket update timestamp.
        recorded = db.execute(
            "SELECT created_at FROM execution_jobs WHERE run_id IN "
            "(SELECT id FROM runs WHERE ticket_id=?) UNION ALL "
            "SELECT committed_at FROM action_ledger WHERE ticket_id=?",
            (ticket.id, ticket.id),
        ).fetchall()
        activity.extend(datetime.fromisoformat(r[0]) for r in recorded)
        return TicketView(
            ticket_id=ticket.id,
            type=ticket.type,
            status=ticket.status,
            supplied_order_id=ticket.supplied_order_id,
            input_revision=ticket.input_revision,
            messages=[redact(m.content) for m in ticket.messages],
            latest_run=self._view_run(db, latest, principal) if latest else None,
            last_activity_at=utc_text(max(activity)),
        )

    def ticket(self, ticket_id, principal):
        with read_database(self.repository.path) as db:
            return self._view_ticket(db, self._ticket(db, ticket_id, principal), principal)

    def tickets(self, principal, *, limit=50, offset=0):
        with read_database(self.repository.path) as db:
            rows = db.execute(
                "SELECT id FROM tickets "
                + ("WHERE customer_id=? " if principal.role == "customer" else "")
                + "ORDER BY id LIMIT ? OFFSET ?",
                (principal.actor_id, limit, offset)
                if principal.role == "customer"
                else (limit, offset),
            ).fetchall()
            return [self._view_ticket(db, _ticket(db, r[0]), principal) for r in rows]

    def events(self, run_id, principal, *, after_seq=0, limit=100):
        with read_database(self.repository.path) as db:
            self._run(db, run_id, principal)
            rows = db.execute(
                "SELECT sequence,payload_json FROM run_events WHERE run_id=? "
                "AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id, after_seq, limit + 1),
            ).fetchall()
        events = [
            EventView(
                **{k: json.loads(r[1])[k] for k in EventView.model_fields if k in json.loads(r[1])}
            )
            for r in rows[:limit]
        ]
        return EventPage(
            events=events,
            next_after_seq=events[-1].sequence if events else after_seq,
            has_more=len(rows) > limit,
        )

    def health(self):
        with read_database(self.repository.path) as db:
            db.execute("SELECT 1").fetchone()
        if not self.lease or self.stop.is_set() or not all(t.is_alive() for t in self.threads):
            raise ApplicationError(503, "NOT_READY", "执行器未就绪。")
        return {"status": "ok", "phase": "P09", "model_mode": "scripted"}
