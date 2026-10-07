"""Isolated business execution driven only by case inputs, never by scoring gold."""

import asyncio
import copy
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import MethodType

from after_sales.agents.contracts import ActionBinding, ReviewResult, ValidationIssue
from after_sales.api.contracts import CreateTicket, Principal
from after_sales.domain.models import utc_text
from after_sales.evaluation.suite import EvaluationCase, write_new_json
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, migrate, read_database, transaction
from after_sales.services.application import ApplicationService
from after_sales.tools.contracts import ErrorCode, failed
from after_sales.workflows.parallel import ParallelReviewRun, parallel_model
from after_sales.workflows.reviewed import response_envelope
from after_sales.workflows.single import SingleReviewRun, single_model


def database_snapshot(repo):
    with read_database(repo.path) as db:
        orders = {
            r["id"]: dict(r)
            for r in db.execute("SELECT id,customer_id,paid_cents,refunded_cents FROM orders")
        }
        return orders, db.execute("SELECT count(*) FROM after_sales_history").fetchone()[0]


def database_effects(repo):
    with read_database(repo.path) as db:
        return [
            {"receipt": json.loads(r["receipt_json"]), "payload": json.loads(r["payload_json"])}
            for r in db.execute("SELECT * FROM action_ledger ORDER BY operation_key")
        ], db.execute("SELECT count(*) FROM runs").fetchone()[0]


class FaultPlan:
    def __init__(self, names, repo, clock):
        self.names, self.repo, self.clock = set(names), repo, clock
        self.triggered, self.failures = [], []
        self.tracking_failed = self.changed_order = False

    def mark(self, name, stage):
        self.triggered.append({"name": name, "stage": stage})

    def session(self, session):
        original = session.call

        async def call(name, arguments):
            if (
                name == "get_tracking"
                and "get_tracking_timeout_once" in self.names
                and not self.tracking_failed
            ):
                self.tracking_failed = True
                self.mark("get_tracking_timeout_once", "tool_result")
                result = failed(ErrorCode.TOOL_TIMEOUT, "注入一次物流查询超时", retryable=True)
            elif (
                name == "get_delivery_proof" and "get_delivery_proof_always_times_out" in self.names
            ):
                self.mark("get_delivery_proof_always_times_out", "tool_result")
                result = failed(ErrorCode.TOOL_TIMEOUT, "注入持续凭证查询超时", retryable=True)
            else:
                result = await original(name, arguments)
            if not result.ok:
                self.failures.append({"tool": name, "code": result.error.code.value})
            return result

        session.call = call
        return session

    def factory(self, original):
        def create(role, stage, payload):
            model = original(role, stage, payload)
            if "invalid_evidence_reference" not in self.names or stage != "draft":
                return model
            steps = []
            for step in model.steps:
                original_response = step.response

                def corrupt(messages, response=original_response):
                    message = response(messages) if callable(response) else response
                    message = message.model_copy(deep=True)
                    for call in message.tool_calls:
                        if call["name"] == "ResolutionProposal" and call["args"].get(
                            "evidence_refs"
                        ):
                            call["args"]["evidence_refs"][0]["evidence_id"] = "E-injected-missing"
                            self.mark("invalid_evidence_reference", "candidate_output")
                    return message

                steps.append(replace(step, response=corrupt))
            return model.model_copy(update={"steps": tuple(steps)})

        return create

    def callback(self, stage):
        if (
            stage != "after_input_commit"
            or "order_changes_after_approval" not in self.names
            or self.changed_order
        ):
            return
        self.changed_order = True
        with transaction(self.repo.path) as db:
            row = db.execute(
                "SELECT json_extract(payload_json,'$.actions[0].candidate.order_id') "
                "FROM pending_inputs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                from datetime import timedelta

                db.execute(
                    "UPDATE orders SET received_at=?,version=version+1 WHERE id=?",
                    (utc_text(self.clock() - timedelta(days=8)), row[0]),
                )
                self.mark("order_changes_after_approval", "after_input_commit")

    def run_class(self, architecture):
        base, factory = (
            (SingleReviewRun, single_model)
            if architecture == "single"
            else (ParallelReviewRun, parallel_model)
        )
        plan = self

        class EvaluationRun(base):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("model_factory", plan.factory(factory))
                super().__init__(*args, **kwargs)
                if "review_always_rejects" in plan.names:
                    original = self.runtime.review

                    async def review(runtime, state, config):
                        result = await original(state, config)
                        if result["review"]["outcome"] == "accept":
                            plan.mark("review_always_rejects", "review_guard")
                            rejected = ReviewResult(
                                outcome="revise",
                                issues=(
                                    ValidationIssue(
                                        code="INJECTED_REVIEW_REJECTION", message="注入持续审核返工"
                                    ),
                                ),
                                revision_instructions="重新生成建议后再次审核。",
                            )
                            return {"review": rejected.model_dump(mode="json"), "route": "repair"}
                        return result

                    self.runtime.review = MethodType(review, self.runtime)

            def new_session(self, evidence_store=None):
                return plan.session(super().new_session(evidence_store))

        return EvaluationRun


def holdout_ticket(repo, settings, case, now):
    service = ApplicationService(repo, settings, clock=lambda: now)
    request = copy.deepcopy(case.request)
    customer = request.pop("customer_id")
    view = service.create_ticket(
        Principal(role="customer", actor_id=customer), case.id, CreateTicket.model_validate(request)
    )
    return view.ticket_id


def http_protocol(repo, settings, ticket_id, architecture, now, duplicate):
    # Real application admission/worker/response path, through an in-process ASGI transport.
    from fastapi.testclient import TestClient

    from after_sales.api.app import create_app

    service = ApplicationService(repo, settings, clock=lambda: now)
    headers = {"Authorization": "Bearer demo-operator", "Idempotency-Key": "evaluation-start"}
    requests = []
    with TestClient(create_app(settings, service=service)) as client:
        first = client.post(
            f"/tickets/{ticket_id}/runs",
            json={"workflow": "single" if architecture == "single" else "parallel"},
            headers=headers,
        )
        requests.append(
            {"method": "POST", "path": f"/tickets/{ticket_id}/runs", "status": first.status_code}
        )
        if first.status_code != 202:
            raise RuntimeError("HTTP start was not accepted")
        run_id = first.json()["run_id"]
        if duplicate:
            second = client.post(
                f"/tickets/{ticket_id}/runs",
                json={"workflow": "single" if architecture == "single" else "parallel"},
                headers=headers,
            )
            if second.status_code != 202 or second.json() != first.json():
                raise RuntimeError("duplicate HTTP start did not replay its receipt")
            requests.append(
                {
                    "method": "POST",
                    "path": f"/tickets/{ticket_id}/runs",
                    "status": second.status_code,
                    "same_receipt": True,
                }
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            view = client.get(f"/runs/{run_id}", headers=headers).json()
            pending = view.get("pending_input")
            if (
                pending
                and pending["can_respond"]
                or view.get("result")
                and view["status"] in {"completed", "failed"}
            ):
                break
            time.sleep(0.02)
        else:
            raise TimeoutError("HTTP executor did not reach a stable result")
        if pending and pending["kind"] == "operator_decision":
            body = {
                k: pending[k]
                for k in ("pending_id", "input_revision", "proposal_revision", "proposal_hash")
            }
            body.update(
                decision="approve",
                action_hashes={a["action_id"]: a["content_hash"] for a in pending["actions"]},
            )
            response_headers = {**headers, "Idempotency-Key": "evaluation-approval"}
            approved = client.post(f"/runs/{run_id}/responses", json=body, headers=response_headers)
            if approved.status_code != 202:
                raise RuntimeError("HTTP approval was not accepted")
            requests.append({"method": "POST", "path": f"/runs/{run_id}/responses", "status": 202})
            if duplicate:
                again = client.post(
                    f"/runs/{run_id}/responses", json=body, headers=response_headers
                )
                if again.status_code != 202 or again.json() != approved.json():
                    raise RuntimeError("duplicate HTTP approval did not replay its receipt")
                requests.append(
                    {
                        "method": "POST",
                        "path": f"/runs/{run_id}/responses",
                        "status": 202,
                        "same_receipt": True,
                    }
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                view = client.get(f"/runs/{run_id}", headers=headers).json()
                if view["status"] == "completed" and view.get("result"):
                    break
                time.sleep(0.02)
            else:
                raise TimeoutError("HTTP approval did not complete")
    return run_id, requests


async def execute_case(
    case: EvaluationCase,
    split,
    architecture,
    repetition,
    settings,
    root: Path,
    now,
    *,
    browser=False,
):
    started = time.perf_counter()
    settings = settings.model_copy(
        update={
            "business_db_path": root / "business.sqlite",
            "checkpoint_db_path": root / "checkpoints.sqlite",
            "model_mode": "scripted",
        }
    )
    if "one_model_call_remaining_two_workers" in case.fault_injections:
        settings = settings.model_copy(update={"max_model_calls": 2})
    seed_demo(settings.business_db_path)
    migrate(settings.business_db_path)
    repo = BusinessRepository(settings.business_db_path)
    ticket_id = case.ticket_id or holdout_ticket(repo, settings, case, now)
    before, history_before = database_snapshot(repo)
    plan = FaultPlan(case.fault_injections, repo, lambda: now)
    cls = plan.run_class(architecture)
    protocol, scenario_exercised = [], True
    run = None
    try:
        if set(case.fault_injections) & {"repeat_start_and_confirm", "refresh_and_reconnect"}:
            run_id, protocol = await asyncio.to_thread(
                http_protocol,
                repo,
                settings,
                ticket_id,
                architecture,
                now,
                "repeat_start_and_confirm" in case.fault_injections,
            )
            run = await cls.load(repo, run_id, settings, clock=lambda: now)
            report = await run.recover()
            if "repeat_start_and_confirm" in case.fault_injections:
                plan.mark("repeat_start_and_confirm", "HTTP_idempotent_start_and_response")
            if "refresh_and_reconnect" in case.fault_injections:
                if browser:
                    from after_sales.evaluation.browser import check_refresh

                    await run.aclose()
                    protocol.extend(
                        await asyncio.to_thread(
                            check_refresh, root, settings, ticket_id, run_id, now, architecture
                        )
                    )
                    plan.mark("refresh_and_reconnect", "real_browser_reload_and_cursor_reconnect")
                    run = await cls.load(repo, run_id, settings, clock=lambda: now)
                    report = await run.recover()
                else:
                    scenario_exercised = False
        else:
            run = await cls.create(
                repo, ticket_id, settings, clock=lambda: now, fault=plan.callback
            )
            report = await run.start()
        if "one_model_call_remaining_two_workers" in case.fault_injections:
            plan.mark(
                "one_model_call_remaining_two_workers",
                "same_two_call_budget_for_both_architectures",
            )
        first_pending = report.get("pending_input")
        if first_pending and first_pending["kind"] == "operator_decision":
            envelope = response_envelope(first_pending, decision="approve")
            run_id = run.run_id
            await run.aclose()
            if "crash_after_action_commit" in case.fault_injections:
                response_path = root / "approval.json"
                write_new_json(response_path, envelope)
                write_new_json(
                    root / "worker-settings.json",
                    {name: getattr(settings, name) for name in run.limit_fields},
                )
                child = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-m",
                        "after_sales.evaluation.worker",
                        "crash",
                        str(root),
                        "--architecture",
                        architecture,
                        "--run-id",
                        run_id,
                        "--response",
                        str(response_path),
                        "--as-of",
                        now.isoformat(),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if child.returncode != 86:
                    raise RuntimeError(f"crash child did not exit at commit: {child.returncode}")
                plan.mark("crash_after_action_commit", "subprocess_os_exit_86_after_commit")
                protocol.append({"operation": "crash_after_action_commit", "exit_code": 86})
                run = await cls.load(repo, run_id, settings, clock=lambda: now)
                report = await run.recover()
            else:
                run = await cls.load(repo, run_id, settings, clock=lambda: now, fault=plan.callback)
                report = await run.resume(envelope)
            if "repeat_same_action" in case.fault_injections:
                report = await run.resume(envelope)
                for binding in first_pending["actions"]:
                    run.actions.execute(
                        run_id,
                        first_pending["pending_id"],
                        ActionBinding.model_validate(binding),
                        clock=lambda: now,
                    )
                plan.mark("repeat_same_action", "approval_replay_and_action_ledger_replay")
        after, history_after = database_snapshot(repo)
        writes, run_count = database_effects(repo)
        return {
            "schema_version": "evaluation-observation-v1",
            "case_id": case.id,
            "split": split,
            "repetition": repetition,
            "report": report,
            "before": before,
            "after": after,
            "writes": writes,
            "new_business_records": history_after - history_before,
            "run_count": run_count,
            "failures": plan.failures,
            "faults": plan.triggered,
            "scenario_exercised": scenario_exercised,
            "protocol": protocol,
            "limits": {name: getattr(settings, name) for name in run.limit_fields},
            "wall_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    finally:
        if run is not None:
            await run.aclose()
