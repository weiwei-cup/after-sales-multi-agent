from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from after_sales.domain.models import DemoDataset, Order, Run, RunStatus, TicketStatus, UtcTime
from after_sales.repositories.seed import load_demo_dataset

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", [-1, 10.01, 10.0, True, "100", 9_223_372_036_854_775_808])
def test_money_rejects_negative_float_bool_string_and_sqlite_overflow(value):
    sample = load_demo_dataset().orders[0].model_dump(mode="json")
    sample["paid_cents"] = value
    with pytest.raises(ValidationError):
        Order.model_validate(sample)


def test_refund_total_cannot_exceed_paid_amount():
    sample = load_demo_dataset().orders[0].model_dump(mode="json")
    sample["refunded_cents"] = sample["paid_cents"] + 1
    with pytest.raises(ValidationError, match="exceed"):
        Order.model_validate(sample)


def test_aware_time_is_normalized_and_naive_time_is_rejected():
    adapter = TypeAdapter(UtcTime)
    local = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=8)))
    assert adapter.validate_python(local) == datetime(2026, 10, 2, 4, tzinfo=UTC)
    assert adapter.validate_python("2026-10-02T12:00:00+08:00").utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        adapter.validate_python("2026-10-02T12:00:00")


def test_invalid_customer_order_link_is_rejected():
    sample = load_demo_dataset().model_dump(mode="json")
    sample["tickets"][0]["customer_id"] = "CUST-B"
    with pytest.raises(ValidationError, match="belong"):
        DemoDataset.model_validate(sample)


def test_untrusted_order_reference_can_be_preserved_without_verified_link():
    ticket = load_demo_dataset().tickets[7]
    assert ticket.customer_id == "CUST-A"
    assert ticket.supplied_order_id == "ORD-008"
    assert ticket.order_id is None


def test_dataset_rejects_duplicate_ids_and_inconsistent_refund_history():
    sample = load_demo_dataset().model_dump(mode="json")
    sample["customers"].append(sample["customers"][0])
    with pytest.raises(ValidationError, match="unique"):
        DemoDataset.model_validate(sample)
    sample = load_demo_dataset().model_dump(mode="json")
    sample["orders"][22]["refunded_cents"] = 0
    with pytest.raises(ValidationError, match="refund totals"):
        DemoDataset.model_validate(sample)


def test_business_status_and_run_status_are_separate():
    run = Run(
        id="RUN-001",
        ticket_id="T-RETURN-001",
        thread_id="THREAD-001",
        status=RunStatus.COMPLETED,
        as_of_time="2026-10-02T04:00:00Z",
        created_at="2026-10-02T04:00:00Z",
    )
    assert run.status == RunStatus.COMPLETED
    assert TicketStatus.WAITING_RETURN.value != run.status.value


def test_fixture_has_boundary_cases_and_versioned_policies():
    sample = load_demo_dataset()
    assert len(sample.orders) == 30
    assert len(sample.tickets) == 20
    assert sample.fixture_seed == 42
    orders = {order.id: order for order in sample.orders}
    assert sample.as_of_time - orders["ORD-024"].received_at == timedelta(days=7)
    assert sample.as_of_time - orders["ORD-025"].received_at == timedelta(days=7, seconds=1)
    assert orders["ORD-023"].paid_cents - orders["ORD-023"].refunded_cents == 8900
    assert orders["ORD-030"].paid_cents == orders["ORD-030"].refunded_cents
    assert orders["ORD-028"].received_at is None
    assert {(p.id, p.version) for p in sample.policies} >= {
        ("RETURN-STANDARD", 1),
        ("RETURN-STANDARD", 2),
        ("RETURN-CONFLICT", 1),
    }
