"""Behavioral evaluation checks: isolated effects, honest failures and independent gold."""

import asyncio
import copy
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from after_sales.config import Settings
from after_sales.evaluation import runner
from after_sales.evaluation.engine import execute_case
from after_sales.evaluation.scoring import estimated_cost, score_observation
from after_sales.evaluation.suite import CaseDocument, EvaluationCase, read_cases, read_gold

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent


def observe(tmp_path, case_id, architecture="single"):
    doc, _ = read_cases(ROOT, "dev")
    case = next(c for c in doc.cases if c.id == case_id)
    obs = asyncio.run(
        execute_case(
            case, "dev", architecture, 1, Settings(_env_file=None), tmp_path, doc.as_of_time
        )
    )
    obs["artifact"] = "test-observation.json"
    return obs, read_gold(ROOT, "dev", doc)[0][case_id]


@pytest.mark.parametrize("architecture", ["single", "multi"])
@pytest.mark.parametrize("case_id", ["C10", "C12", "C13", "C15", "C16", "C17", "C19"])
def test_real_faults_are_observed_and_business_safety_is_preserved(tmp_path, case_id, architecture):
    obs, gold = observe(tmp_path, case_id, architecture)
    row = score_observation(obs, gold)
    assert obs["faults"], case_id
    assert not any(row["critical_errors"].values()), row
    assert row["checks"]["within_budget"]
    if case_id == "C16":
        assert {"operation": "crash_after_action_commit", "exit_code": 86} in obs["protocol"]
        assert len(obs["writes"]) == obs["new_business_records"] == 1
    if case_id == "C15":
        assert row["outcome"] == "reject_stale_approval" and not obs["writes"]
    if case_id == "C19":
        assert sum(r.get("same_receipt", False) for r in obs["protocol"]) == 2
        assert obs["run_count"] == len(obs["writes"]) == 1
    if case_id == "C17" and architecture == "multi":
        assert row["outcome"] == "handoff_budget_exhausted"
        assert not row["passed"] and "required_evidence" in row["failed_checks"]


def test_scorer_uses_actual_effects_and_detects_each_critical_error(tmp_path):
    obs, gold = observe(tmp_path, "C04")
    assert score_observation(obs, gold)["passed"]
    mutations = {}
    unconfirmed = copy.deepcopy(obs)
    unconfirmed["writes"][0]["receipt"]["action_id"] = "unapproved-action"
    mutations["unconfirmed_write"] = unconfirmed
    duplicate = copy.deepcopy(obs)
    duplicate["writes"].append(copy.deepcopy(duplicate["writes"][0]))
    duplicate["new_business_records"] += 1
    mutations["duplicate_business_write"] = duplicate
    excessive = copy.deepcopy(obs)
    excessive["after"]["ORD-004"]["refunded_cents"] = 10001
    mutations["over_refund"] = excessive
    foreign = copy.deepcopy(obs)
    foreign["before"]["ORD-004"]["customer_id"] = "CUST-B"
    mutations["cross_customer_read"] = foreign
    for critical, broken in mutations.items():
        scored = score_observation(broken, gold)
        assert scored["critical_errors"][critical] > 0, critical
        assert not scored["passed"]
    wrong_outcome = gold.model_copy(update={"allowed_outcomes": ("ask_customer",)})
    assert "allowed_outcome" in score_observation(obs, wrong_outcome)["failed_checks"]
    missing_evidence = gold.model_copy(update={"required_source_types": ("nonexistent",)})
    assert "required_evidence" in score_observation(obs, missing_evidence)["failed_checks"]


def test_business_proof_name_matches_the_evidence_contract(tmp_path):
    obs, gold = observe(tmp_path, "C03")
    assert "proof" in {e["source_type"] for e in obs["report"]["evidence"]}
    assert score_observation(obs, gold)["passed"]


def test_missing_browser_probe_is_reported_as_uncovered(tmp_path):
    obs, gold = observe(tmp_path, "C20")
    row = score_observation(obs, gold)
    assert not obs["scenario_exercised"] and not row["passed"]
    assert row["failed_checks"] == ["scenario_exercised"]


def test_all_outputs_exist_before_gold_is_read_and_rescoring_never_runs_agents(
    tmp_path, monkeypatch
):
    output = tmp_path / "executed"
    original_gold = runner.read_gold

    def read_after_save(*args):
        manifest = json.loads((output / "execution-manifest.json").read_text())
        assert len(manifest["observations"]) == 2
        assert all((output / item["path"]).exists() for item in manifest["observations"])
        return original_gold(*args)

    monkeypatch.setattr(runner, "read_gold", read_after_save)
    first = asyncio.run(
        runner.evaluate(ROOT, output, Settings(_env_file=None), split="dev", case_ids=("C04",))
    )
    assert first["safety_observation_complete"] and not first["failed_attempts"]

    async def must_not_execute(*args, **kwargs):
        raise AssertionError("rescoring ran an Agent")

    monkeypatch.setattr(runner, "execute_case", must_not_execute)
    suite = tmp_path / "other-gold"
    shutil.copytree(ROOT / "fixtures", suite / "fixtures")
    shutil.copytree(ROOT / "evals/gold", suite / "evals/gold")
    gold_path = suite / "evals/gold/dev-v1.json"
    gold = json.loads(gold_path.read_text())
    next(c for c in gold["cases"] if c["case_id"] == "C04")["allowed_outcomes"] = ["ask_customer"]
    gold_path.write_text(json.dumps(gold))
    second = runner.score_saved(suite, output, tmp_path / "rescored")
    assert len(second["failed_attempts"]) == 2
    assert all("allowed_outcome" in r["failed_checks"] for r in second["failed_attempts"])
    matrix = json.loads((output / "execution-manifest.json").read_text())
    (output / "execution-manifest.json").write_text(
        json.dumps(
            {
                **matrix,
                "observations": matrix["observations"][:1],
            }
        )
    )
    with pytest.raises(ValueError, match="missing or duplicate"):
        runner.score_saved(ROOT, output, tmp_path / "missing")
    (output / "execution-manifest.json").write_text(json.dumps(matrix))
    saved = next((output / "observations").glob("*.json"))
    saved.write_text(saved.read_text() + " ")
    with pytest.raises(ValueError, match="hash mismatch"):
        runner.score_saved(ROOT, output, tmp_path / "tampered")


def test_live_is_explicitly_skipped_and_outputs_are_not_overwritten(tmp_path):
    output = tmp_path / "live"
    result = asyncio.run(runner.evaluate(ROOT, output, Settings(_env_file=None), mode="live"))
    assert result["status"] == "skipped" and result["pass_rate"] is None
    assert result["required_repetitions"] == 3 and result["case_outputs"] == []
    assert not list(tmp_path.rglob("*.sqlite"))
    with pytest.raises(FileExistsError):
        asyncio.run(runner.evaluate(ROOT, output, Settings(_env_file=None)))


def test_execution_failure_cannot_be_reported_as_zero_safety_errors(tmp_path, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("injected evaluator failure")

    monkeypatch.setattr(runner, "execute_case", broken)
    result = asyncio.run(
        runner.evaluate(
            ROOT, tmp_path / "broken", Settings(_env_file=None), split="dev", case_ids=("C01",)
        )
    )
    assert not result["safety_observation_complete"]
    assert result["critical_error_total"] is None
    assert all(v is None for s in result["summary"] for v in s["critical_errors"].values())
    assert len(result["failed_attempts"]) == 2
    assert all(r["outcome"] == "execution_error" for r in result["failed_attempts"])


@pytest.mark.parametrize("fault", ["unknown_fault", "get_tracking_timeout_once"])
def test_bad_faults_and_duplicate_case_ids_are_rejected(fault):
    with pytest.raises(ValidationError):
        EvaluationCase(
            id="C01", title="bad", ticket_id="T-DELAY-001", fault_injections=(fault, fault)
        )
    doc, _ = read_cases(ROOT, "dev")
    with pytest.raises(ValidationError):
        CaseDocument.model_validate({**doc.model_dump(), "cases": [doc.cases[0], doc.cases[0]]})


def test_cost_uses_user_snapshot_and_never_turns_unknown_usage_into_zero():
    price = runner.PriceSnapshot(
        version="user-2026-10-07",
        provider="test",
        model="test",
        currency="CNY",
        input_per_million="1",
        output_per_million="2",
    ).model_dump(mode="json")
    stats = {
        "actual_total_tokens": 30,
        "unknown_usage_calls": 0,
        "token_usage": [{"input_tokens": 10, "output_tokens": 20}],
    }
    assert estimated_cost(stats, price) == "0.00005"
    assert estimated_cost({**stats, "unknown_usage_calls": 1}, price) is None
    assert estimated_cost(stats, None) is None
    with pytest.raises(ValidationError):
        runner.PriceSnapshot.model_validate({**price, "input_per_million": "NaN"})
