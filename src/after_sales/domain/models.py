"""Money is integer cents; all business timestamps are aware and normalized to UTC."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Cents = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
UtcTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TicketType(StrEnum):
    DELAY = "logistics_delay"
    NOT_RECEIVED = "delivered_not_received"
    RETURN = "return_request"
    UNKNOWN = "unknown"


class TicketStatus(StrEnum):
    NEW = "new"
    PROCESSING = "processing"
    WAITING_CUSTOMER = "waiting_customer"
    WAITING_REVIEW = "waiting_review"
    WAITING_RETURN = "waiting_return"
    RESOLVED = "resolved"
    HANDED_OFF = "handed_off"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ProductCategory(StrEnum):
    APPAREL = "apparel"
    ELECTRONICS = "electronics"
    HYGIENE = "hygiene"


class OrderStatus(StrEnum):
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    LOST = "lost"
    CANCELLED = "cancelled"


class ActionType(StrEnum):
    LOGISTICS_CASE = "open_logistics_case"
    RETURN_REQUEST = "create_return_request"
    MOCK_REFUND = "issue_mock_refund"


class Customer(DomainModel):
    id: Identifier
    display_name: str = Field(min_length=1, max_length=100)


class Product(DomainModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    category: ProductCategory


class OrderItem(DomainModel):
    product_id: Identifier
    quantity: PositiveInt
    unit_price_cents: Cents
    unopened: bool | None = None


class Order(DomainModel):
    id: Identifier
    customer_id: Identifier
    items: tuple[OrderItem, ...] = Field(min_length=1)
    paid_cents: Cents
    refunded_cents: Cents = 0
    status: OrderStatus
    created_at: UtcTime
    expected_delivery_at: UtcTime | None = None
    received_at: UtcTime | None = None
    version: PositiveInt = 1

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.refunded_cents > self.paid_cents:
            raise ValueError("refunded_cents cannot exceed paid_cents")
        if self.received_at and self.received_at < self.created_at:
            raise ValueError("received_at cannot precede created_at")
        if len({item.product_id for item in self.items}) != len(self.items):
            raise ValueError("order items must have unique product IDs")
        return self


class TrackingEvent(DomainModel):
    id: Identifier
    order_id: Identifier
    event_type: str = Field(min_length=1, max_length=50)
    occurred_at: UtcTime
    description: str = Field(min_length=1, max_length=2000)
    source: str = "mock_carrier"
    version: PositiveInt = 1


class DeliveryProof(DomainModel):
    order_id: Identifier
    proof_status: Annotated[str, Field(pattern=r"^(present|missing|unknown)$")]
    description: str
    observed_at: UtcTime
    source: str = "mock_carrier"
    version: PositiveInt = 1


class PolicyConditions(DomainModel):
    window_hours: PositiveInt | None = None
    product_categories: tuple[ProductCategory, ...] = ()
    require_unopened: bool = False
    require_confirmed_lost: bool = False


class Policy(DomainModel):
    id: Identifier
    version: PositiveInt
    title: str = Field(min_length=1)
    effective_from: UtcTime
    effective_to: UtcTime | None = None
    scope: tuple[TicketType, ...] = Field(min_length=1)
    scope_product_ids: tuple[Identifier, ...] = ()
    conditions: PolicyConditions
    allowed_actions: tuple[ActionType, ...]
    clauses: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_effective_period(self) -> Self:
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        return self


class TicketMessage(DomainModel):
    id: Identifier
    ticket_id: Identifier
    role: Annotated[str, Field(pattern=r"^(customer|staff)$")]
    content: str = Field(min_length=1, max_length=4000)
    created_at: UtcTime


class Ticket(DomainModel):
    id: Identifier
    customer_id: Identifier
    supplied_order_id: Identifier | None = None
    order_id: Identifier | None = None
    type: TicketType
    status: TicketStatus = TicketStatus.NEW
    messages: tuple[TicketMessage, ...] = Field(min_length=1)
    created_at: UtcTime
    input_revision: PositiveInt = 1
    version: PositiveInt = 1

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if self.order_id and self.supplied_order_id and self.order_id != self.supplied_order_id:
            raise ValueError("verified order must match the supplied reference")
        if any(message.ticket_id != self.id for message in self.messages):
            raise ValueError("messages must belong to the ticket")
        return self


class AfterSalesRecord(DomainModel):
    id: Identifier
    order_id: Identifier
    type: ActionType
    status: Annotated[str, Field(pattern=r"^(active|completed|cancelled)$")]
    amount_cents: Cents | None = None
    created_at: UtcTime
    version: PositiveInt = 1

    @model_validator(mode="after")
    def validate_amount(self) -> Self:
        if self.type == ActionType.MOCK_REFUND:
            if self.amount_cents is None or self.amount_cents == 0:
                raise ValueError("refund history requires a positive amount")
        elif self.amount_cents is not None:
            raise ValueError("non-refund history must not carry an amount")
        return self


class Run(DomainModel):
    id: Identifier
    ticket_id: Identifier
    thread_id: Identifier
    status: RunStatus = RunStatus.QUEUED
    as_of_time: UtcTime
    created_at: UtcTime
    workflow_version: PositiveInt = 1


class DemoDataset(DomainModel):
    dataset_id: Annotated[str, Field(pattern=r"^demo$")]
    version: Identifier
    fixture_seed: Annotated[int, Field(strict=True, ge=0)]
    as_of_time: UtcTime
    customers: tuple[Customer, ...]
    products: tuple[Product, ...]
    orders: tuple[Order, ...]
    tracking_events: tuple[TrackingEvent, ...]
    delivery_proofs: tuple[DeliveryProof, ...]
    policies: tuple[Policy, ...]
    tickets: tuple[Ticket, ...]
    after_sales_history: tuple[AfterSalesRecord, ...]

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        collections = (
            self.customers,
            self.products,
            self.orders,
            self.tracking_events,
            self.tickets,
            self.after_sales_history,
        )
        for records in collections:
            if len({record.id for record in records}) != len(records):
                raise ValueError("dataset IDs must be unique within each collection")
        if len({(p.id, p.version) for p in self.policies}) != len(self.policies):
            raise ValueError("policy ID and version pairs must be unique")
        if len({p.order_id for p in self.delivery_proofs}) != len(self.delivery_proofs):
            raise ValueError("delivery proof must be unique per order")
        customers = {record.id for record in self.customers}
        products = {record.id for record in self.products}
        orders = {record.id: record for record in self.orders}
        for order in self.orders:
            if order.customer_id not in customers:
                raise ValueError("order references an unknown customer")
            if any(item.product_id not in products for item in order.items):
                raise ValueError("order references an unknown product")
        for ticket in self.tickets:
            if ticket.customer_id not in customers:
                raise ValueError("ticket references an unknown customer")
            if ticket.order_id:
                order = orders.get(ticket.order_id)
                if order is None or order.customer_id != ticket.customer_id:
                    raise ValueError("verified order must belong to the ticket customer")
        for record in (*self.tracking_events, *self.delivery_proofs, *self.after_sales_history):
            if record.order_id not in orders:
                raise ValueError("related record references an unknown order")
        for policy in self.policies:
            if not set(policy.scope_product_ids).issubset(products):
                raise ValueError("policy references an unknown product")
        message_ids = [message.id for ticket in self.tickets for message in ticket.messages]
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("message IDs must be unique")
        for order in self.orders:
            refunded = sum(
                record.amount_cents or 0
                for record in self.after_sales_history
                if record.order_id == order.id
                and record.type == ActionType.MOCK_REFUND
                and record.status == "completed"
            )
            if refunded != order.refunded_cents:
                raise ValueError("order refund totals must match completed refund history")
        return self


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must have a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
