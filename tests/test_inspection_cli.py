import json

import pytest

from after_sales.cli import main

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(capsys):
    assert main(["seed", "--json"]) == 0
    capsys.readouterr()


def test_inspect_cli_has_facts_conditions_and_evidence(seeded, capsys):
    assert main(["inspect", "--ticket", "T-RETURN-001"]) == 0
    text = capsys.readouterr().out
    assert "return_window = true" in text
    assert "证据：E-" in text
    assert "模型调用：0" in text


@pytest.mark.parametrize(
    ("ticket", "error"), [("T-MISSING-001", "MISSING_REFERENCE"), ("T-CROSS-001", "NOT_OWNED")]
)
def test_expected_business_gaps_are_structured_reports(seeded, capsys, ticket, error):
    assert main(["inspect", "--ticket", ticket, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["query_ok"] is False
    assert report["results"]["get_order"]["error"]["code"] == error
    assert report["evidence"] == []


def test_unknown_ticket_is_cli_error(seeded, capsys):
    assert main(["inspect", "--ticket", "T-ABSENT", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "RecordNotFound"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AFTER_SALES_TOOL_TIMEOUT_SECONDS", "0"),
        ("AFTER_SALES_TOOL_TIMEOUT_SECONDS", "nan"),
        ("AFTER_SALES_TOOL_MAX_RESULT_BYTES", "511"),
    ],
)
def test_tool_limits_are_validated(monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    assert main(["doctor", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["errors"][0]["field"] == name.removeprefix("AFTER_SALES_").lower()
