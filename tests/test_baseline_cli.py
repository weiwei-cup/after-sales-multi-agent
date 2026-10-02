import json
from pathlib import Path

import pytest

from after_sales.cli import main

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()


def test_run_cli_saves_complete_report_and_can_read_events(seeded, capsys, tmp_path):
    target = tmp_path / "result.json"
    assert (
        main(
            [
                "run",
                "--ticket",
                "T-RETURN-001",
                "--architecture",
                "single",
                "--model",
                "scripted",
                "--output",
                str(target),
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert json.loads(target.read_text()) == report
    assert report["accepted_proposal"]["decision"] == "propose_return"
    assert main(["report", "show", str(target), "--events-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report["events"]


def test_run_cli_text_has_draft_limits_and_saved_file(seeded, capsys, tmp_path):
    assert main(["run", "--ticket", "T-NOTRECEIVED-002"]) == 0
    output = capsys.readouterr().out
    assert "propose_refund" in output and "10000 分" in output
    assert "执行动作数：0" in output
    reports = list((tmp_path / "var/runs").glob("*.json"))
    assert len(reports) == 1
    assert main(["report", "show", str(reports[0]), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["statistics"]["validation_tool_calls"] == 1


def test_cli_cannot_overwrite_an_existing_result(seeded, capsys, tmp_path):
    target = tmp_path / "result.json"
    args = ["run", "--ticket", "T-RETURN-001", "--output", str(target), "--json"]
    assert main(args) == 0
    capsys.readouterr()
    before = target.read_bytes()
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "FileExistsError"
    assert target.read_bytes() == before


def test_failed_run_has_nonzero_exit_and_saved_diagnostics(seeded, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AFTER_SALES_MAX_MODEL_CALLS", "1")
    target = tmp_path / "failed.json"
    assert main(["run", "--ticket", "T-RETURN-001", "--output", str(target), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "CallLimitExceeded"
    assert report["accepted_proposal"] is None
    assert json.loads(target.read_text())["status"] == "failed"


@pytest.mark.parametrize(
    "command",
    [["live-smoke", "--json"], ["run", "--ticket", "T-RETURN-001", "--model", "live", "--json"]],
)
def test_live_cli_explicitly_skips_without_network_or_credentials(seeded, capsys, command):
    assert main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "skipped"
    assert report.get("model_calls", report.get("statistics", {}).get("model_calls")) == 0


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"schema_version":"other"}',
        '{"schema_version":"baseline-run-v1","status":"completed"}',
    ],
)
def test_malformed_run_report_is_a_clear_cli_error(tmp_path, capsys, payload):
    target = tmp_path / "bad.json"
    target.write_text(payload)
    assert main(["report", "show", str(target), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PROPOSAL_REPAIR_LIMIT", "-1"),
        ("PROPOSAL_REPAIR_LIMIT", "4"),
        ("MODEL_TIMEOUT_SECONDS", "0"),
        ("MODEL_TIMEOUT_SECONDS", "inf"),
    ],
)
def test_baseline_runtime_configuration_is_bounded(monkeypatch, capsys, field, value):
    monkeypatch.setenv(f"AFTER_SALES_{field}", value)
    assert main(["doctor", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["errors"][0]["field"] == field.lower()


def test_unused_live_key_is_not_sent_or_saved_in_offline_run(seeded, monkeypatch, capsys):
    secret = "unused-live-secret-placeholder"
    monkeypatch.setenv("AFTER_SALES_MODEL_API_KEY", secret)
    assert main(["run", "--ticket", "T-MISSING-001", "--json"]) == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert secret not in Path(json.loads(output)["artifact_path"]).read_text()
