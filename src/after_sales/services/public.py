"""Allowlisted projections and defensive redaction of customer-facing free text."""

import re

from after_sales.api.contracts import PendingView, ResultView


def redact(value):
    value = re.sub(
        r"(?i)(?<![A-Za-z0-9])(?:sk|bearer|api[_-]?key)[-:=\s]+[A-Za-z0-9_./+-]+",
        "[已隐藏]",
        value,
    )
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", value)
    return re.sub(r"(?<![0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9])", "[电话已隐藏]", value)


def pending_view(pending, principal):
    return PendingView(
        **{
            key: pending[key]
            for key in (
                "pending_id",
                "kind",
                "expected_role",
                "input_revision",
                "proposal_revision",
                "proposal_hash",
            )
        },
        questions=[
            {"field": q["field"], "question": redact(q["question"])} for q in pending["questions"]
        ],
        actions=[
            {
                "action_id": a["action_id"],
                "content_hash": a["content_hash"],
                **{k: a["candidate"][k] for k in ("type", "order_id", "amount_cents")},
                "policies": a["candidate"]["policy_refs"],
            }
            for a in pending["actions"]
        ],
        can_respond=(principal.role, principal.actor_id)
        == (pending["expected_role"], pending["expected_actor"]),
    )


def result_view(report):
    proposal = report.get("proposal") or {}
    stats = report["statistics"]
    review = report.get("review") or {}
    return ResultView(
        outcome=report["status"],
        decision=proposal.get("decision"),
        customer_reply=redact(proposal["customer_reply_draft"])
        if proposal.get("customer_reply_draft")
        else None,
        receipts=[
            {
                key: receipt.get(key)
                for key in (
                    "action_id",
                    "type",
                    "order_id",
                    "amount_cents",
                    "business_record_id",
                    "business_status",
                    "committed_at",
                )
            }
            for receipt in report.get("executed_actions", [])
        ],
        model_calls=stats["model_calls"],
        tool_calls=stats["tool_calls"],
        error_code=(report.get("error") or {}).get("code"),
        input_revision=report.get("input_revision"),
        proposal_revision=report.get("proposal_revision"),
        evidence=[evidence_view(e) for e in report.get("evidence", [])],
        gaps=[
            {"field": q["field"], "question": redact(q["question"])[:500]}
            for q in proposal.get("unresolved_questions", [])
        ],
        review_outcome=review.get("outcome"),
        review_issues=[redact(i["message"])[:500] for i in review.get("issues", [])],
        elapsed_ms=stats.get("elapsed_ms"),
        schema_repairs=stats.get("schema_repairs"),
        review_reworks=stats.get("review_repairs_reserved"),
    )


def evidence_view(item):
    """Display trusted source metadata and small summaries, never raw evidence payloads."""
    source = item["source_type"]
    facts = item["facts"]
    labels = {
        "shipped": "已发货",
        "delivered": "已签收",
        "lost": "已确认丢失",
        "cancelled": "已取消",
        "present": "有签收凭证",
        "missing": "缺少签收凭证",
        "unknown": "凭证状态未知",
    }
    summary = "已采集来源快照"
    if source == "order":
        summary = "订单状态：" + labels.get(facts.get("status"), "待核实")
    elif source == "proof":
        summary = labels.get(facts.get("proof_status"), "凭证状态未知")
    elif source in {"tracking", "products", "history"}:
        key = {"tracking": "events", "products": "products", "history": "records"}[source]
        summary = f"已采集 {len(facts.get(key, []))} 条记录"
    elif source == "policy":
        summary = redact(facts.get("title", "售后政策"))[:200]
    return {
        **{
            key: redact(str(item[key]))[:200]
            for key in ("source_type", "source_id", "source_version", "observed_at")
        },
        "evidence_id": item["id"],
        "summary": summary,
    }
