import json

import pytest

from after_sales.cli import main

pytestmark = pytest.mark.integration


def test_multi_cli_saves_and_reads_graph_results(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    target = tmp_path / "multi.json"
    assert (
        main(
            [
                "run",
                "--ticket",
                "T-RETURN-001",
                "--architecture",
                "multi",
                "--output",
                str(target),
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "multi-run-v1"
    assert report["phase"] == "P04" and report["status"] == "completed"
    assert json.loads(target.read_text()) == report
    assert main(["report", "show", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert main(["report", "show", str(target), "--events-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report["events"]


def test_multi_text_demonstrates_nodes_and_pending_candidates(capsys):
    assert main(["seed"]) == 0
    capsys.readouterr()
    assert main(["run", "--ticket", "T-RETURN-001", "--architecture", "multi"]) == 0
    output = capsys.readouterr().out
    assert "intake → order → policy → draft → validate" in output
    assert "propose_return" in output and "执行动作数：0" in output


def test_multi_failed_run_is_saved_with_nonzero_exit(tmp_path, capsys, monkeypatch):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("AFTER_SALES_MAX_MODEL_CALLS", "1")
    target = tmp_path / "failed.json"
    assert (
        main(
            [
                "run",
                "--ticket",
                "T-RETURN-001",
                "--architecture",
                "multi",
                "--output",
                str(target),
                "--json",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["node"] == "order"
    assert report["graph_state"]["intake"] is not None
    assert report["accepted_proposal"] is None
    assert json.loads(target.read_text())["status"] == "failed"


def test_multi_cli_does_not_overwrite_saved_records(tmp_path, capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    target = tmp_path / "multi.json"
    args = [
        "run",
        "--ticket",
        "T-RETURN-001",
        "--architecture",
        "multi",
        "--output",
        str(target),
        "--json",
    ]
    assert main(args) == 0
    capsys.readouterr()
    before = target.read_bytes()
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "FileExistsError"
    assert target.read_bytes() == before


def test_multi_live_cli_skips_without_network(capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "run",
                "--ticket",
                "T-RETURN-001",
                "--architecture",
                "multi",
                "--model",
                "live",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "skipped"
    assert report["statistics"]["model_calls"] == 0
    assert report["agent_runs"] == []
