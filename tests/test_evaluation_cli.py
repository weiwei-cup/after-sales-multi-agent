import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from after_sales.cli import main
from after_sales.workflows import durable
from after_sales.workflows.reviewed import response_envelope

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent


def test_evaluation_cli_keeps_developer_database_untouched(tmp_path, capsys):
    output = tmp_path / "evaluation"
    args = [
        "eval",
        "run",
        "--suite-root",
        str(ROOT),
        "--case",
        "C01",
        "--output-dir",
        str(output),
        "--json",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["critical_error_total"] == 0 and result["safety_observation_complete"]
    assert not (tmp_path / "var").exists()
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_durable_single_cli_reports_the_actual_executed_action(tmp_path, capsys, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 4, tzinfo=UTC)

    monkeypatch.setattr(durable, "datetime", FixedDatetime)
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "run",
                "--ticket",
                "T-NOTRECEIVED-002",
                "--architecture",
                "single",
                "--workflow",
                "single",
                "--run-id",
                "single-cli",
                "--json",
            ]
        )
        == 0
    )
    pending = json.loads(capsys.readouterr().out)
    answer = tmp_path / "approve.json"
    answer.write_text(json.dumps(response_envelope(pending["pending_input"], decision="approve")))
    assert main(["resume", "--run", "single-cli", "--response-file", str(answer)]) == 0
    rendered = capsys.readouterr().out
    assert "已提交动作：1" in rendered and "动作账本：" in rendered
    assert "执行动作数：0" not in rendered
