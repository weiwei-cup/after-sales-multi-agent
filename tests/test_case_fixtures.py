import json
from pathlib import Path

import pytest

from after_sales.repositories.seed import load_demo_dataset

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def load_json(relative):
    return json.loads((ROOT / relative).read_text())


def test_development_and_holdout_cases_have_separate_matching_gold():
    development = load_json("fixtures/dev-cases-v1.json")
    holdout = load_json("evals/holdout/cases-v1.json")
    dev_gold = load_json("evals/gold/dev-v1.json")
    holdout_gold = load_json("evals/gold/holdout-v1.json")
    dev_ids = {case["id"] for case in development["cases"]}
    holdout_ids = {case["id"] for case in holdout["cases"]}
    assert len(dev_ids) == 20
    assert len(holdout_ids) == 10
    assert dev_ids.isdisjoint(holdout_ids)
    assert dev_ids == {case["case_id"] for case in dev_gold["cases"]}
    assert holdout_ids == {case["case_id"] for case in holdout_gold["cases"]}
    for document in (development, holdout, dev_gold, holdout_gold):
        assert document["dataset_version"] == "demo-v1"
    for document in (dev_gold, holdout_gold):
        for case in document["cases"]:
            assert case["allowed_outcomes"]
            if case["allowed_actions"]:
                assert case["requires_operator_approval"] is True


def test_development_inputs_match_tickets_and_holdout_orders_are_distinct():
    sample = load_demo_dataset()
    dev_cases = load_json("fixtures/dev-cases-v1.json")["cases"]
    holdout = load_json("evals/holdout/cases-v1.json")["cases"]
    assert {case["ticket_id"] for case in dev_cases} == {ticket.id for ticket in sample.tickets}
    dev_orders = {ticket.supplied_order_id for ticket in sample.tickets if ticket.supplied_order_id}
    orders = {order.id: order for order in sample.orders}
    for case in holdout:
        request = case["request"]
        assert request["supplied_order_id"] not in dev_orders
        if request["supplied_order_id"] == "ORD-DOES-NOT-EXIST":
            assert request["supplied_order_id"] not in orders
        else:
            assert orders[request["supplied_order_id"]].customer_id == request["customer_id"]
