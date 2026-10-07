"""Run first, persist every observation, then load independent gold and score."""

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import traceback
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from statistics import mean, median
from typing import Literal
from uuid import uuid4

from pydantic import Field

from after_sales.config import Settings
from after_sales.domain.models import DomainModel
from after_sales.evaluation.engine import execute_case
from after_sales.evaluation.scoring import CRITICAL, estimated_cost, score_observation
from after_sales.evaluation.suite import read_cases, read_gold, write_new_json

METRICS = (
    "model_calls",
    "tool_calls",
    "validation_tool_calls",
    "schema_repairs",
    "review_repairs_reserved",
    "active_elapsed_ms",
    "model_elapsed_ms",
    "tool_elapsed_ms",
    "actual_total_tokens",
    "known_total_tokens",
    "unknown_usage_calls",
    "token_budget_charged",
    "peak_in_flight",
    "stop_reason",
)


class PriceSnapshot(DomainModel):
    version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: Literal["per_million_tokens"] = "per_million_tokens"
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0, allow_inf_nan=False)


def source_snapshot(root):
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", str(root), "status", "--porcelain"], text=True, timeout=5
            ).strip()
        )
        return {"commit": sha, "working_tree_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "working_tree_dirty": None}


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["split"], row["architecture"])].append(row)
    summaries = []
    for (split, architecture), items in sorted(groups.items()):
        measured = [r for r in items if r.get("critical_errors") is not None]
        active = [r["statistics"]["active_elapsed_ms"] for r in measured]
        wall = [r["wall_elapsed_ms"] for r in measured]
        by_case = defaultdict(list)
        for row in items:
            by_case[row["case_id"]].append(row)
        summaries.append(
            {
                "split": split,
                "architecture": architecture,
                "distinct_cases": len(by_case),
                "attempts": len(items),
                "observed_attempts": len(measured),
                "passed": sum(r["passed"] for r in items),
                "pass_rate": round(sum(r["passed"] for r in items) / len(items), 4),
                "consistent_cases": sum(
                    len({(r["outcome"], r["status"], r["passed"]) for r in rs}) == 1
                    for rs in by_case.values()
                ),
                "fully_passing_cases": sum(all(r["passed"] for r in rs) for rs in by_case.values()),
                "critical_errors": {
                    k: sum(r["critical_errors"][k] for r in measured)
                    if len(measured) == len(items)
                    else None
                    for k in CRITICAL
                },
                "safety_observation_complete": len(measured) == len(items),
                "mean_model_calls": round(mean(r["statistics"]["model_calls"] for r in measured), 3)
                if measured
                else None,
                "mean_tool_calls": round(mean(r["statistics"]["tool_calls"] for r in measured), 3)
                if measured
                else None,
                "active_ms_p50": round(median(active), 3) if active else None,
                "active_ms_p95": round(sorted(active)[math.ceil(len(active) * 0.95) - 1], 3)
                if active
                else None,
                "wall_ms_p50": round(median(wall), 3) if wall else None,
                "actual_total_tokens": sum(r["statistics"]["actual_total_tokens"] for r in measured)
                if measured
                and all(r["statistics"].get("actual_total_tokens") is not None for r in measured)
                else None,
                "known_total_tokens": sum(
                    r["statistics"].get("known_total_tokens", 0) for r in measured
                ),
                "unknown_usage_calls": sum(
                    r["statistics"].get("unknown_usage_calls", 0) for r in measured
                ),
                "estimated_cost": None
                if any(r.get("estimated_cost") is None for r in items)
                else str(sum(Decimal(r["estimated_cost"]) for r in items)),
            }
        )
    return summaries


def markdown_report(document):
    lines = [
        "# P10 离线对照评估",
        "",
        "模型：ScriptedChatModel；同一业务资料、判断时间、规则、审批和动作服务。"
        "单 Agent 使用代码审核，多 Agent 另有模型审核；共享确定性审核下限。",
        "",
        "所有运行输出保存后才读取 gold。暂停等待客户属于合法停止；"
        "操作员确认由评估驱动器模拟，未接入真实支付。",
        "",
        "| 数据集 | 架构 | 案例 | 尝试 | 通过 | 比例 | 模型调用均值 | "
        "工具调用均值 | 活动耗时 P50 ms | 关键错误 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in document["summary"]:
        safety = (
            str(sum(row["critical_errors"].values()))
            if row["safety_observation_complete"]
            else "未完整观测"
        )
        lines.append(
            f"| {row['split']} | {row['architecture']} | {row['distinct_cases']} | "
            f"{row['attempts']} | {row['passed']} | {row['pass_rate']:.1%} | "
            f"{row['mean_model_calls']} | {row['mean_tool_calls']} | "
            f"{row['active_ms_p50']} | {safety} |"
        )
    lines += [
        "",
        "## 失败与覆盖缺口",
        "",
        "| Case | 架构 | 重复 | 实际结果 | 未通过检查 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for row in document["results"]:
        if not row["passed"]:
            lines.append(
                f"| {row['case_id']} | {row['architecture']} | {row['repetition']} | "
                f"{row['outcome']} | {', '.join(row['failed_checks'])} |"
            )
    lines += [
        "",
        "## 解释范围",
        "",
        "- 脚本模型输出可重复，次数和耗时反映当前编排的工程开销。"
        "不能证明真实模型语言理解、鲁棒性或多 Agent 的普遍优势。",
        "- 单/多 Agent 交替先后执行；P50/P95 来自本次机器的样本，不是速度保证。"
        "活动耗时排除人工等待；wall 含准备、协议、恢复和可选浏览器。",
        "- token 未报告时总量与费用为 null；known_total_tokens 只表示已报告部分。"
        "价格仅接受用户配置快照，不在线猜测收费。",
        "- C17 两种架构均用两次模型额度；多 Agent 的 intake 后仅一个额度供两分支竞争。"
        "单 Agent 依真实调用顺序耗尽同一总额度。",
        "- 未启用 --browser 时 C20 刷新未验证，scenario_exercised=false，"
        "计入未通过，不冒充浏览器覆盖。",
        "- 真人证据标注、真实模型每例至少三次及 ≥90% 目标均待执行；模型裁判未使用。",
        "",
    ]
    return "\n".join(lines)


def score_saved(root: Path, observations: Path, output: Path, *, price=None):
    manifest = json.loads((observations / "execution-manifest.json").read_text())
    if manifest["schema_version"] != "evaluation-execution-v1":
        raise ValueError("unsupported execution manifest")
    # Verify every observation before opening any gold file.
    saved = []
    documents = {}
    for split, expected_hash in manifest["case_hashes"].items():
        cases, actual_hash = read_cases(root, split)
        if actual_hash != expected_hash:
            raise ValueError("evaluation inputs changed; refuse mismatched rescoring")
        documents[split] = cases
    for item in manifest["observations"]:
        path = observations / item["path"]
        if path.resolve().parent != (observations / "observations").resolve():
            raise ValueError("observation path must stay inside its observation directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("saved observation hash mismatch")
        saved.append(json.loads(raw))
    # A deleted/duplicated cell is a coverage error, even if all remaining hashes match.
    selection, architectures = manifest.get("case_selection"), manifest.get("architectures")
    if (
        not selection
        or not any(selection.values())
        or not architectures
        or len(set(architectures)) != len(architectures)
    ):
        raise ValueError("execution manifest is missing its evaluation matrix")
    if set(architectures) - {"single", "multi"} or set(selection) != set(documents):
        raise ValueError("invalid evaluation matrix")
    if not 1 <= manifest["repetitions"] <= 100:
        raise ValueError("invalid evaluation repetition count")
    for split, ids in selection.items():
        if len(ids) != len(set(ids)) or set(ids) - {c.id for c in documents[split].cases}:
            raise ValueError("invalid selected evaluation cases")
    expected = {
        (split, case_id, architecture, repetition)
        for split, ids in selection.items()
        for case_id in ids
        for architecture in architectures
        for repetition in range(1, manifest["repetitions"] + 1)
    }
    actual = [
        (
            obs["split"],
            obs["case_id"],
            obs.get("architecture") or obs["report"]["architecture"],
            obs["repetition"],
        )
        for obs in saved
    ]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("evaluation matrix has missing or duplicate observations")
    golds, gold_hashes = {}, {}
    for split, cases in documents.items():
        golds[split], gold_hashes[split] = read_gold(root, split, cases)
    rows = []
    for observation in saved:
        if observation.get("execution_error"):
            row = {
                "case_id": observation["case_id"],
                "split": observation["split"],
                "architecture": observation["architecture"],
                "repetition": observation["repetition"],
                "outcome": "execution_error",
                "status": "failed",
                "passed": False,
                "checks": {"execution_completed": False},
                "failed_checks": [observation["execution_error"]],
                "critical_errors": None,
                "statistics": {},
                "estimated_cost": None,
                "artifact": observation["artifact"],
            }
        else:
            row = score_observation(
                observation, golds[observation["split"]][observation["case_id"]]
            )
            row["estimated_cost"] = estimated_cost(row["statistics"], price)
            row["statistics"] = {key: row["statistics"].get(key) for key in METRICS}
        rows.append(row)
    document = {
        "schema_version": "evaluation-report-v1",
        "phase": "P10",
        "model_mode": "scripted",
        "source": manifest["source"],
        "packages": manifest["packages"],
        "budget": manifest["budget"],
        "case_hashes": manifest["case_hashes"],
        "gold_hashes": gold_hashes,
        "repetitions": manifest["repetitions"],
        "browser_enabled": manifest["browser_enabled"],
        "scoring_version": "scoring-v1",
        "gold_access_order": "after_all_observations_saved_and_verified",
        "live_evaluation": {
            "status": "skipped",
            "code": "LIVE_PROVIDER_DEFERRED",
            "required_repetitions": 3,
        },
        "pricing_snapshot": price,
        "summary": summarize(rows),
        "results": rows,
    }
    write_new_json(output / "evaluation.json", document)
    with (output / "report.md").open("x") as out:
        out.write(markdown_report(document))
    return {
        "status": "completed",
        "report_path": str((output / "evaluation.json").resolve()),
        "markdown_path": str((output / "report.md").resolve()),
        "summary": document["summary"],
        "failed_attempts": [
            {k: r[k] for k in ("case_id", "architecture", "repetition", "outcome", "failed_checks")}
            for r in rows
            if not r["passed"]
        ],
        "safety_observation_complete": all(
            s["safety_observation_complete"] for s in document["summary"]
        ),
        "critical_error_total": sum(sum(s["critical_errors"].values()) for s in document["summary"])
        if all(s["safety_observation_complete"] for s in document["summary"])
        else None,
    }


async def evaluate(
    root: Path,
    output: Path,
    settings: Settings,
    *,
    split="all",
    architectures=("single", "multi"),
    repetitions=1,
    case_ids=(),
    browser=False,
    mode="scripted",
    price=None,
):
    if (
        not 1 <= repetitions <= 100
        or not architectures
        or len(set(architectures)) != len(architectures)
        or set(architectures) - {"single", "multi"}
    ):
        raise ValueError("invalid evaluation repetitions or architectures")
    output.mkdir(parents=True, exist_ok=False)
    if mode == "live":
        result = {
            "status": "skipped",
            "code": "LIVE_PROVIDER_DEFERRED",
            "requested_repetitions": repetitions,
            "required_repetitions": 3,
            "case_outputs": [],
            "pass_rate": None,
            "message": "用户选择继续离线；真实 provider、真人标注和真实模型重复评估未执行。",
        }
        write_new_json(output / "live-skipped.json", result)
        return result
    if mode != "scripted" or split not in {"dev", "holdout", "all"}:
        raise ValueError("unsupported evaluation mode or split")
    splits = ("dev", "holdout") if split == "all" else (split,)
    documents, hashes = {}, {}
    for name in splits:
        documents[name], hashes[name] = read_cases(root, name)
    ids = [case.id for doc in documents.values() for case in doc.cases]
    if len(set(ids)) != len(ids) or set(case_ids) - set(ids):
        raise ValueError("duplicate cross-split IDs or unknown requested case")
    manifest = {
        "schema_version": "evaluation-execution-v1",
        "source": source_snapshot(root),
        "packages": {n: version(n) for n in ("langchain", "langgraph", "after-sales-multi-agent")},
        "budget": {n: getattr(settings, n) for n in EVAL_LIMIT_FIELDS},
        "case_hashes": hashes,
        "repetitions": repetitions,
        "browser_enabled": browser,
        "architectures": list(architectures),
        "case_selection": {
            name: [c.id for c in doc.cases if not case_ids or c.id in case_ids]
            for name, doc in documents.items()
        },
        "observations": [],
    }
    for name, document in documents.items():
        for case in document.cases:
            if case_ids and case.id not in case_ids:
                continue
            for repetition in range(1, repetitions + 1):
                ordered = tuple(architectures) if repetition % 2 else tuple(reversed(architectures))
                for architecture in ordered:
                    artifact = f"{name}-{case.id}-{architecture}-{repetition:03d}"
                    artifact_path = output / "observations" / (artifact + ".json")
                    try:
                        with tempfile.TemporaryDirectory(prefix="after-sales-eval-") as temporary:
                            cell = Path(temporary)
                            observation = await execute_case(
                                case,
                                name,
                                architecture,
                                repetition,
                                settings,
                                cell,
                                document.as_of_time,
                                browser=browser,
                            )
                            if (cell / "browser-refresh.png").exists():
                                (output / "screenshots").mkdir(exist_ok=True)
                                shutil.copyfile(
                                    cell / "browser-refresh.png",
                                    output / "screenshots" / (artifact + ".png"),
                                )
                    except Exception as error:
                        (output / "errors").mkdir(exist_ok=True)
                        (output / "errors" / (artifact + ".log")).write_text(traceback.format_exc())
                        observation = {
                            "schema_version": "evaluation-observation-v1",
                            "case_id": case.id,
                            "split": name,
                            "architecture": architecture,
                            "repetition": repetition,
                            "execution_error": type(error).__name__,
                        }
                    observation["artifact"] = "observations/" + artifact + ".json"
                    write_new_json(artifact_path, observation)
                    manifest["observations"].append(
                        {
                            "path": observation["artifact"],
                            "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                        }
                    )
    # The execution manifest contains no expectations. Gold is only opened below.
    write_new_json(output / "execution-manifest.json", manifest)
    return score_saved(root, output, output, price=price)


EVAL_LIMIT_FIELDS = (
    "max_model_calls",
    "max_tool_calls",
    "review_repair_limit",
    "proposal_repair_limit",
    "max_concurrency",
    "token_budget",
    "model_token_reservation",
    "active_time_budget_seconds",
    "transient_retry_limit",
    "retry_backoff_seconds",
    "model_timeout_seconds",
    "tool_timeout_seconds",
    "tool_max_result_bytes",
)


def default_output():
    return Path("evals/output") / (
        "p10-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    )
