import json
import subprocess
import sys
from pathlib import Path

import pytest

from after_sales.cli import main

pytestmark = pytest.mark.integration


def test_seed_then_query_three_business_scenarios(capsys, tmp_path):
    assert main(["seed", "--dataset", "demo", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["orders"] == 30
    assert report["counts"]["tickets"] == 20
    assert not (tmp_path / "var/checkpoints.sqlite").exists()
    for ticket in ("T-DELAY-001", "T-NOTRECEIVED-001", "T-RETURN-001"):
        assert main(["ticket", "show", ticket, "--json"]) == 0
        view = json.loads(capsys.readouterr().out)
        assert view["ticket"]["id"] == ticket
        assert view["order_reference_status"] == "verified"
        assert "allowed_outcomes" not in view
    assert main(["ticket", "list", "--customer", "CUST-A", "--json"]) == 0
    assert all(ticket["customer_id"] == "CUST-A" for ticket in json.loads(capsys.readouterr().out))


def test_human_ticket_display_uses_shanghai_time(capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert main(["ticket", "show", "T-RETURN-001"]) == 0
    display = capsys.readouterr().out
    assert "2026-10-02T12:00:00+08:00" in display
    assert "2026-09-29T12:00:00+08:00" in display
    assert "100.00" in display


def test_missing_and_inaccessible_order_return_clear_errors(capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert main(["order", "show", "ORD-008", "--customer", "CUST-A", "--json"]) == 2
    denied = json.loads(capsys.readouterr().out)
    assert denied["error"] == "OrderAccessDenied"
    assert "CUST-B" not in json.dumps(denied)
    assert main(["order", "show", "ORD-DOES-NOT-EXIST", "--customer", "CUST-A", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "RecordNotFound"


def test_policy_cli_supports_versions_and_rejects_naive_time(capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()
    assert main(["policy", "show", "RETURN-STANDARD", "--version", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["conditions"]["window_hours"] == 120
    assert main(["policy", "list", "--intent", "return_request", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["policy", "list", "--as-of", "2026-10-02T12:00:00", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_installed_seed_command_finds_packaged_data_from_other_directory(tmp_path):
    command = Path(sys.executable).with_name("after-sales")
    result = subprocess.run(
        [str(command), "seed", "--json"], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["counts"]["orders"] == 30
