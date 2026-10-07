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
            }
            for a in pending["actions"]
        ],
        can_respond=(principal.role, principal.actor_id)
        == (pending["expected_role"], pending["expected_actor"]),
    )


def result_view(report):
    proposal = report.get("proposal") or {}
    stats = report["statistics"]
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
    )
