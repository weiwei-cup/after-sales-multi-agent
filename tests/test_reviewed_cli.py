import json

import pytest

from after_sales.cli import main

pytestmark = pytest.mark.integration


def arguments(target, ticket="T-MISSING-001", *extra):
    return [
        "run",
        "--ticket",
        ticket,
        "--architecture",
        "multi",
        "--workflow",
        "reviewed",
        "--output",
        str(target),
        *extra,
    ]


def test_noninteractive_pending_save_and_report_read(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    target = tmp_path / "pending.json"
    assert main(arguments(target, "T-RETURN-001", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "P05" and report["status"] == "paused"
    assert report["schema_version"] == "review-run-v1"
    assert json.loads(target.read_text()) == report
    assert main(["report", "show", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert main(["report", "show", str(target), "--events-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report["events"]
    assert main(arguments(target, "T-RETURN-001", "--json")) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "FileExistsError"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_interactive_order_answer_and_operator_decision_same_process(
    tmp_path, capsys, monkeypatch, decision
):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    answers = iter(("ORD-019", decision))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    target = tmp_path / "interactive.json"
    assert main(arguments(target, "T-MISSING-001", "--interactive")) == 0
    output = capsys.readouterr().out
    report = json.loads(target.read_text())
    assert report["status"] == ("completed" if decision == "approve" else "handed_off")
    assert report["input_revision"] == report["proposal_revision"] == 2
    assert "customer_info" in output and "operator_decision" in output
    assert "ORD-019" in output and "create_return_request" in output
    assert "执行动作数：0" in output
    assert report["confirmations"][0]["decision"] == decision
    assert len(report["pending_history"]) == 2


def test_interactive_refund_edit_is_redisplayed_then_confirmed(tmp_path, capsys, monkeypatch):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    answers = iter(("revise", "9000", "approve"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    target = tmp_path / "revised.json"
    assert main(arguments(target, "T-NOTRECEIVED-002", "--interactive")) == 0
    output = capsys.readouterr().out
    report = json.loads(target.read_text())
    assert "金额 10000 分" in output and "金额 9000 分" in output
    assert report["status"] == "completed" and report["repair_count"] == 1
    assert report["accepted_proposal"]["actions"][0]["amount_cents"] == 9000


def test_interactive_bad_answer_retries_without_duplicate_pending(tmp_path, capsys, monkeypatch):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    answers = iter(("../wrong", "ORD-019", "approve"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    target = tmp_path / "retry.json"
    assert main(arguments(target, "T-MISSING-001", "--interactive")) == 0
    output = capsys.readouterr().out
    report = json.loads(target.read_text())
    assert "回答未消费" in output and report["status"] == "completed"
    assert sum(e["kind"] == "pending_created" for e in report["events"]) == 2
    assert report["input_revision"] == 2


@pytest.mark.parametrize("end", ["eof", "quit"])
def test_interactive_exit_keeps_paused_static_report(tmp_path, capsys, monkeypatch, end):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()

    def answer(_):
        if end == "eof":
            raise EOFError
        return "quit"

    monkeypatch.setattr("builtins.input", answer)
    target = tmp_path / "paused.json"
    assert main(arguments(target, "T-RETURN-001", "--interactive")) == 0
    capsys.readouterr()
    report = json.loads(target.read_text())
    assert report["status"] == "paused" and not report["confirmations"]


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--ticket", "T-RETURN-001", "--workflow", "reviewed", "--json"],
        ["run", "--ticket", "T-RETURN-001", "--interactive", "--json"],
        [
            "run",
            "--ticket",
            "T-RETURN-001",
            "--architecture",
            "multi",
            "--workflow",
            "reviewed",
            "--interactive",
            "--json",
        ],
    ],
)
def test_invalid_workflow_flags_do_not_run_or_write(capsys, tmp_path, args):
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"
    assert not (tmp_path / "var").exists()


def test_reviewed_budget_failure_is_saved_and_nonzero(tmp_path, capsys, monkeypatch):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("AFTER_SALES_MAX_MODEL_CALLS", "1")
    target = tmp_path / "budget.json"
    assert main(arguments(target, "T-RETURN-001", "--json")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "handed_off" and report["error"]["code"] == "CallLimitExceeded"
    assert report["accepted_proposal"] is None


def test_reviewed_live_is_skipped_without_network(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert main(arguments(tmp_path / "live.json", "T-RETURN-001", "--model", "live", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "skipped" and report["statistics"]["model_calls"] == 0


def test_doctor_reports_actual_scope_without_creating_db(capsys, tmp_path):
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "P10"
    assert report["human_input"]["resume_scope"] == "cross_process"
    assert report["budgets"]["review_repairs"] == 2
    assert not (tmp_path / "var").exists()
