"""P05: bounded feedback and real interrupt/resume, owned by one process."""

import asyncio
import copy
import json
from importlib.metadata import version
from typing import TypedDict
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import JsonValue, TypeAdapter

from after_sales.agents.contracts import (
    REPORT_ADAPTER,
    ActionBinding,
    Confirmation,
    Decision,
    PendingInput,
    ProposalValidation,
    ResearchTask,
    ResolutionProposal,
    ResumeInput,
    ReviewResult,
)
from after_sales.agents.review import (
    REVIEW_TOOLS,
    action_id,
    create_review_model,
    digest,
    merge_research,
    safe_draft,
)
from after_sales.agents.telemetry import RunTelemetry
from after_sales.config import Settings
from after_sales.domain.models import ActionType, Identifier, Ticket, TicketMessage
from after_sales.domain.rules import RULES_VERSION
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.tools.service import ToolSession
from after_sales.workflows.contracts import ORDER_TOOLS, POLICY_TOOLS, OrderInvestigation, Role
from after_sales.workflows.serial import HandoffRejected, ModelFactory, MultiRuntime


class ReviewState(TypedDict):
    schema_version: str
    run_id: str
    ticket_id: str
    input: dict[str, JsonValue]
    input_revision: int
    proposal_revision: int
    intake: dict | None
    order_findings: dict | None
    policy_assessment: dict | None
    proposal: dict | None
    validation: dict | None
    review: dict | None
    pending_input: dict | None
    answered_fields: list[str]
    refund_amounts: dict[str, int]
    repair_count: int
    route: str
    status: str
    node_trace: list[str]
    reason: str | None


class ResumeRejected(ValueError):
    """Rejected before consuming a pending input or changing its checkpoint."""


class TicketOverlayRepository(BusinessRepository):
    """One in-memory input revision; business reads still use the original scoped repository."""

    def __init__(self, source: BusinessRepository, ticket: Ticket):
        super().__init__(source.path)
        self.source, self.ticket = source, ticket

    def get_ticket(self, ticket_id):
        return self.ticket if ticket_id == self.ticket.id else self.source.get_ticket(ticket_id)

    def get_order(self, order_id, *, customer_id):
        return self.source.get_order(order_id, customer_id=customer_id)

    def get_order_products(self, order_id, *, customer_id):
        return self.source.get_order_products(order_id, customer_id=customer_id)

    def get_tracking(self, order_id, *, customer_id):
        return self.source.get_tracking(order_id, customer_id=customer_id)

    def get_delivery_proof(self, order_id, *, customer_id):
        return self.source.get_delivery_proof(order_id, customer_id=customer_id)

    def get_history(self, order_id, *, customer_id):
        return self.source.get_history(order_id, customer_id=customer_id)

    def list_policies(self, **kwargs):
        return self.source.list_policies(**kwargs)


def draft_input(state):
    return {
        name: copy.deepcopy(state[name])
        for name in ("intake", "order_findings", "policy_assessment", "run_id", "refund_amounts")
    }


def feedback(outcome, code, message, **kwargs):
    return ReviewResult(outcome=outcome, issues=({"code": code, "message": message},), **kwargs)


def minimum_review(state) -> ReviewResult:
    """A deterministic lower bound. Model acceptance never weakens these requirements."""
    validation = ProposalValidation.model_validate(state["validation"])
    proposal = ResolutionProposal.model_validate(state["proposal"])
    expected = ResolutionProposal.model_validate(safe_draft(draft_input(state)))
    if not validation.ok:
        return feedback(
            "revise",
            "CODE_VALIDATION_FAILED",
            "方案未通过确定性校验。",
            revision_instructions="根据代码校验问题重新生成有完整可信引用的合法方案。",
        )
    if expected.decision == Decision.HUMAN_REVIEW:
        return feedback("handoff", "MANUAL_REVIEW_REQUIRED", expected.customer_reply_draft)
    tasks = []
    order = state["order_findings"]
    if order:
        retry = [error["tool"] for error in order["tool_errors"] if error["error"]["retryable"]]
        if retry:
            tools = (
                ORDER_TOOLS
                if order["outcome"] != "ready"
                else tuple(t for t in ORDER_TOOLS if t in retry)
            )
            tasks.append(
                ResearchTask(
                    owner=Role.ORDER.value,
                    tools=tools,
                    question="仅重试失败读取，保留其余已核验结果。",
                )
            )
    policy = state["policy_assessment"]
    if policy and any(error["error"]["retryable"] for error in policy["tool_errors"]):
        tasks.append(
            ResearchTask(
                owner=Role.POLICY.value,
                tools=POLICY_TOOLS,
                question="重新检索并计算失败的政策结果。",
            )
        )
    if tasks:
        return feedback(
            "research",
            "QUERY_RETRY_REQUIRED",
            "查询失败不能作为资料不存在的证据。",
            research_tasks=tuple(tasks),
        )
    if state["refund_amounts"] and proposal.actions != expected.actions:
        return feedback(
            "revise",
            "OPERATOR_EDIT_NOT_APPLIED",
            "新方案没有准确应用操作员指定的金额。",
            revision_instructions="应用当前指定金额后重新校验与展示，不沿用旧金额。",
        )
    if (
        proposal.decision != expected.decision
        or proposal.customer_reply_draft != expected.customer_reply_draft
        or proposal.unresolved_questions != expected.unresolved_questions
    ):
        return feedback(
            "revise",
            "UNSUPPORTED_REPLY",
            "措辞或结论不符合可信结果及受控回复模板。",
            revision_instructions="采用当前证据对应的中文模板，不作未经核验的承诺。",
        )
    if expected.decision == Decision.REQUEST_INFORMATION:
        fields = {q.field for q in expected.unresolved_questions}
        if fields & set(state["answered_fields"]):
            return feedback(
                "handoff",
                "ANSWER_STILL_UNVERIFIED",
                "客户已回答，但可信数据仍不足；转人工核验，避免重复追问。",
            )
        if not fields.issubset(
            {"order_id", "received_at", "product_state", "logistics_evidence", "delivery_proof"}
        ):
            return feedback(
                "handoff",
                "INTERNAL_EVIDENCE_MISSING",
                "缺少内部资料，需要人工核验；不要求客户修复内部服务。",
            )
        return feedback("customer_info", "NECESSARY_FACT_MISSING", "缺少继续处理必需的资料。")
    return ReviewResult(outcome="accept")


class ReviewRuntime(MultiRuntime):
    def __init__(self, owner, session, trace, factory):
        super().__init__(session, trace, factory)
        self.owner = owner

    async def draft(self, state, config):
        payload = draft_input(state)
        payload["review_feedback"] = copy.deepcopy(state["review"])
        payload["validation_feedback"] = copy.deepcopy(state["validation"])
        result, _ = await self.invoke_role(
            Role.COORDINATOR, "draft", payload, ResolutionProposal, (), config
        )
        return {
            "proposal": result.model_dump(mode="json"),
            "proposal_revision": state["proposal_revision"] + 1,
            "pending_input": None,
            "validation": None,
            "review": None,
            "status": "running",
            "route": "validate",
        }

    async def validate(self, state, config):
        result = await super().validate(state, config)
        return {**result, "status": "running", "route": "review"}

    async def review(self, state, config):
        minimum = minimum_review(state)
        payload = {
            "proposal": copy.deepcopy(state["proposal"]),
            "validation": copy.deepcopy(state["validation"]),
            "evidence": self.session.evidence.export(self.session.context),
            "minimum_review": minimum.model_dump(mode="json"),
        }
        result, actual = await self.invoke_role(
            Role.REVIEW, "review", payload, ReviewResult, REVIEW_TOOLS, config
        )
        if any(not item.ok for item in actual.values()):
            result = feedback("handoff", "REVIEW_EVIDENCE_INVALID", "审核引用核验失败。")
        # A reviewer may be stricter, but cannot invent missing customer fields or bypass a guard.
        if minimum.outcome != "accept":
            result = minimum
        elif result.outcome == "customer_info":
            result = feedback(
                "handoff", "UNNECESSARY_QUESTION", "当前方案不缺必要资料，不追加客户追问。"
            )
        route = {
            "research": "repair",
            "revise": "repair",
            "customer_info": "prepare_customer",
            "handoff": "handoff",
            "accept": "prepare_operator" if state["proposal"]["actions"] else "finish",
        }[result.outcome]
        return {"review": result.model_dump(mode="json"), "route": route}

    async def repair(self, state, config):
        if state["repair_count"] >= self.trace.settings.review_repair_limit:
            return {"route": "handoff", "reason": "REPAIR_LIMIT_REACHED"}
        count = state["repair_count"] + 1
        self.trace.event(
            "review_repair", role="application", attempt=count, feedback=state["review"]
        )
        return {
            "repair_count": count,
            "route": "research" if state["review"]["outcome"] == "research" else "draft",
        }

    async def research(self, state, config):
        tasks = [ResearchTask.model_validate(t) for t in state["review"]["research_tasks"]]
        updated = copy.deepcopy(state)
        order_task = next((t for t in tasks if t.owner == Role.ORDER.value), None)
        policy_needed = any(t.owner == Role.POLICY.value for t in tasks)
        if order_task:
            order_id = state["intake"]["slots"]["order_id"]
            payload = {
                "ticket_id": state["ticket_id"],
                "order_id": order_id,
                "order_findings": copy.deepcopy(state["order_findings"]),
                "tools": list(order_task.tools),
                "question": order_task.question,
            }
            result, actual = await self.invoke_role(
                Role.ORDER, "research", payload, OrderInvestigation, order_task.tools, config
            )
            expected_tools = set(order_task.tools)
            if "get_order" in actual and not actual["get_order"].ok:
                expected_tools = {"get_order"}
            if set(actual) != expected_tools or result != merge_research(payload, actual):
                raise HandoffRejected(
                    "targeted research must query only assigned tools and preserve other results"
                )
            self.check_snapshots(result.facts)
            updated["order_findings"] = result.model_dump(mode="json")
            # Proof does not enter evaluate_policy; other changed inputs invalidate old assessments.
            policy_needed |= bool(set(order_task.tools) - {"get_delivery_proof"})
        if updated["order_findings"]["outcome"] != "ready":
            updated["policy_assessment"] = None
        elif policy_needed:
            self.trace.event(
                "dependent_policy_recheck",
                role="application",
                reason="rule inputs refreshed or policy task assigned",
            )
            updated.update(await super().policy(updated, config))
        return {
            "order_findings": updated["order_findings"],
            "policy_assessment": updated["policy_assessment"],
            "route": "draft",
        }

    async def prepare_customer(self, state, config):
        return self.owner.prepare_pending(state, "customer_info")

    async def prepare_operator(self, state, config):
        return self.owner.prepare_pending(state, "operator_decision")

    async def wait_customer(self, state, config):
        raw = interrupt(copy.deepcopy(state["pending_input"]))
        request = self.owner.validate_resume(raw, state)
        self.owner.consume(request)
        new_input = self.owner.apply_customer(request)
        if "order_id" not in request.answers:
            return {
                "input": new_input,
                "input_revision": self.owner.ticket.input_revision,
                "answered_fields": [*state["answered_fields"], *request.answers],
                "pending_input": None,
                "status": "running",
                "route": "refresh_input",
            }
        return {
            "input": new_input,
            "input_revision": self.owner.ticket.input_revision,
            "answered_fields": [*state["answered_fields"], *request.answers],
            "pending_input": None,
            "intake": None,
            "order_findings": None,
            "policy_assessment": None,
            "proposal": None,
            "validation": None,
            "review": None,
            "refund_amounts": {},
            "status": "running",
            "route": "intake",
        }

    async def refresh_input(self, state, config):
        # Customer statements update intake, never the verified findings or computed policy facts.
        result = await super().intake(state, config)
        return {**result, "route": "draft"}

    async def wait_operator(self, state, config):
        raw = interrupt(copy.deepcopy(state["pending_input"]))
        request = self.owner.validate_resume(raw, state)
        self.owner.consume(request)
        self.owner.record_confirmation(
            Confirmation(
                pending_id=request.pending_id,
                actor_id=request.actor_id,
                decision=request.decision,
                input_revision=request.input_revision,
                proposal_revision=request.proposal_revision,
                proposal_hash=request.proposal_hash,
                action_hashes=request.action_hashes,
            )
        )
        if request.decision == "revise":
            review = feedback(
                "revise",
                "OPERATOR_AMOUNT_EDIT",
                "操作员修改退款金额，旧确认失效，重新审核并展示。",
                revision_instructions="应用已校验的整数分金额，生成新方案版本并重新确认。",
            )
            return {
                "pending_input": None,
                "refund_amounts": {**state["refund_amounts"], **request.refund_amounts},
                "review": review.model_dump(mode="json"),
                "route": "repair",
                "status": "running",
            }
        return {
            "pending_input": None,
            "status": "running",
            "route": "approve" if request.decision == "approve" else "handoff",
            "reason": None if request.decision == "approve" else "OPERATOR_REJECTED",
        }

    async def approve(self, state, config):
        checked = await self.validate(state, config)
        current = {**state, **checked}
        if not checked["validation"]["ok"] or minimum_review(current).outcome != "accept":
            return {**checked, "route": "handoff", "reason": "APPROVAL_RECHECK_FAILED"}
        return {**checked, "status": "completed", "route": "end"}

    async def finish(self, state, config):
        return {"status": "completed", "route": "end"}

    async def handoff(self, state, config):
        return {
            "pending_input": None,
            "status": "handed_off",
            "route": "end",
            "reason": state["reason"] or state["review"]["issues"][0]["code"],
        }


def build_review_graph(runtime, checkpointer, *, state_schema=ReviewState, persistent=False):
    graph = StateGraph(state_schema)
    names = (
        "intake",
        "order",
        "policy",
        "draft",
        "validate",
        "review",
        "repair",
        "research",
        "prepare_customer",
        "wait_customer",
        "refresh_input",
        "prepare_operator",
        "wait_operator",
        "approve",
        "finish",
        "handoff",
    ) + (("refresh",) if persistent else ())
    for name in names:
        operation = getattr(runtime, name)

        async def node(state, config: RunnableConfig, name=name, operation=operation):
            runtime.trace.event("node_started", role="application", node=name)
            try:
                update = await operation(state, config)
                update = json.loads(json.dumps(update, ensure_ascii=False))
                update["node_trace"] = [*state["node_trace"], name]
                if persistent:
                    runtime.owner.after_node({**state, **update}, name)
                runtime.trace.event("node_finished", role="application", node=name)
                return update
            except GraphInterrupt:
                runtime.trace.event("node_interrupted", role="application", node=name)
                raise
            except Exception as error:
                runtime.trace.event(
                    "node_failed", role="application", node=name, error_code=type(error).__name__
                )
                raise

        graph.add_node(name, node)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake", lambda s: s["route"], {"order": "order", "draft": "draft"}
    )
    graph.add_conditional_edges(
        "order", lambda s: s["route"], {"policy": "policy", "draft": "draft"}
    )
    graph.add_edge("policy", "draft")
    graph.add_edge("draft", "validate")
    graph.add_edge("validate", "review")
    for name, routes in {
        "review": ("repair", "prepare_customer", "prepare_operator", "handoff", "finish"),
        "repair": ("research", "draft", "handoff"),
        "approve": ("handoff", "end", "refresh") if persistent else ("handoff", "end"),
    }.items():
        graph.add_conditional_edges(
            name,
            lambda s: s["route"],
            {route: END if route == "end" else route for route in routes},
        )
    graph.add_edge("research", "draft")
    if persistent:
        graph.add_edge("refresh", "draft")
    graph.add_edge("prepare_customer", "wait_customer")
    graph.add_edge("prepare_operator", "wait_operator")
    graph.add_conditional_edges(
        "wait_customer",
        lambda s: s["route"],
        {"intake": "intake", "refresh_input": "refresh_input"},
    )
    graph.add_edge("refresh_input", "draft")
    graph.add_conditional_edges(
        "wait_operator",
        lambda s: s["route"],
        {"repair": "repair", "approve": "approve", "handoff": "handoff"},
    )
    for name in ("finish", "handoff"):
        graph.add_edge(name, END)
    return graph.compile(checkpointer=checkpointer)


class InMemoryReviewRun:
    """Own the graph, evidence, budgets and pending registry until close; no restart resume."""

    def __init__(
        self,
        repository: BusinessRepository,
        ticket_id: str,
        settings: Settings,
        *,
        mode="scripted",
        model_factory: ModelFactory = create_review_model,
        run_id: str | None = None,
        operator_id="OP-DEMO",
    ):
        self.repository, self.settings, self.mode = repository, settings, mode
        self.run_id = TypeAdapter(Identifier).validate_python(run_id or f"run-{uuid4().hex}")
        self.operator_id = TypeAdapter(Identifier).validate_python(operator_id)
        self.ticket = repository.get_ticket(ticket_id)
        self.trace = RunTelemetry(settings)
        self.runtime = ReviewRuntime(self, self.new_session(), self.trace, model_factory)
        self.checkpointer = InMemorySaver()
        self.graph = build_review_graph(self.runtime, self.checkpointer)
        self.config = {"configurable": {"thread_id": self.run_id}, "recursion_limit": 100}
        self.pending_history: dict[str, PendingInput] = {}
        self.consumed: set[str] = set()
        self.confirmations: list[Confirmation] = []
        self.lock = asyncio.Lock()
        self.started = self.closed = False
        self.error = None
        self.state: ReviewState = {
            "schema_version": "review-state-v1",
            "run_id": self.run_id,
            "ticket_id": ticket_id,
            "input": self.ticket_input(),
            "input_revision": self.ticket.input_revision,
            "proposal_revision": 0,
            "intake": None,
            "order_findings": None,
            "policy_assessment": None,
            "proposal": None,
            "validation": None,
            "review": None,
            "pending_input": None,
            "answered_fields": [],
            "refund_amounts": {},
            "repair_count": 0,
            "route": "intake",
            "status": "running",
            "node_trace": [],
            "reason": None,
        }

    def ticket_input(self):
        return {
            "ticket_id": self.ticket.id,
            "intent": self.ticket.type.value,
            "supplied_order_id": self.ticket.supplied_order_id,
            "ticket_messages": [
                {"role": m.role, "content": m.content} for m in self.ticket.messages
            ],
        }

    def new_session(self, evidence_store=None):
        repository = TicketOverlayRepository(self.repository, self.ticket)
        return ToolSession.for_ticket(
            repository,
            self.ticket.id,
            session_id=self.run_id,
            evidence_store=evidence_store,
            timeout_seconds=self.settings.tool_timeout_seconds,
            max_result_bytes=self.settings.tool_max_result_bytes,
        )

    def prepare_pending(self, state, kind):
        proposal = ResolutionProposal.model_validate(state["proposal"])
        actions = (
            tuple(
                ActionBinding(
                    action_id=action_id(self.run_id, a.model_dump(mode="json")),
                    content_hash=digest(a.model_dump(mode="json")),
                    candidate=a,
                )
                for a in proposal.actions
            )
            if kind == "operator_decision"
            else ()
        )
        payload = {
            "run_id": self.run_id,
            "ticket_id": self.ticket.id,
            "kind": kind,
            "expected_role": "customer" if kind == "customer_info" else "operator",
            "expected_actor": self.ticket.customer_id
            if kind == "customer_info"
            else self.operator_id,
            "input_revision": state["input_revision"],
            "proposal_revision": state["proposal_revision"],
            "proposal_hash": digest(proposal.model_dump(mode="json")),
            "questions": [q.model_dump(mode="json") for q in proposal.unresolved_questions]
            if kind == "customer_info"
            else [],
            "actions": [a.model_dump(mode="json") for a in actions],
        }
        pending = PendingInput(pending_id="pending-" + digest(payload)[:32], **payload)
        if pending.pending_id not in self.pending_history:
            self.pending_history[pending.pending_id] = pending
            self.trace.event(
                "pending_created", role="application", pending=pending.model_dump(mode="json")
            )
        return {
            "pending_input": pending.model_dump(mode="json"),
            "status": "paused",
            "route": "wait_customer" if kind == "customer_info" else "wait_operator",
        }

    def validate_resume(self, raw, state=None):
        state = state or self.state
        if self.closed or state["status"] != "paused" or state["pending_input"] is None:
            raise ResumeRejected("当前没有可消费的待办；恢复仅限仍持有该运行的进程。")
        request = ResumeInput.model_validate(
            json.loads(
                json.dumps(raw.model_dump(mode="json") if isinstance(raw, ResumeInput) else raw)
            )
        )
        pending = PendingInput.model_validate(state["pending_input"])
        actual = (
            request.pending_id,
            request.run_id,
            request.ticket_id,
            request.role,
            request.actor_id,
            request.input_revision,
            request.proposal_revision,
            request.proposal_hash,
            request.action_hashes,
        )
        expected = (
            pending.pending_id,
            pending.run_id,
            pending.ticket_id,
            pending.expected_role,
            pending.expected_actor,
            pending.input_revision,
            pending.proposal_revision,
            pending.proposal_hash,
            {a.action_id: a.content_hash for a in pending.actions},
        )
        if actual != expected or request.pending_id in self.consumed:
            raise ResumeRejected("角色、身份、待办 ID、版本或动作内容不匹配，或该待办已消费。")
        if pending.kind == "customer_info":
            if set(request.answers) != {q.field for q in pending.questions}:
                raise ResumeRejected("必须且只能回答本次待办中列出的字段。")
            if "order_id" in request.answers:
                TypeAdapter(Identifier).validate_python(request.answers["order_id"])
        elif request.decision == "revise":
            if state["repair_count"] >= self.settings.review_repair_limit:
                raise ResumeRejected("共享返工次数已用完；可拒绝并交人工处理。")
            actions = {a.action_id: a.candidate for a in pending.actions}
            for identifier, amount in request.refund_amounts.items():
                action = actions.get(identifier)
                if action is None or action.type != ActionType.MOCK_REFUND:
                    raise ResumeRejected("只支持修改当前退款候选的金额。")
                order = next(
                    f["facts"]
                    for f in state["order_findings"]["facts"]
                    if f["source_type"] == "order"
                )
                if (
                    amount <= 0
                    or amount > order["paid_cents"] - order["refunded_cents"]
                    or amount == action.amount_cents
                ):
                    raise ResumeRejected(
                        "新金额必须为正整数分、不同于旧金额且不超过已核验可退余额。"
                    )
        return request

    def consume(self, request):
        self.consumed.add(request.pending_id)
        self.trace.event(
            "pending_consumed",
            role="application",
            pending_id=request.pending_id,
            input_revision=request.input_revision,
            proposal_revision=request.proposal_revision,
        )

    def record_confirmation(self, confirmation):
        self.confirmations.append(confirmation)

    def apply_customer(self, request):
        revision = self.ticket.input_revision + 1
        messages = tuple(
            TicketMessage(
                id=f"answer-{revision}-{index}",
                ticket_id=self.ticket.id,
                role="customer",
                content=f"{field}: {value}",
                created_at=self.runtime.session.context.as_of_time,
            )
            for index, (field, value) in enumerate(request.answers.items())
        )
        values = self.ticket.model_dump(mode="python")
        values.update(input_revision=revision, messages=(*self.ticket.messages, *messages))
        if "order_id" in request.answers:
            values.update(supplied_order_id=request.answers["order_id"], order_id=None)
        self.ticket = Ticket.model_validate(values)
        old = self.runtime.session
        self.runtime.session = self.new_session(old.evidence)
        old.close()
        return self.ticket_input()

    async def advance(self, value):
        try:
            async for _ in self.graph.astream(value, self.config, stream_mode="updates"):
                pass
            snapshot = await self.graph.aget_state(self.config)
            self.state = json.loads(json.dumps(snapshot.values, ensure_ascii=False))
        except Exception as error:
            snapshot = await self.graph.aget_state(self.config)
            if snapshot.values:
                self.state = json.loads(json.dumps(snapshot.values, ensure_ascii=False))
            self.state.update(status="handed_off", pending_input=None, reason=type(error).__name__)
            failed = next(
                (e for e in reversed(self.trace.events) if e["kind"] == "node_failed"), None
            )
            self.error = {
                "code": type(error).__name__,
                "message": "运行在预算、角色交接或模型约束处停止，转人工处理。",
                "node": failed["node"] if failed else None,
            }
        self.trace.event(
            "run_paused" if self.state["status"] == "paused" else "run_finished",
            role="application",
            status=self.state["status"],
        )
        return self.report()

    async def start(self):
        async with self.lock:
            if self.started or self.closed:
                raise ResumeRejected("运行已经启动或关闭。")
            self.started = True
            self.trace.event(
                "run_started",
                role="application",
                architecture="multi",
                workflow=getattr(self, "workflow_version", "review-v1"),
                ticket_id=self.ticket.id,
            )
            if self.mode == "live":
                self.state["status"] = "skipped"
                self.error = {
                    "code": "LIVE_PROVIDER_DEFERRED",
                    "message": "真实模型接入按用户选择延期。",
                }
                return self.report()
            if self.mode != "scripted":
                raise ValueError("unsupported model mode")
            return await self.advance(copy.deepcopy(self.state))

    async def resume(self, raw):
        async with self.lock:
            request = self.validate_resume(raw)
            return await self.advance(Command(resume=request.model_dump(mode="json")))

    def report(self, *, raw=False):
        state, session = self.state, self.runtime.session
        stats = self.trace.statistics()
        stats["roles"] = {
            role.value: {
                "model_calls": sum(
                    r["model_calls"] for r in self.runtime.agent_runs if r["role"] == role.value
                ),
                "tool_calls": sum(
                    r["tool_calls"] for r in self.runtime.agent_runs if r["role"] == role.value
                ),
            }
            for role in Role
        }
        report = {
            "schema_version": "review-run-v1",
            "phase": "P05",
            "run_id": self.run_id,
            "ticket_id": self.ticket.id,
            "architecture": "multi",
            "model_mode": self.mode,
            "workflow_version": "review-v1",
            "script_version": "review-roles-v1" if self.mode == "scripted" else None,
            "rules_version": RULES_VERSION,
            "dataset_version": session.context.dataset_version,
            "as_of_time": session.context.model_dump(mode="json")["as_of_time"],
            "packages": {name: version(name) for name in ("langchain", "langgraph")},
            "input": self.ticket.model_dump(mode="json"),
            "status": state["status"],
            "proposal": state["proposal"],
            "accepted_proposal": state["proposal"] if state["status"] == "completed" else None,
            "validation": state["validation"],
            "review": state["review"],
            "error": self.error,
            "candidate_only": True,
            "executed_actions": [],
            "resume_scope": "same_process_only",
            "checkpointer": "InMemorySaver",
            "pending_input": state["pending_input"],
            "pending_history": [p.model_dump(mode="json") for p in self.pending_history.values()],
            "confirmations": [c.model_dump(mode="json") for c in self.confirmations],
            "input_revision": state["input_revision"],
            "proposal_revision": state["proposal_revision"],
            "repair_count": state["repair_count"],
            "graph_state": state,
            "node_trace": state["node_trace"],
            "agent_runs": self.runtime.agent_runs,
            "graph_mermaid": self.graph.get_graph().draw_mermaid(),
            "events": self.trace.events,
            "statistics": stats,
            "messages": self.trace.messages,
            "evidence": session.evidence.export(session.context),
        }
        # A returned report is a static value, never a handle for restoring trusted state.
        if raw:
            return json.loads(json.dumps(report, ensure_ascii=False))
        return REPORT_ADAPTER.validate_python(
            json.loads(json.dumps(report, ensure_ascii=False))
        ).model_dump(mode="json")

    def close(self):
        self.runtime.session.close()
        self.closed = True


def response_envelope(pending: dict, **payload) -> dict:
    """Copy bindings from a displayed pending; local CLI injects the chosen demo identity."""
    value = PendingInput.model_validate(pending)
    return {
        "pending_id": value.pending_id,
        "run_id": value.run_id,
        "ticket_id": value.ticket_id,
        "role": value.expected_role,
        "actor_id": value.expected_actor,
        "input_revision": value.input_revision,
        "proposal_revision": value.proposal_revision,
        "proposal_hash": value.proposal_hash,
        "action_hashes": {a.action_id: a.content_hash for a in value.actions},
        **payload,
    }


async def run_reviewed(repository, ticket_id, settings, **kwargs):
    run = InMemoryReviewRun(repository, ticket_id, settings, **kwargs)
    try:
        return await run.start()
    finally:
        run.close()
