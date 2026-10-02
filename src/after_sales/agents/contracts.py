"""Shared proposal contract for the single and later multi-Agent implementations."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from after_sales.domain.models import ActionType, Cents, DomainModel, Identifier, PositiveInt
from after_sales.tools.contracts import EvidenceRef


class Decision(StrEnum):
    INFORM = "inform_progress"
    INVESTIGATE = "propose_logistics_investigation"
    RETURN = "propose_return"
    REFUND = "propose_refund"
    REQUEST_INFORMATION = "request_information"
    EXISTING = "existing_application"
    HUMAN_REVIEW = "human_review"
    DECLINE = "decline_request"


class PolicyRef(DomainModel):
    policy_id: Identifier
    version: PositiveInt


class FactClaim(DomainModel):
    fact: Literal[
        "order_status",
        "received_at",
        "expected_delivery_at",
        "refund_remaining_cents",
        "proof_status",
        "confirmed_lost",
    ]
    value: JsonValue
    evidence_ref: EvidenceRef


class MissingInformation(DomainModel):
    field: Literal[
        "order_id",
        "received_at",
        "product_state",
        "logistics_evidence",
        "delivery_proof",
        "after_sales_history",
        "policy_evidence",
        "tool_service",
    ]
    question: str = Field(min_length=1, max_length=500)


class ActionCandidate(DomainModel):
    type: ActionType
    order_id: Identifier
    amount_cents: Cents | None = None
    assessment_ref: EvidenceRef
    policy_refs: tuple[PolicyRef, ...] = Field(min_length=1, max_length=20)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=50)
    requires_operator_confirmation: Literal[True] = True

    @model_validator(mode="after")
    def validate_amount(self) -> Self:
        if self.type == ActionType.MOCK_REFUND:
            if self.amount_cents is None or self.amount_cents == 0:
                raise ValueError("refund candidate requires a positive amount in integer cents")
        elif self.amount_cents is not None:
            raise ValueError("only refund candidates carry an amount")
        return self


class ResolutionProposal(DomainModel):
    """A proposed resolution and reply draft; this schema never represents an executed action."""

    ticket_id: Identifier
    order_id: Identifier | None = None
    decision: Decision
    claims: tuple[FactClaim, ...] = Field(default=(), max_length=50)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=50)
    actions: tuple[ActionCandidate, ...] = Field(default=(), max_length=3)
    unresolved_questions: tuple[MissingInformation, ...] = Field(default=(), max_length=10)
    customer_reply_draft: str = Field(min_length=1, max_length=2000)
    candidate_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        expected = {
            Decision.INVESTIGATE: ActionType.LOGISTICS_CASE,
            Decision.RETURN: ActionType.RETURN_REQUEST,
            Decision.REFUND: ActionType.MOCK_REFUND,
        }.get(self.decision)
        if expected is not None:
            if not self.actions or any(action.type != expected for action in self.actions):
                raise ValueError("action candidates must match the proposed decision")
        elif self.actions:
            raise ValueError("this decision cannot contain action candidates")
        if self.decision == Decision.REQUEST_INFORMATION and not self.unresolved_questions:
            raise ValueError("request_information requires an explicit question")
        if self.actions and self.order_id is None:
            raise ValueError("action candidates require an order reference")
        return self


class ValidationIssue(DomainModel):
    code: str
    message: str


class ProposalValidation(DomainModel):
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()
    rechecked_actions: int = Field(default=0, ge=0)
    executable: Literal[False] = False


class StoredRunReport(DomainModel):
    """Validate the complete persisted report before showing it through the CLI."""

    schema_version: Literal["baseline-run-v1"]
    phase: Literal["P03"]
    run_id: Identifier
    ticket_id: Identifier
    architecture: Literal["single"]
    model_mode: Literal["scripted", "live"]
    workflow_version: str
    script_version: str | None
    rules_version: str
    dataset_version: str
    as_of_time: str
    packages: dict[str, str]
    input: dict[str, JsonValue]
    status: Literal["completed", "validation_failed", "failed", "skipped"]
    proposal: ResolutionProposal | None
    accepted_proposal: ResolutionProposal | None
    validation: ProposalValidation | None
    error: dict[str, JsonValue] | None
    candidate_only: Literal[True]
    executed_actions: tuple[JsonValue, ...] = Field(max_length=0)
    events: tuple[dict[str, JsonValue], ...]
    statistics: dict[str, JsonValue]
    messages: tuple[dict[str, JsonValue], ...]
    evidence: tuple[dict[str, JsonValue], ...]
    artifact_path: str | None = None

    @model_validator(mode="after")
    def validate_acceptance(self) -> Self:
        if self.status == "completed":
            if self.accepted_proposal is None or self.validation is None or not self.validation.ok:
                raise ValueError("completed run requires a code-validated proposal")
        elif self.accepted_proposal is not None:
            raise ValueError("failed or skipped run cannot contain an accepted proposal")
        return self


class StoredMultiRunReport(StoredRunReport):
    schema_version: Literal["multi-run-v1"]
    phase: Literal["P04"]
    architecture: Literal["multi"]
    graph_state: dict[str, JsonValue]
    node_trace: tuple[Literal["intake", "order", "policy", "draft", "validate"], ...]
    agent_runs: tuple[dict[str, JsonValue], ...]
    graph_mermaid: str


RunReport = Annotated[StoredRunReport | StoredMultiRunReport, Field(discriminator="schema_version")]
REPORT_ADAPTER = TypeAdapter(RunReport)
