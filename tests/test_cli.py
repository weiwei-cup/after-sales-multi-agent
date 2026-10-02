import json
import subprocess
import sys
from pathlib import Path

import pytest

from after_sales.cli import main

pytestmark = pytest.mark.unit


def test_doctor_runs_offline_and_does_not_create_databases(capsys, tmp_path):
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["model_mode"] == "scripted"
    assert report["model_connectivity"] == "not_checked"
    assert report["packages"]["langchain"] != "missing"
    assert not (tmp_path / "var").exists()


@pytest.mark.parametrize("json_output", [True, False])
def test_bad_config_is_clear_and_does_not_leak_key(monkeypatch, capsys, json_output):
    secret = "test-placeholder-sensitive"
    monkeypatch.setenv("AFTER_SALES_MODEL_API_KEY", secret)
    monkeypatch.setenv("AFTER_SALES_MAX_MODEL_CALLS", "invalid")
    args = ["doctor", "--json"] if json_output else ["doctor"]
    assert main(args) == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "max_model_calls" in output.out + output.err
    assert "invalid" not in output.out + output.err


def test_live_config_is_checked_without_model_request(monkeypatch, capsys):
    monkeypatch.setenv("AFTER_SALES_MODEL_MODE", "live")
    monkeypatch.setenv("AFTER_SALES_MODEL_PROVIDER", "example")
    monkeypatch.setenv("AFTER_SALES_MODEL_NAME", "example-model")
    monkeypatch.setenv("AFTER_SALES_MODEL_API_KEY", "test-placeholder-sensitive")
    assert main(["doctor", "--json"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["model_connectivity"] == "not_checked"
    assert "test-placeholder-sensitive" not in output


def test_installed_command_help():
    result = subprocess.run(
        [str(Path(sys.executable).with_name("after-sales")), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "doctor" in result.stdout
