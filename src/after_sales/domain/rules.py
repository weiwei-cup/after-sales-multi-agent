"""Deterministic, three-valued checks. These functions never execute business actions."""

from datetime import datetime, timedelta
from enum import StrEnum

from after_sales.domain.models import (
    ActionType,
    AfterSalesRecord,
    DomainModel,
    Order,
    OrderStatus,
    Policy,
    Product,
    TicketType,
    TrackingEvent,
)

RULES_VERSION = "rules-v1"


class Truth(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class Condition(DomainModel):
    name: str
    value: Truth
    reason: str


class PolicyEvaluation(DomainModel):
    policy_id: str
    policy_version: int
    action: ActionType
    eligibility: Truth
    conditions: tuple[Condition, ...]
    remaining_refund_cents: int
    requested_amount_cents: int | None
    existing_record_ids: tuple[str, ...]


def conjunction(values: list[Truth]) -> Truth:
    if Truth.FALSE in values:
        return Truth.FALSE
    return Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.TRUE


def check(name: str, value: bool | None, reason: str) -> Condition:
    truth = Truth.UNKNOWN if value is None else Truth.TRUE if value else Truth.FALSE
    return Condition(name=name, value=truth, reason=reason)


def return_window(order: Order, hours: int, as_of_time: datetime) -> Condition:
    if order.status != OrderStatus.DELIVERED:
        return check("return_window", False, "order_not_received")
    if order.received_at is None:
        return check("return_window", None, "received_at_missing")
    elapsed = as_of_time - order.received_at
    if elapsed < timedelta(0):
        return check("return_window", None, "received_at_in_future")
    return check("return_window", elapsed <= timedelta(hours=hours), f"window_hours={hours}")


def evaluate_policy(
    policy: Policy,
    action: ActionType,
    *,
    intent: TicketType,
    order: Order,
    products: tuple[Product, ...] | None,
    tracking: tuple[TrackingEvent, ...] | None,
    history: tuple[AfterSalesRecord, ...] | None,
    as_of_time: datetime,
    requested_amount_cents: int | None = None,
) -> PolicyEvaluation:
    """Only application-loaded facts belong here; tools accept references to those facts."""
    active = policy.effective_from <= as_of_time and (
        policy.effective_to is None or as_of_time < policy.effective_to
    )
    conditions = [
        check("policy_active", active, "effective_period_is_start_inclusive_end_exclusive"),
        check("intent_scope", intent in policy.scope, "ticket_intent"),
        check("action_allowed", action in policy.allowed_actions, "explicit_policy_action"),
    ]
    item_ids = {item.product_id for item in order.items}
    scoped = set(policy.scope_product_ids)
    scope_match = not scoped or bool(item_ids & scoped)
    whole_order_scope = scope_match and (not scoped or item_ids <= scoped)
    conditions.append(
        check(
            "product_scope",
            True if whole_order_scope else None if scope_match else False,
            "partial_order_scope_needs_review"
            if scope_match and not whole_order_scope
            else "scope",
        )
    )
    if policy.conditions.window_hours is not None:
        conditions.append(return_window(order, policy.conditions.window_hours, as_of_time))
    if policy.conditions.product_categories:
        by_id = {product.id: product for product in products or ()}
        category_values = [
            None
            if item.product_id not in by_id
            else by_id[item.product_id].category in policy.conditions.product_categories
            for item in order.items
        ]
        value = False if False in category_values else None if None in category_values else True
        conditions.append(check("product_category", value, "all_order_items_must_match"))
    if policy.conditions.require_unopened:
        values = [item.unopened for item in order.items]
        value = False if False in values else None if None in values else True
        conditions.append(check("unopened", value, "trusted_order_item_state"))
    if policy.conditions.require_confirmed_lost:
        confirmed = (
            None
            if tracking is None
            else any(
                event.event_type == "investigation_confirmed_lost"
                and event.source == "mock_carrier_investigation"
                and event.occurred_at <= as_of_time
                for event in tracking
            )
        )
        conditions.append(
            check("confirmed_lost", confirmed, "requires_trusted_investigation_event")
        )
    if policy.id == "LOGISTICS-DELAY":
        overdue = (
            False
            if order.status != OrderStatus.SHIPPED
            else None
            if order.expected_delivery_at is None
            else as_of_time > order.expected_delivery_at
        )
        conditions.append(check("overdue", overdue, "delay_alone_does_not_confirm_loss"))

    remaining = order.paid_cents - order.refunded_cents
    amount = requested_amount_cents
    if action == ActionType.MOCK_REFUND:
        amount = remaining if amount is None else amount
        conditions.append(
            check("refund_amount", 0 < amount <= remaining, "positive_and_within_balance")
        )
    elif amount is not None:
        conditions.append(check("amount_not_applicable", False, "only_refund_accepts_an_amount"))

    existing = tuple(
        record.id
        for record in history or ()
        if record.type == action
        and record.created_at <= as_of_time
        and (
            record.status == "active"
            or (action == ActionType.RETURN_REQUEST and record.status == "completed")
        )
    )
    history_known = history is not None and all(
        record.created_at <= as_of_time for record in history
    )
    conditions.append(
        check("no_existing_application", not existing if history_known else None, "history_query")
    )
    return PolicyEvaluation(
        policy_id=policy.id,
        policy_version=policy.version,
        action=action,
        eligibility=conjunction([condition.value for condition in conditions]),
        conditions=tuple(conditions),
        remaining_refund_cents=remaining,
        requested_amount_cents=amount,
        existing_record_ids=existing,
    )


def policy_conflicts(policies: tuple[Policy, ...]) -> list[dict[str, object]]:
    """Keep overlapping rules with different restrictions; never invent a precedence."""
    conflicts = []
    for index, left in enumerate(policies):
        for right in policies[index + 1 :]:
            shared = sorted(set(left.allowed_actions) & set(right.allowed_actions))
            if shared and left.conditions != right.conditions:
                conflicts.append(
                    {
                        "policy_refs": [
                            {"policy_id": left.id, "version": left.version},
                            {"policy_id": right.id, "version": right.version},
                        ],
                        "actions": shared,
                        "reason": "overlapping_policies_have_different_conditions",
                    }
                )
    return conflicts
