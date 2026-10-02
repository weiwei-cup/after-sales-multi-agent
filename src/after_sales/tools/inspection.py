"""A code-driven investigation through exactly the tools exposed to the P03 Agent."""

from after_sales.domain.models import ActionType
from after_sales.tools.contracts import ErrorCode, ToolResult, failed
from after_sales.tools.service import ToolSession


async def inspect_ticket(session: ToolSession) -> dict[str, object]:
    context = session.context
    results: dict[str, ToolResult] = {}
    order_id = context.supplied_order_id
    query = {"order_id": order_id} if order_id else {}
    # Missing input is a business gap, not an invalid model argument.
    if order_id is None:
        results["get_order"] = failed(ErrorCode.MISSING_REFERENCE, "当前工单缺少订单号")
    else:
        results["get_order"] = await session.call("get_order", query)
    if results["get_order"].ok:
        for name in (
            "get_order_products",
            "get_tracking",
            "get_delivery_proof",
            "get_after_sales_history",
        ):
            results[name] = await session.call(name, query)
        results["search_policies"] = await session.call(
            "search_policies", {"intent": context.intent}
        )
        policies = results["search_policies"]
        if policies.ok:
            actions = sorted(
                {
                    action
                    for policy in policies.data["policies"]
                    for action in policy["allowed_actions"]
                }
            )
            for action in actions:
                arguments = {
                    "action": ActionType(action),
                    "order_ref": results["get_order"].evidence_refs[0],
                    "policy_refs": tuple(
                        ref
                        for policy, ref in zip(
                            policies.data["policies"], policies.evidence_refs, strict=True
                        )
                        if action in policy["allowed_actions"]
                    ),
                }
                for field, name in (
                    ("products_ref", "get_order_products"),
                    ("tracking_ref", "get_tracking"),
                    ("history_ref", "get_after_sales_history"),
                ):
                    if results[name].ok:
                        arguments[field] = results[name].evidence_refs[0]
                results[f"evaluate_policy:{action}"] = await session.call(
                    "evaluate_policy", arguments
                )
    return {
        "phase": "P02",
        "ticket_id": context.ticket_id,
        "session_id": context.session_id,
        "as_of_time": context.model_dump(mode="json")["as_of_time"],
        "model_calls": 0,
        "candidate_only": True,
        "query_ok": all(result.ok for result in results.values()),
        "results": {name: result.model_dump(mode="json") for name, result in results.items()},
        "evidence": session.evidence.export(context),
    }
