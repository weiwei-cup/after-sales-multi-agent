"""Shared tool input, output and immutable evidence contracts."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from after_sales.domain.models import (
    ActionType,
    DomainModel,
    Identifier,
    PositiveInt,
    TicketType,
    UtcTime,
)


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    MISSING_REFERENCE = "MISSING_REFERENCE"
    ORDER_SCOPE_MISMATCH = "ORDER_SCOPE_MISMATCH"
    NOT_OWNED = "NOT_OWNED"
    NOT_FOUND = "NOT_FOUND"
    QUERY_FAILED = "QUERY_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_BUSY = "TOOL_BUSY"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    EVIDENCE_SCOPE_MISMATCH = "EVIDENCE_SCOPE_MISMATCH"
    EVIDENCE_VERSION_MISMATCH = "EVIDENCE_VERSION_MISMATCH"
    EVIDENCE_SOURCE_MISMATCH = "EVIDENCE_SOURCE_MISMATCH"


class ToolFailure(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


class ToolError(DomainModel):
    code: ErrorCode
    message: str
    retryable: bool = False


class EvidenceRef(DomainModel):
    evidence_id: Identifier
    source_version: Identifier


class ToolResult(DomainModel):
    schema_version: Literal["tool-result-v1"] = "tool-result-v1"
    ok: bool
    data: JsonValue = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.ok == (self.error is not None):
            raise ValueError("ok results cannot carry errors; failed results must carry an error")
        if not self.ok and (self.data is not None or self.evidence_refs):
            raise ValueError("failures cannot introduce business facts or evidence")
        return self


def failed(code: ErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(code=code, message=message, retryable=retryable))


class ToolContext(DomainModel):
    """Created by application code after ticket lookup, never supplied in model arguments."""

    ticket_id: Identifier
    ticket_version: PositiveInt
    customer_id: Identifier
    supplied_order_id: Identifier | None
    intent: TicketType
    session_id: Identifier
    as_of_time: UtcTime
    dataset_version: Identifier


class Evidence(DomainModel):
    id: Identifier
    ticket_id: Identifier
    session_id: Identifier
    source_type: Literal[
        "order", "products", "tracking", "proof", "history", "policy", "assessment"
    ]
    source_id: Identifier
    source_version: Identifier
    observed_at: UtcTime
    as_of_time: UtcTime
    # A JSON string stays immutable even when callers mutate decoded result dictionaries.
    facts_json: str

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(evidence_id=self.id, source_version=self.source_version)


class OrderQuery(DomainModel):
    order_id: Identifier = Field(description="用户提供的订单号；客户范围由应用注入")


class PolicySearch(DomainModel):
    intent: TicketType = Field(description="政策标签；必须与当前工单类型一致")


class PolicyQuery(DomainModel):
    policy_id: Identifier
    version: PositiveInt = Field(description="明确版本号，禁止隐式使用最新版")


class AssessmentQuery(DomainModel):
    order_ref: EvidenceRef
    policy_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=20)
    products_ref: EvidenceRef | None = None
    tracking_ref: EvidenceRef | None = None
    history_ref: EvidenceRef | None = None
    action: ActionType
    requested_amount_cents: Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)] | None = (
        None
    )

    @model_validator(mode="after")
    def validate_amount_scope(self) -> Self:
        if self.action != ActionType.MOCK_REFUND and self.requested_amount_cents is not None:
            raise ValueError("only refund assessments accept a requested amount")
        return self


class ReferenceQuery(DomainModel):
    refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=50)


class EvidenceQuery(DomainModel):
    ref: EvidenceRef
