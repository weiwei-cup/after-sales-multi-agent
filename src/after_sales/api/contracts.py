"""Public DTOs deliberately omit graph state, prompts, evidence payloads and credentials."""

from typing import Literal

from pydantic import Field

from after_sales.domain.models import (
    Cents,
    DomainModel,
    Identifier,
    PositiveInt,
    RunStatus,
    TicketStatus,
    TicketType,
)


class Principal(DomainModel):
    role: Literal["customer", "operator"]
    actor_id: Identifier


class CreateTicket(DomainModel):
    type: TicketType
    supplied_order_id: Identifier | None = None
    message: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class StartRun(DomainModel):
    workflow: Literal["parallel", "single"] = "parallel"
    expected_input_revision: PositiveInt | None = None


class EmptyRequest(DomainModel):
    pass


class PendingResponse(DomainModel):
    pending_id: Identifier
    input_revision: PositiveInt
    proposal_revision: PositiveInt
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_hashes: dict[Identifier, str] = Field(default_factory=dict, max_length=3)
    answers: dict[str, str] = Field(default_factory=dict, max_length=10)
    decision: Literal["approve", "reject", "revise"] | None = None
    refund_amounts: dict[Identifier, Cents] = Field(default_factory=dict, max_length=3)


class PendingAction(DomainModel):
    action_id: Identifier
    content_hash: str
    type: str
    order_id: Identifier
    amount_cents: Cents | None
    policies: list["PolicyView"] = Field(default_factory=list)


class PolicyView(DomainModel):
    policy_id: str
    version: int


class EvidenceView(DomainModel):
    evidence_id: str
    source_type: str
    source_id: str
    source_version: str
    observed_at: str
    summary: str


class PendingView(DomainModel):
    pending_id: Identifier
    kind: Literal["customer_info", "operator_decision"]
    expected_role: Literal["customer", "operator"]
    input_revision: PositiveInt
    proposal_revision: PositiveInt
    proposal_hash: str
    questions: list[dict[str, str]]
    actions: list[PendingAction]
    can_respond: bool


class ReceiptView(DomainModel):
    action_id: Identifier
    type: str
    order_id: Identifier
    amount_cents: Cents | None
    business_record_id: Identifier
    business_status: TicketStatus
    committed_at: str


class ResultView(DomainModel):
    outcome: str
    decision: str | None
    customer_reply: str | None
    receipts: list[ReceiptView]
    model_calls: int
    tool_calls: int
    error_code: str | None
    input_revision: int | None = None
    proposal_revision: int | None = None
    evidence: list[EvidenceView] = Field(default_factory=list)
    gaps: list[dict[str, str]] = Field(default_factory=list)
    review_outcome: str | None = None
    review_issues: list[str] = Field(default_factory=list)
    elapsed_ms: float | None = None
    schema_repairs: int | None = None
    review_reworks: int | None = None


class RunView(DomainModel):
    run_id: Identifier
    ticket_id: Identifier
    status: RunStatus
    cancel_requested: bool
    pending_input: PendingView | None
    result: ResultView | None
    execution_error_code: str | None


class TicketView(DomainModel):
    ticket_id: Identifier
    type: TicketType
    status: TicketStatus
    supplied_order_id: Identifier | None
    input_revision: PositiveInt
    messages: list[str]
    latest_run: RunView | None
    last_activity_at: str | None = None


class Accepted(DomainModel):
    run_id: Identifier
    job_id: Identifier | None
    accepted: Literal[True] = True


class EventView(DomainModel):
    sequence: int
    kind: str
    role: str | None = None
    node: str | None = None
    tool: str | None = None
    elapsed_ms: float | None = None


class EventPage(DomainModel):
    events: list[EventView]
    next_after_seq: int
    has_more: bool


class ErrorView(DomainModel):
    code: str
    message: str
    request_id: str
