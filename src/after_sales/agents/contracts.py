"""Shared proposal contract for the single and later multi-Agent implementations."""

import hashlib
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from after_sales.domain.models import ActionType, Cents, DomainModel, Identifier, PositiveInt
from after_sales.tools.contracts import EvidenceRef
from after_sales.tools.evidence import canonical


def _digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


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
    rechecked_decisions: int = Field(default=0, ge=0)
    executable: Literal[False] = False

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.ok != (not self.issues):
            raise ValueError("validation outcome must agree with its issues")
        return self


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
        if (
            self.accepted_proposal is not None
            and self.accepted_proposal.ticket_id != self.ticket_id
        ):
            raise ValueError("run and accepted proposal must refer to the same ticket")
        if self.status == "completed":
            if self.accepted_proposal is None or self.validation is None or not self.validation.ok:
                raise ValueError("completed run requires a code-validated proposal")
            if self.proposal != self.accepted_proposal:
                raise ValueError("accepted proposal must equal the proposal that was validated")
            if self.error is not None:
                raise ValueError("completed run cannot carry an execution error")
        elif self.accepted_proposal is not None:
            raise ValueError("failed or skipped run cannot contain an accepted proposal")
        if self.status == "validation_failed" and (
            self.proposal is None or self.validation is None or self.validation.ok
        ):
            raise ValueError("validation_failed requires a rejected proposal and failed validation")
        return self


class StoredMultiRunReport(StoredRunReport):
    schema_version: Literal["multi-run-v1"]
    phase: Literal["P04"]
    architecture: Literal["multi"]
    graph_state: dict[str, JsonValue]
    node_trace: tuple[Literal["intake", "order", "policy", "draft", "validate"], ...]
    agent_runs: tuple[dict[str, JsonValue], ...]
    graph_mermaid: str


class ResearchTask(DomainModel):
    owner: Literal["order_specialist", "policy_specialist"]
    tools: tuple[str, ...] = Field(min_length=1, max_length=5)
    question: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def allowlist(self) -> Self:
        allowed = {
            "order_specialist": {
                "get_order",
                "get_order_products",
                "get_tracking",
                "get_delivery_proof",
                "get_after_sales_history",
            },
            "policy_specialist": {"search_policies", "get_policy", "evaluate_policy"},
        }[self.owner]
        if len(set(self.tools)) != len(self.tools) or not set(self.tools).issubset(allowed):
            raise ValueError("research tools must be unique and belong to the assigned role")
        if self.owner == "policy_specialist" and set(self.tools) != allowed:
            raise ValueError("policy research must recompute the complete policy assessment")
        return self


class ReviewResult(DomainModel):
    outcome: Literal["accept", "research", "revise", "customer_info", "handoff"]
    issues: tuple[ValidationIssue, ...] = Field(default=(), max_length=20)
    research_tasks: tuple[ResearchTask, ...] = Field(default=(), max_length=2)
    revision_instructions: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def feedback(self) -> Self:
        if (self.outcome == "accept") != (not self.issues):
            raise ValueError("non-accepting review requires an issue; accepting review has none")
        if (self.outcome == "research") != bool(self.research_tasks):
            raise ValueError("only research reviews carry targeted tasks")
        if self.outcome == "revise" and not self.revision_instructions:
            raise ValueError("revision requires explicit instructions")
        if len({task.owner for task in self.research_tasks}) != len(self.research_tasks):
            raise ValueError("at most one task per research role")
        return self


class ActionBinding(DomainModel):
    action_id: Identifier
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate: ActionCandidate

    @model_validator(mode="after")
    def content_matches(self) -> Self:
        if self.content_hash != _digest(self.candidate.model_dump(mode="json")):
            raise ValueError("action binding hash must match its full candidate")
        return self


class PendingInput(DomainModel):
    pending_id: Identifier
    run_id: Identifier
    ticket_id: Identifier
    kind: Literal["customer_info", "operator_decision"]
    expected_role: Literal["customer", "operator"]
    expected_actor: Identifier
    input_revision: PositiveInt
    proposal_revision: PositiveInt
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    questions: tuple[MissingInformation, ...] = Field(default=(), max_length=10)
    actions: tuple[ActionBinding, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def input_kind(self) -> Self:
        if self.kind == "customer_info":
            if self.expected_role != "customer" or not self.questions or self.actions:
                raise ValueError("customer pending requires questions, no actions")
        elif self.expected_role != "operator" or not self.actions or self.questions:
            raise ValueError("operator pending requires action bindings, no questions")
        if len({q.field for q in self.questions}) != len(self.questions):
            raise ValueError("pending questions must be unique")
        if len({a.action_id for a in self.actions}) != len(self.actions):
            raise ValueError("pending action IDs must be unique")
        for binding in self.actions:
            action = binding.candidate
            if (
                binding.action_id
                != "action-" + _digest([self.run_id, action.type.value, action.order_id])[:32]
            ):
                raise ValueError("action ID must match its stable run, type and order identity")
        payload = self.model_dump(mode="json", exclude={"pending_id"})
        if self.pending_id != "pending-" + _digest(payload)[:32]:
            raise ValueError("pending ID must bind its full versioned contents")
        return self


class ResumeInput(DomainModel):
    """Local demo identity envelope; an authenticated transport is deferred to P08."""

    pending_id: Identifier
    run_id: Identifier
    ticket_id: Identifier
    role: Literal["customer", "operator"]
    actor_id: Identifier
    input_revision: PositiveInt
    proposal_revision: PositiveInt
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_hashes: dict[Identifier, str] = Field(default_factory=dict, max_length=3)
    answers: dict[str, str] = Field(default_factory=dict, max_length=10)
    decision: Literal["approve", "reject", "revise"] | None = None
    refund_amounts: dict[Identifier, Cents] = Field(default_factory=dict, max_length=3)

    @model_validator(mode="after")
    def actor_payload(self) -> Self:
        if self.role == "customer":
            if self.decision or self.action_hashes or self.refund_amounts or not self.answers:
                raise ValueError("customer answers cannot authorize or modify actions")
            if any(not value.strip() or len(value) > 1000 for value in self.answers.values()):
                raise ValueError("answers must contain 1–1000 characters")
        elif self.answers or self.decision is None:
            raise ValueError("operator input requires a decision and cannot supply customer facts")
        if (self.decision == "revise") != bool(self.refund_amounts):
            raise ValueError("only a revision carries changed refund amounts")
        return self


class Confirmation(DomainModel):
    pending_id: Identifier
    actor_id: Identifier
    decision: Literal["approve", "reject", "revise"]
    input_revision: PositiveInt
    proposal_revision: PositiveInt
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_hashes: dict[Identifier, str]


class StoredReviewRunReport(StoredRunReport):
    schema_version: Literal["review-run-v1"]
    phase: Literal["P05"]
    architecture: Literal["multi"]
    status: Literal["paused", "completed", "handed_off", "failed", "skipped"]
    resume_scope: Literal["same_process_only"]
    checkpointer: Literal["InMemorySaver"]
    review: ReviewResult | None
    pending_input: PendingInput | None
    pending_history: tuple[PendingInput, ...]
    confirmations: tuple[Confirmation, ...]
    input_revision: PositiveInt
    proposal_revision: int = Field(ge=0, strict=True)
    repair_count: int = Field(ge=0, le=2, strict=True)
    graph_state: dict[str, JsonValue]
    node_trace: tuple[str, ...]
    agent_runs: tuple[dict[str, JsonValue], ...]
    graph_mermaid: str

    @model_validator(mode="after")
    def validate_acceptance(self) -> Self:
        for name in (
            "status",
            "proposal",
            "validation",
            "review",
            "pending_input",
            "input_revision",
            "proposal_revision",
            "repair_count",
            "node_trace",
            "run_id",
            "ticket_id",
        ):
            value = getattr(self, name)
            if isinstance(value, DomainModel):
                value = value.model_dump(mode="json")
            elif isinstance(value, tuple):
                value = list(value)
            if self.graph_state.get(name) != value:
                raise ValueError("report and graph state must agree on current values")
        if self.input.get("input_revision") != self.input_revision:
            raise ValueError("report input must match the current input revision")
        history = {p.pending_id: p for p in self.pending_history}
        if len(history) != len(self.pending_history) or any(
            p.run_id != self.run_id or p.ticket_id != self.ticket_id for p in self.pending_history
        ):
            raise ValueError("pending history must be unique and scoped to this run")
        seen = set()
        for confirmation in self.confirmations:
            pending = history.get(confirmation.pending_id)
            if (
                pending is None
                or pending.kind != "operator_decision"
                or confirmation.pending_id in seen
            ):
                raise ValueError(
                    "each operator confirmation must consume a distinct operator pending"
                )
            seen.add(confirmation.pending_id)
            if (
                confirmation.actor_id != pending.expected_actor
                or confirmation.input_revision != pending.input_revision
                or confirmation.proposal_revision != pending.proposal_revision
                or confirmation.proposal_hash != pending.proposal_hash
                or confirmation.action_hashes
                != {a.action_id: a.content_hash for a in pending.actions}
            ):
                raise ValueError(
                    "confirmation must bind its pending identity, revisions and contents"
                )
        if self.status == "completed":
            if (
                self.proposal is None
                or self.proposal != self.accepted_proposal
                or self.proposal.ticket_id != self.ticket_id
                or self.validation is None
                or not self.validation.ok
                or self.review is None
                or self.review.outcome != "accept"
                or self.error is not None
                or self.pending_input is not None
            ):
                raise ValueError("completed review run requires the accepted current safe proposal")
            if self.proposal.actions:
                digest = _digest(self.proposal.model_dump(mode="json"))
                if not self.confirmations or not any(
                    c.decision == "approve"
                    and c.input_revision == self.input_revision
                    and c.proposal_revision == self.proposal_revision
                    and c.proposal_hash == digest
                    and c.action_hashes
                    == {
                        a.action_id: a.content_hash
                        for p in self.pending_history
                        if p.pending_id == c.pending_id
                        for a in p.actions
                    }
                    and c.action_hashes
                    and tuple(a.candidate for a in history[c.pending_id].actions)
                    == self.proposal.actions
                    for c in self.confirmations
                ):
                    raise ValueError(
                        "action proposal requires an approval bound to its current contents"
                    )
        elif self.accepted_proposal is not None:
            raise ValueError("only completed review runs have an accepted proposal")
        if (self.status == "paused") != (self.pending_input is not None):
            raise ValueError("paused run requires its current pending input")
        if self.pending_input is not None and (
            self.pending_input.run_id != self.run_id
            or self.pending_input.ticket_id != self.ticket_id
            or self.pending_input.input_revision != self.input_revision
            or self.pending_input.proposal_revision != self.proposal_revision
            or self.pending_input not in self.pending_history
        ):
            raise ValueError("pending input must belong to the current run and revisions")
        if self.pending_input is not None and (
            self.proposal is None
            or self.pending_input.pending_id in seen
            or self.pending_input.proposal_hash != _digest(self.proposal.model_dump(mode="json"))
            or (
                self.pending_input.kind == "operator_decision"
                and (
                    self.validation is None
                    or not self.validation.ok
                    or self.review is None
                    or self.review.outcome != "accept"
                    or tuple(a.candidate for a in self.pending_input.actions)
                    != self.proposal.actions
                )
            )
            or (
                self.pending_input.kind == "customer_info"
                and (
                    self.review is None
                    or self.review.outcome != "customer_info"
                    or self.pending_input.questions != self.proposal.unresolved_questions
                )
            )
        ):
            raise ValueError(
                "pending must display the current proposal and unconsumed safe actions"
            )
        return self


RunReport = Annotated[
    StoredRunReport | StoredMultiRunReport | StoredReviewRunReport,
    Field(discriminator="schema_version"),
]
REPORT_ADAPTER = TypeAdapter(RunReport)
