import json
from datetime import UTC, datetime

import pytest

from after_sales.cli import main
from after_sales.domain.models import TicketStatus
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.workflows import durable
from after_sales.workflows.reviewed import response_envelope

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 4, tzinfo=UTC)

    monkeypatch.setattr(durable, "datetime", FixedDatetime)


def args(target, ticket="T-NOTRECEIVED-002", run_id="cli-run", *extra):
    return [
        "run",
        "--ticket",
        ticket,
        "--architecture",
        "multi",
        "--workflow",
        "durable",
        "--run-id",
        run_id,
        "--output",
        str(target),
        *extra,
    ]


def test_start_resume_response_duplicate_and_static_report(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    initial = tmp_path / "initial.json"
    assert main(args(initial, "T-NOTRECEIVED-002", "cli-run", "--json")) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "paused"
    assert first["phase"] == "P06" and first["resume_scope"] == "cross_process"
    assert main(["resume", "--run", "cli-run", "--json"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["pending_input"] == first["pending_input"]
    response = tmp_path / "answer.json"
    response.write_text(json.dumps(response_envelope(first["pending_input"], decision="approve")))
    completed = tmp_path / "completed.json"
    command = [
        "resume",
        "--run",
        "cli-run",
        "--model",
        "scripted",
        "--response-file",
        str(response),
        "--json",
        "--output",
        str(completed),
    ]
    assert main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed" and report["business_status"] == "resolved"
    assert len(report["executed_actions"]) == 1
    assert json.loads(initial.read_text())["status"] == "paused"
    assert main(["resume", "--run", "cli-run", "--response-file", str(response), "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["executed_actions"] == report["executed_actions"]
    assert repeated["statistics"]["model_calls"] == report["statistics"]["model_calls"]
    assert main(["report", "show", str(completed), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert main(["report", "show", str(completed), "--events-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report["events"]


def test_resume_interactive_can_stop_then_finish_in_a_later_invocation(
    tmp_path, capsys, monkeypatch
):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert main(args(tmp_path / "missing.json", "T-MISSING-001", "missing-run", "--json")) == 0
    capsys.readouterr()
    answers = iter(["ORD-019", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert (
        main(["resume", "--run", "missing-run", "--output", str(tmp_path / "answered.json")]) == 0
    )
    output = capsys.readouterr().out
    report = json.loads((tmp_path / "answered.json").read_text())
    assert report["status"] == "paused" and report["input_revision"] == 2
    assert "resume --run missing-run" in output
    monkeypatch.setattr("builtins.input", lambda _: "approve")
    assert (
        main(["resume", "--run", "missing-run", "--output", str(tmp_path / "finished.json")]) == 0
    )
    output = capsys.readouterr().out
    report = json.loads((tmp_path / "finished.json").read_text())
    assert report["business_status"] == "waiting_return" and report["status"] == "completed"
    assert "已提交动作：1" in output


def test_existing_output_refuses_before_any_business_effect(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    target = tmp_path / "report.json"
    assert main(args(target, "T-NOTRECEIVED-002", "output-run", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    response = tmp_path / "response.json"
    response.write_text(json.dumps(response_envelope(report["pending_input"], decision="approve")))
    assert (
        main(
            [
                "resume",
                "--run",
                "output-run",
                "--response-file",
                str(response),
                "--output",
                str(target),
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"] == "FileExistsError"
    repo = BusinessRepository(tmp_path / "var" / "business.sqlite")
    assert repo.get_order("ORD-004", customer_id="CUST-A").refunded_cents == 0
    assert repo.get_ticket("T-NOTRECEIVED-002").status == TicketStatus.WAITING_REVIEW


@pytest.mark.parametrize(
    "extra",
    [
        ["--workflow", "durable"],
        ["--run-id", "test"],
        ["--architecture", "multi", "--workflow", "durable", "--interactive"],
    ],
)
def test_invalid_flags_do_not_initialize_storage(tmp_path, capsys, extra):
    assert main(["run", "--ticket", "T-RETURN-001", *extra, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"
    assert not (tmp_path / "var").exists()


def test_unknown_run_and_json_report_cannot_restore_graph(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    report = tmp_path / "fake.json"
    report.write_text('{"run_id":"fake"}')
    assert main(["resume", "--run", "fake", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "RecordNotFound"
    assert not (tmp_path / "var" / "checkpoints.sqlite").exists()


def test_durable_live_explicitly_unavailable_and_doctor_has_no_side_effects(tmp_path, capsys):
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "P07"
    assert report["human_input"]["checkpointer"] == "AsyncSqliteSaver"
    assert not (tmp_path / "var").exists()
    assert (
        main(args(tmp_path / "live.json", "T-RETURN-001", "live-run", "--model", "live", "--json"))
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"
    assert not (tmp_path / "var").exists()
