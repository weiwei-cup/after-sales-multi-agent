import asyncio
import json

import pytest
from pydantic import ValidationError

from after_sales.agents.runner import run_baseline, save_run
from after_sales.cli import main
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.workflows.serial import run_multi

pytestmark = pytest.mark.integration


@pytest.fixture(params=[run_baseline, run_multi], ids=["single", "multi"])
def report(request, tmp_path):
    path = tmp_path / "business.sqlite"
    seed_demo(path)
    generated = asyncio.run(request.param(BusinessRepository(path), "T-RETURN-001", Settings()))
    # A stored JSON file has independent proposal objects, even if the runtime reused a dict.
    return json.loads(json.dumps(generated))


@pytest.mark.parametrize(
    "variant",
    ["ticket", "proposal", "accepted", "validation", "validation_failed", "error"],
)
def test_inconsistent_reports_are_rejected_before_save_and_by_cli(
    report, tmp_path, capsys, variant
):
    if variant == "ticket":
        report["ticket_id"] = "T-CROSS-001"
    elif variant == "proposal":
        report["proposal"]["ticket_id"] = "T-CROSS-001"
    elif variant == "accepted":
        report["accepted_proposal"]["customer_reply_draft"] = "另一份未经本次校验的草稿。"
    elif variant == "validation":
        report["validation"]["issues"] = [{"code": "UNSUPPORTED_CLAIM", "message": "事实不符"}]
    elif variant == "validation_failed":
        report["status"] = "validation_failed"
        report["accepted_proposal"] = None
    else:
        report["error"] = {"code": "MODEL_OR_TOOL_FAILURE", "message": "执行失败"}
    target = tmp_path / "output" / "rejected.json"
    with pytest.raises(ValidationError):
        save_run(report, target)
    assert not target.parent.exists()
    target = tmp_path / "inconsistent.json"
    target.write_text(json.dumps(report), encoding="utf-8")
    assert main(["report", "show", str(target), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
