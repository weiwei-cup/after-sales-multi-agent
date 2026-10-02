"""P04 role handoffs and finite routes, validated at every main-graph boundary."""

from enum import StrEnum
from typing import Literal, Self, TypedDict

from pydantic import Field, JsonValue, model_validator

from after_sales.agents.contracts import MissingInformation
from after_sales.domain.models import DomainModel, Identifier, TicketType
from after_sales.tools.contracts import EvidenceRef, ToolError

ORDER_TOOLS = (
    "get_order",
    "get_order_products",
    "get_tracking",
    "get_delivery_proof",
    "get_after_sales_history",
)
POLICY_TOOLS = ("search_policies", "get_policy", "evaluate_policy")
SOURCE_TO_TOOL = dict(
    zip(("order", "products", "tracking", "proof", "history"), ORDER_TOOLS, strict=True)
)


class Role(StrEnum):
    COORDINATOR = "coordinator"
    ORDER = "order_specialist"
    POLICY = "policy_specialist"


class Route(StrEnum):
    ORDER = "order"
    POLICY = "policy"
    DRAFT = "draft"
    VALIDATE = "validate"
    FINISH = "finish"


class IntakeSlots(DomainModel):
    order_id: Identifier | None
    # These are customer statements, never verified order facts.
    customer_claims: tuple[str, ...] = Field(max_length=50)


class InvestigationTask(DomainModel):
    task_id: Literal["order-facts", "policy-rules"]
    owner: Literal[Role.ORDER, Role.POLICY]
    depends_on: tuple[Literal["order-facts"], ...]
    tools: tuple[str, ...]
    question: str = Field(min_length=1, max_length=500)


class InvestigationPlan(DomainModel):
    tasks: tuple[InvestigationTask, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def finite_plan(self) -> Self:
        if not self.tasks:
            return self
        if tuple(t.task_id for t in self.tasks) != ("order-facts", "policy-rules"):
            raise ValueError("P04 plan must use the fixed serial task sequence")
        first, second = self.tasks
        if (first.owner, first.depends_on, first.tools) != (Role.ORDER, (), ORDER_TOOLS):
            raise ValueError("order task requires the order-only tool allowlist")
        if (second.owner, second.depends_on, second.tools) != (
            Role.POLICY,
            ("order-facts",),
            POLICY_TOOLS,
        ):
            raise ValueError("policy task requires order facts and the policy-only allowlist")
        return self


class IntakeResult(DomainModel):
    ticket_id: Identifier
    intent: TicketType
    slots: IntakeSlots
    missing_information: tuple[MissingInformation, ...] = Field(max_length=10)
    plan: InvestigationPlan

    @model_validator(mode="after")
    def route_constraints(self) -> Self:
        blocked = self.intent == TicketType.UNKNOWN or self.slots.order_id is None
        if bool(self.plan.tasks) == blocked:
            raise ValueError("only supported intents with an order reference may investigate")
        if self.slots.order_id is None and not any(
            q.field == "order_id" for q in self.missing_information
        ):
            raise ValueError("missing order reference requires a question")
        return self


class EvidenceSnapshot(DomainModel):
    source_type: Literal[
        "order", "products", "tracking", "proof", "history", "policy", "assessment"
    ]
    source_id: Identifier
    ref: EvidenceRef
    facts: JsonValue


class QueryIssue(DomainModel):
    tool: str
    error: ToolError


class OrderInvestigation(DomainModel):
    ticket_id: Identifier
    order_id: Identifier | None
    outcome: Literal["ready", "unavailable"]
    facts: tuple[EvidenceSnapshot, ...] = Field(max_length=5)
    tool_errors: tuple[QueryIssue, ...] = Field(max_length=5)

    @model_validator(mode="after")
    def order_sources(self) -> Self:
        sources = tuple(f.source_type for f in self.facts)
        if len(set(sources)) != len(sources) or any(s not in SOURCE_TO_TOOL for s in sources):
            raise ValueError("order findings require unique order-only evidence sources")
        if self.outcome == "ready":
            if self.order_id is None or "order" not in sources:
                raise ValueError("ready findings require order evidence")
        elif self.order_id is not None or self.facts:
            raise ValueError("unavailable orders cannot introduce order facts")
        if any(e.tool not in ORDER_TOOLS for e in self.tool_errors):
            raise ValueError("order findings cannot carry other roles' tool errors")
        return self


class PolicyAssessment(DomainModel):
    ticket_id: Identifier
    order_id: Identifier
    search_completed: bool
    policy_evidence: tuple[EvidenceSnapshot, ...] = Field(max_length=20)
    assessments: tuple[EvidenceSnapshot, ...] = Field(max_length=3)
    tool_errors: tuple[QueryIssue, ...] = Field(max_length=10)

    @model_validator(mode="after")
    def policy_sources(self) -> Self:
        if any(f.source_type != "policy" for f in self.policy_evidence):
            raise ValueError("policy_evidence requires policy sources")
        if any(f.source_type != "assessment" for f in self.assessments):
            raise ValueError("assessments require computed rule sources")
        refs = [f.ref.evidence_id for f in (*self.policy_evidence, *self.assessments)]
        if len(refs) != len(set(refs)) or any(e.tool not in POLICY_TOOLS for e in self.tool_errors):
            raise ValueError("policy handoff requires unique evidence and policy-only errors")
        if not self.search_completed and (self.policy_evidence or self.assessments):
            raise ValueError("failed policy search cannot introduce policy findings")
        return self


class TicketState(TypedDict):
    schema_version: Literal["multi-state-v1"]
    run_id: str
    ticket_id: str
    input: dict[str, JsonValue]
    intake: dict[str, JsonValue] | None
    order_findings: dict[str, JsonValue] | None
    policy_assessment: dict[str, JsonValue] | None
    proposal: dict[str, JsonValue] | None
    validation: dict[str, JsonValue] | None
    route: Literal["order", "policy", "draft", "validate", "finish"]
    status: Literal["running", "completed", "validation_failed", "failed", "skipped"]
    node_trace: list[Literal["intake", "order", "policy", "draft", "validate"]]


TRANSITIONS = {
    "__start__": ("intake",),
    "intake": ("order", "draft"),
    "order": ("policy", "draft"),
    "policy": ("draft",),
    "draft": ("validate",),
    "validate": ("__end__",),
}
