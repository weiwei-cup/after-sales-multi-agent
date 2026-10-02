import json

import pytest

from after_sales.cli import main
from after_sales.workflows.reviewed import response_envelope

pytestmark = pytest.mark.integration


def start(tmp_path, capsys, run_id="parallel-cli"):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "run",
                "--ticket",
                "T-NOTRECEIVED-002",
                "--architecture",
                "multi",
                "--workflow",
                "parallel",
                "--run-id",
                run_id,
                "--output",
                str(tmp_path / f"{run_id}.json"),
                "--json",
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


def test_parallel_cli_auto_dispatches_resume_and_static_report(tmp_path, capsys):
    first = start(tmp_path, capsys)
    assert first["status"] == "paused" and first["phase"] == "P07"
    assert main(["resume", "--run", first["run_id"], "--json"]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["pending_input"] == first["pending_input"]
    response = tmp_path / "approval.json"
    response.write_text(json.dumps(response_envelope(first["pending_input"], decision="approve")))
    completed_path = tmp_path / "completed.json"
    assert (
        main(
            [
                "resume",
                "--run",
                first["run_id"],
                "--response-file",
                str(response),
                "--output",
                str(completed_path),
                "--json",
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed" and len(completed["executed_actions"]) == 1
    assert main(["report", "show", str(completed_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == "parallel-run-v1"


def test_cli_cancel_then_resume_acknowledges_without_effect(tmp_path, capsys):
    report = start(tmp_path, capsys)
    assert main(["cancel", "--run", report["run_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cancel_requested"]
    assert main(["resume", "--run", report["run_id"], "--json"]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["status"] == "cancelled" and cancelled["executed_actions"] == []
    assert main(["cancel", "--run", report["run_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["acknowledged"]


@pytest.mark.parametrize(
    "extra", [["--interactive", "--json"], ["--model", "live"], ["--architecture", "single"]]
)
def test_parallel_cli_rejects_incompatible_flags_before_execution(tmp_path, capsys, extra):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    command = [
        "run",
        "--ticket",
        "T-NOTRECEIVED-002",
        "--architecture",
        "multi",
        "--workflow",
        "parallel",
        "--run-id",
        "invalid",
        "--output",
        str(tmp_path / "invalid.json"),
        *extra,
    ]
    assert main(command) == 2
    capsys.readouterr()
    assert not (tmp_path / "invalid.json").exists()


def test_existing_output_blocks_cancellation_sensitive_resume_before_effect(tmp_path, capsys):
    report = start(tmp_path, capsys)
    response = tmp_path / "approve.json"
    response.write_text(json.dumps(response_envelope(report["pending_input"], decision="approve")))
    existing = tmp_path / "existing.json"
    existing.write_text("keep")
    assert (
        main(
            [
                "resume",
                "--run",
                report["run_id"],
                "--response-file",
                str(response),
                "--output",
                str(existing),
                "--json",
            ]
        )
        == 2
    )
    capsys.readouterr()
    assert existing.read_text() == "keep"
    assert main(["resume", "--run", report["run_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "paused"


@pytest.mark.parametrize(
    "field,value", [("actual_total_tokens", 0), ("monetary_cost", 0), ("model_calls", 0)]
)
def test_saved_parallel_reports_cannot_claim_zero_unknown_cost_or_refunded_calls(
    tmp_path, capsys, field, value
):
    report = start(tmp_path, capsys)
    report["statistics"][field] = value
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(report))
    assert main(["report", "show", str(tampered), "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
