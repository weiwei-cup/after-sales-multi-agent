// The server owns business decisions, identities, revisions and action receipts.
const $ = (id) => document.getElementById(id);
const types = {
  logistics_delay: "物流延迟",
  delivered_not_received: "签收未收到",
  return_request: "退货申请",
  unknown: "其他诉求",
};
const business = {
  new: "待处理",
  processing: "处理中",
  waiting_customer: "待客户补充",
  waiting_review: "待人工确认",
  waiting_return: "待客户寄回",
  resolved: "已办结",
  handed_off: "已转人工",
};
const runs = {
  queued: "排队中",
  running: "运行中",
  paused: "等待输入",
  completed: "运行结束",
  failed: "运行失败",
  interrupted: "执行中断",
  cancelled: "已取消",
};
const decisions = {
  inform_progress: "说明物流进度",
  propose_logistics_investigation: "建议登记物流调查",
  propose_return: "建议登记退货申请",
  propose_refund: "建议模拟退款",
  request_information: "需要补充资料",
  existing_application: "已有售后申请",
  human_review: "转交人工处理",
  decline_request: "当前条件不支持申请",
};
const actions = {
  open_logistics_case: "登记物流调查",
  create_return_request: "登记退货申请",
  issue_mock_refund: "登记模拟退款",
};
const roles = {
  single_agent: "调查 Agent",
  coordinator: "协调员",
  order_specialist: "订单专员",
  policy_specialist: "政策专员",
  reviewer: "审核员",
  validator: "代码核验",
  application: "流程调度",
  executor: "模拟动作执行器",
};
const nodes = {
  intake: "受理诉求",
  order: "订单调查",
  policy_candidates: "检索候选政策",
  policy: "核算政策",
  draft: "生成建议",
  validate: "代码校验",
  review: "审核建议",
  human: "等待人工输入",
  execute: "执行模拟动作",
  join: "汇总调查",
  dispatch: "分派任务",
  finish: "结束处理",
};
const tools = {
  get_order: "读取订单",
  get_order_products: "读取商品",
  get_tracking: "查询物流",
  get_delivery_proof: "查询签收凭证",
  get_after_sales_history: "查询售后历史",
  search_policies: "检索政策",
  search_policy_candidates: "检索候选政策",
  get_policy: "读取政策",
  evaluate_policy: "复算政策",
};
const kinds = {
  node_started: "开始",
  node_finished: "完成",
  model_started: "调用模型",
  model_finished: "模型返回",
  tool_started: "工具查询",
  tool_finished: "工具返回",
  tool_failed: "工具失败",
  run_started: "运行启动",
  run_finished: "运行结束",
  cancelled: "运行已取消",
  cancel_requested: "收到取消请求",
  retry: "暂时错误重试",
  checkpoint_saved: "保存检查点",
  budget_reserved: "预留调用预算",
  human_input: "收到人工输入",
  action_committed: "模拟动作已登记",
};
Object.assign(kinds, {
  call_reserved: "预留调用预算",
  model_failed: "模型调用失败",
  branch_started: "开始独立调查",
  branch_finished: "独立调查完成",
  join_decided: "汇总调查结果",
  review_repair: "审核返工",
  retry_scheduled: "准备重试",
  input_accepted: "已接受人工输入",
  run_registered: "已登记运行",
  action_replayed: "复用已登记动作",
  action_failed: "动作未执行",
  node_replayed: "恢复已完成节点",
  pending_created: "生成待办",
});
Object.assign(kinds, {
  run_paused: "运行已暂停",
  run_completed: "运行已结束",
  branch_replayed: "恢复已完成调查",
  approval_invalidated: "旧审批失效，重新审核",
  schema_repair: "修复输出格式",
  pending_consumed: "人工待办已提交",
  retry_started: "开始重试",
  action_started: "开始登记动作",
  agent_started: "专员开始处理",
  agent_finished: "专员处理完成",
  budget_initialized: "初始化共享预算",
  call_limit_reached: "调用预算已用完",
  dependent_policy_recheck: "汇总后复核适用政策",
  node_failed: "节点执行失败",
  node_interrupted: "节点执行中断",
  run_cancelled: "运行已取消",
  run_stopped: "运行已停止",
  schema_invalid: "输出格式不符合约定",
  validation_started: "开始代码校验",
  validation_finished: "代码校验完成",
});
Object.assign(nodes, {
  order_branch: "订单独立调查",
  candidate_branch: "候选政策调查",
  policy_candidates_branch: "候选政策调查",
  repair: "审核返工",
  await_customer: "等待客户补充",
  await_operator: "等待操作员",
  handoff: "转交人工",
  finalize: "结束处理",
});
const sources = {
  order: "订单",
  products: "商品",
  tracking: "物流",
  proof: "签收凭证",
  history: "售后历史",
  policy: "政策",
  assessment: "规则核算",
};
const reviews = {
  accept: "审核通过",
  research: "需补充调查",
  revise: "需修改建议",
  customer_info: "等待客户资料",
  handoff: "转交人工",
};
const active = new Set(["queued", "running"]);
const identities = new Set(["demo-customer-a", "demo-customer-b", "demo-operator"]);

function readStored(key, fallback) {
  try {
    return JSON.parse(sessionStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}
function store(key, value) {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* A tab can still operate without storage. */
  }
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function button(text, handler, className) {
  const node = el("button", text, className);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}
function money(cents) {
  // Format integer cents directly, without floating point round-tripping.
  if (!Number.isSafeInteger(cents) || cents < 0) return "金额超出页面显示范围";
  return `¥${Math.floor(cents / 100).toLocaleString("zh-CN")}.${String(cents % 100).padStart(2, "0")}`;
}
function parseMoney(value) {
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) throw new Error("请填写正数金额，最多两位小数。");
  const [whole, fraction = ""] = value.split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(cents) || cents <= 0) throw new Error("金额须大于零且在可显示范围内。");
  return cents;
}
function date(value) {
  if (!value || Number.isNaN(Date.parse(value))) return "暂无活动记录";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
const state = {
  actor: readStored("as.identity", "demo-customer-a"),
  selected: readStored("as.ticket", null),
  tickets: [],
  ticket: null,
  offset: 0,
  epoch: 0,
  busy: false,
  refreshing: false,
  tick: 0,
  pendingSignature: "",
  runId: null,
  cursor: 0,
  events: [],
  allEvents: false,
  retry: null,
  refreshQueued: false,
};
if (!identities.has(state.actor)) state.actor = "demo-customer-a";
$("identity").value = state.actor;

class ApiError extends Error {
  constructor(message, status = 0, code = "NETWORK_ERROR", requestId = "") {
    super(message);
    Object.assign(this, { status, code, requestId });
  }
}
async function api(path, { method = "GET", body, key, actor = state.actor } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {
      method,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${actor}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(key ? { "Idempotency-Key": key } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    const payload = await response.json();
    if (!response.ok)
      throw new ApiError(
        payload.message || "请求失败。",
        response.status,
        payload.code,
        payload.request_id,
      );
    return payload;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("连接暂时中断，请重试。提交结果尚未确认时，请重试原请求。");
  } finally {
    clearTimeout(timeout);
  }
}
function notice(message, retry = null) {
  $("notice-text").textContent = message;
  $("notice").hidden = false;
  state.retry = retry;
  $("retry").hidden = !retry;
  $("retry").textContent = retry ? "重试原请求" : "重试";
}
function errorText(error) {
  return `${error.message}${error.requestId ? `（请求 ID：${error.requestId}）` : ""}`;
}
function setBusy(value) {
  state.busy = value;
  $("identity").disabled = value;
  $("new-ticket").disabled = value || state.actor === "demo-operator";
  document
    .querySelectorAll(
      "#controls button, #pending-card button, #create-form button[type=submit], .ticket-row",
    )
    .forEach((b) => {
      b.disabled = value;
    });
  if (!value && state.ticket) renderControls();
}
function operationKey(actor, path, body, context) {
  const fingerprint = JSON.stringify([actor, path, body, context]);
  const keys = readStored("as.keys", {});
  if (!keys[fingerprint]) {
    // Save before sending. Retries and reloads reuse the original request key.
    keys[fingerprint] = `ui-${crypto.randomUUID()}`;
    const entries = Object.entries(keys).slice(-150);
    store("as.keys", Object.fromEntries(entries));
  }
  return keys[fingerprint];
}
async function mutate(path, body, context, onSuccess = null, existing = null) {
  if (state.busy) return;
  const request = existing || {
    path,
    body,
    actor: state.actor,
    key: operationKey(state.actor, path, body, context),
  };
  if (request.actor !== state.actor) {
    notice("请切换回提交时的演示身份，再重试该请求。");
    return;
  }
  setBusy(true);
  store("as.unconfirmed", request);
  try {
    const result = await api(request.path, { ...request, method: "POST" });
    store("as.unconfirmed", null);
    const keys = readStored("as.keys", {});
    Object.keys(keys).forEach((k) => {
      if (keys[k] === request.key) delete keys[k];
    });
    store("as.keys", keys);
    state.retry = null;
    notice("请求已受理。工作台会持续显示处理进度。");
    if (onSuccess) onSuccess(result);
    state.pendingSignature = "";
  } catch (error) {
    if (error.status >= 400 && error.status < 500) {
      store("as.unconfirmed", null);
      state.pendingSignature = "";
      notice(
        error.status === 409
          ? "方案或任务状态已变化。已重新读取当前方案，请查看内容后重新确认。" + errorText(error)
          : errorText(error),
      );
    } else {
      notice(errorText(error), () => mutate(path, body, context, onSuccess, request));
    }
  } finally {
    setBusy(false);
    await refresh(true);
  }
}
function selectTicket(id) {
  if (state.busy) return;
  state.selected = id;
  state.epoch++;
  state.ticket = null;
  state.pendingSignature = "";
  store("as.ticket", id);
  resetEvents(null);
  $("ticket-content").hidden = true;
  $("empty").hidden = false;
  renderList();
  refresh(true);
}
function resetEvents(runId) {
  state.runId = runId;
  state.cursor = 0;
  state.events = [];
  state.allEvents = false;
  renderEvents();
}
function renderList() {
  const query = $("search").value.toLowerCase().trim(),
    filter = $("filter").value;
  const shown = state.tickets.filter(
    (t) =>
      (filter === "all" || t.type === filter) &&
      `${t.ticket_id} ${t.supplied_order_id || ""} ${t.messages.join(" ")}`
        .toLowerCase()
        .includes(query),
  );
  $("ticket-count").textContent = state.tickets.length;
  $("ticket-list").replaceChildren(
    ...shown.map((t) => {
      const row = button(
        "",
        () => selectTicket(t.ticket_id),
        `ticket-row${t.ticket_id === state.selected ? " active" : ""}`,
      );
      row.dataset.ticket = t.ticket_id;
      row.disabled = state.busy;
      if (t.ticket_id === state.selected) row.setAttribute("aria-current", "true");
      const top = el("span", null, "row-top");
      top.append(
        el("strong", types[t.type] || t.type),
        el(
          "span",
          business[t.status] || t.status,
          `badge${t.status.startsWith("waiting_") ? " amber" : ""}`,
        ),
      );
      const bottom = el("span", null, "row-bottom");
      bottom.append(el("span", t.ticket_id), el("span", date(t.last_activity_at)));
      row.append(top, el("span", t.messages[0], "preview"), bottom);
      return row;
    }),
  );
  if (!shown.length)
    $("ticket-list").append(el("p", "暂无匹配工单，可调整筛选或新建工单。", "muted"));
  $("page-number").textContent = `第 ${state.offset / 20 + 1} 页`;
  $("prev").disabled = state.busy || state.offset === 0;
  $("next").disabled = state.busy || state.tickets.length < 20;
  $("new-ticket").disabled = state.busy || state.actor === "demo-operator";
}
function renderControls() {
  const t = state.ticket,
    run = t.latest_run,
    controls = [];
  if (!run || (!active.has(run.status) && !["paused", "interrupted"].includes(run.status))) {
    controls.push(
      button(
        run ? "重新调查" : "开始处理",
        () =>
          mutate(
            `/tickets/${t.ticket_id}/runs`,
            { workflow: "parallel", expected_input_revision: t.input_revision },
            run?.run_id || "first",
          ),
        "primary",
      ),
    );
  }
  if (run?.status === "interrupted")
    controls.push(
      button("恢复处理", () => mutate(`/runs/${run.run_id}/resume`, {}, "resume"), "primary"),
    );
  if (run && ["queued", "running", "paused", "interrupted"].includes(run.status)) {
    const cancel = button(
      run.cancel_requested ? "正在取消…" : "取消运行",
      () => mutate(`/runs/${run.run_id}/cancel`, {}, "cancel"),
      "danger",
    );
    cancel.disabled = run.cancel_requested;
    controls.push(cancel);
  }
  if (active.has(run?.status)) controls.unshift(el("span", "协作处理中…", "muted"));
  $("controls").replaceChildren(...controls);
  if (state.busy)
    $("controls")
      .querySelectorAll("button")
      .forEach((b) => {
        b.disabled = true;
      });
}
function renderTicket(ticket) {
  state.ticket = ticket;
  const run = ticket.latest_run,
    result = run?.result;
  $("empty").hidden = true;
  $("ticket-content").hidden = false;
  $("ticket-id").textContent = ticket.ticket_id;
  $("ticket-title").textContent = types[ticket.type] || ticket.type;
  $("ticket-meta").textContent =
    `${ticket.supplied_order_id ? `订单 ${ticket.supplied_order_id}` : "订单号待补充"} · 最近活动 ${date(ticket.last_activity_at)}`;
  $("business-status").textContent = business[ticket.status] || ticket.status;
  $("business-status").className = `badge${ticket.status.startsWith("waiting_") ? " amber" : ""}`;
  $("run-status").textContent = run
    ? result?.outcome === "handed_off"
      ? "已转人工"
      : runs[run.status] || run.status
    : "尚未启动";
  $("status-note").textContent =
    ticket.status === "waiting_return"
      ? "退货申请已登记，等待客户寄回；尚未履约。"
      : ticket.status === "resolved" && result?.receipts.some((r) => r.type === "issue_mock_refund")
        ? "模拟退款记录已登记。"
        : run?.status === "completed"
          ? "运行结束表示本轮处理完成，业务进度以左侧状态为准。"
          : "人工确认后，才会登记模拟业务动作。";
  $("input-version").textContent = `资料 v${ticket.input_revision}`;
  $("messages").replaceChildren(...ticket.messages.map((m) => el("p", m)));
  $("proposal-version").textContent = result?.proposal_revision
    ? `方案 v${result.proposal_revision}`
    : "";
  $("decision").textContent = result
    ? decisions[result.decision] || "本轮处理已结束"
    : active.has(run?.status)
      ? "正在调查订单与政策…"
      : "等待生成处理建议";
  $("review-summary").textContent = result
    ? [reviews[result.review_outcome] || "", ...(result.review_issues || [])]
        .filter(Boolean)
        .join(" · ")
    : run?.execution_error_code
      ? `执行中断：${run.execution_error_code}。可以恢复处理。`
      : "调查订单与政策后，建议会经过代码校验与审核。";
  $("reply").textContent = result?.customer_reply || "回复草稿将在调查和审核后显示。";
  $("gaps").replaceChildren(...(result?.gaps || []).map((q) => el("p", q.question, "gap")));
  $("receipts").replaceChildren(
    ...(result?.receipts || []).map((r) => {
      const card = el("div", null, "receipt");
      card.append(
        el("strong", `${actions[r.type] || r.type} · 已登记`),
        el(
          "p",
          `${r.order_id}${r.amount_cents === null ? "" : ` · ${money(r.amount_cents)}`} · ${business[r.business_status]}`,
        ),
        el("p", `记录 ${r.business_record_id} · ${date(r.committed_at)}`),
      );
      return card;
    }),
  );
  renderEvidence(result?.evidence || []);
  renderControls();
  renderPending(run?.pending_input, run);
  renderStats(result);
  if (state.runId !== (run?.run_id || null)) resetEvents(run?.run_id || null);
  const index = state.tickets.findIndex((t) => t.ticket_id === ticket.ticket_id);
  if (index >= 0) state.tickets[index] = ticket;
  renderList();
}
function renderEvidence(evidence) {
  $("evidence-count").textContent = evidence.length ? `${evidence.length} 份快照` : "";
  $("evidence").replaceChildren(
    ...evidence.map((e) => {
      const row = el("div", null, "evidence-item"),
        content = el("div");
      row.append(el("span", (sources[e.source_type] || "资料").slice(0, 1), "evidence-icon"));
      content.append(
        el(
          "strong",
          `${sources[e.source_type] || e.source_type} · ${e.source_id} · v${e.source_version}`,
        ),
        el("p", e.summary),
      );
      const details = el("details");
      details.append(
        el("summary", "查看快照标识"),
        el("p", `${e.evidence_id} · 观察时间 ${date(e.observed_at)}`),
      );
      content.append(details);
      row.append(content);
      return row;
    }),
  );
  if (!evidence.length) $("evidence").append(el("p", "本轮尚无可展示的来源快照。", "muted"));
}
function renderStats(result) {
  const values = [
    [result?.model_calls ?? "—", "模型调用"],
    [result?.tool_calls ?? "—", "工具查询"],
    [
      result?.elapsed_ms === null || result?.elapsed_ms === undefined
        ? "—"
        : `${(result.elapsed_ms / 1000).toFixed(1)}s`,
      "活动耗时",
    ],
  ];
  $("stats").replaceChildren(
    ...values.map(([value, label]) => {
      const node = el("div", null, "stat");
      node.append(el("strong", value), el("span", label));
      return node;
    }),
  );
  $("stats").title =
    `格式修复：${result?.schema_repairs ?? "未报告"}；审核返工：${result?.review_reworks ?? "未报告"}；脚本模型未报告 token 和费用。`;
  $("stats-meta").textContent =
    `格式修复 ${result?.schema_repairs ?? "—"} · 审核返工 ${result?.review_reworks ?? "—"} · token / 费用未报告`;
}
function renderPending(pending, run) {
  const signature = JSON.stringify([state.actor, run?.run_id, pending]);
  if (signature === state.pendingSignature) return;
  const previous = $("pending-card").dataset.pending;
  state.pendingSignature = signature;
  const card = $("pending-card");
  card.replaceChildren();
  card.hidden = !pending;
  delete card.dataset.pending;
  if (!pending) return;
  card.dataset.pending = pending.pending_id;
  if (previous && previous !== pending.pending_id)
    notice(`当前待办已更新为方案 v${pending.proposal_revision}。请阅读新内容，重新填写或确认。`);
  const heading = el("div", null, "card-heading");
  heading.append(
    el("h3", pending.kind === "customer_info" ? "需要客户补充" : "需要人工确认"),
    el("span", `方案 v${pending.proposal_revision}`, "subtle"),
  );
  card.append(heading);
  if (pending.kind === "operator_decision") {
    pending.actions.forEach((a) => {
      const detail = el("div", null, "action-detail");
      detail.append(
        el("strong", actions[a.type] || a.type),
        el(
          "p",
          `操作对象：${a.order_id}${a.amount_cents === null ? "" : ` · 金额 ${money(a.amount_cents)}`}`,
        ),
        el(
          "p",
          `依据政策：${a.policies.map((p) => `${p.policy_id} v${p.version}`).join("、") || "旧版本待办未提供政策摘要"}`,
        ),
      );
      card.append(detail);
    });
  } else pending.questions.forEach((q) => card.append(el("p", q.question)));
  card.append(
    el(
      "p",
      `资料 v${pending.input_revision} · 方案 v${pending.proposal_revision} · 绑定 ${pending.proposal_hash.slice(0, 12)}`,
      "binding-note",
    ),
  );
  if (!pending.can_respond) {
    card.append(
      el(
        "p",
        pending.expected_role === "operator"
          ? "请切换到操作员身份，阅读处理内容并确认。"
          : "该待办需要工单所属客户补充。请切换到对应客户身份。",
        "gap",
      ),
    );
    return;
  }
  if (
    pending.actions.some((a) => a.amount_cents !== null && !Number.isSafeInteger(a.amount_cents))
  ) {
    card.append(el("p", "金额超出页面可确认范围，请通过已验证的 CLI 入口核对处理。", "form-error"));
    return;
  }
  const form = el("form");
  form.setAttribute(
    "aria-label",
    pending.kind === "customer_info" ? "客户补充表单" : "操作员确认表单",
  );
  const formError = el("p", "", "form-error");
  formError.setAttribute("role", "alert");
  const body = {
    pending_id: pending.pending_id,
    input_revision: pending.input_revision,
    proposal_revision: pending.proposal_revision,
    proposal_hash: pending.proposal_hash,
    action_hashes: Object.fromEntries(pending.actions.map((a) => [a.action_id, a.content_hash])),
  };
  const inputs = new Map();
  let decision;
  if (pending.kind === "customer_info") {
    pending.questions.forEach((q) => {
      const label = el("label", q.question),
        input = el("input");
      input.name = q.field;
      input.required = true;
      input.maxLength = 1000;
      if (q.field === "order_id") {
        input.placeholder = "例如 ORD-004";
        input.maxLength = 64;
        input.pattern = "[A-Za-z0-9_-]+";
      }
      inputs.set(q.field, input);
      label.append(input);
      form.append(label);
    });
  } else {
    const label = el("label", "处理决定");
    decision = el("select");
    decision.name = "decision";
    const refunds = pending.actions.filter((a) => a.type === "issue_mock_refund");
    [
      ["approve", "批准当前方案"],
      ["reject", "拒绝执行，转人工处理"],
      ...(refunds.length ? [["revise", "修改退款金额，重新审核"]] : []),
    ].forEach(([value, text]) => {
      const option = el("option", text);
      option.value = value;
      decision.append(option);
    });
    label.append(decision);
    form.append(label);
    refunds.forEach((a) => {
      const amountLabel = el("label", "调整后的退款金额（元）"),
        input = el("input");
      input.name = a.action_id;
      input.inputMode = "decimal";
      input.value = `${Math.floor(a.amount_cents / 100)}.${String(a.amount_cents % 100).padStart(2, "0")}`;
      amountLabel.append(input);
      amountLabel.hidden = true;
      form.append(amountLabel);
      inputs.set(a.action_id, input);
      decision.addEventListener("change", () => {
        amountLabel.hidden = decision.value !== "revise";
      });
    });
    const checkboxLabel = el("label", null, "checkbox"),
      checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.name = "read_confirmation";
    checkbox.required = true;
    decision.addEventListener("change", () => {
      checkbox.checked = false;
    });
    checkboxLabel.append(
      checkbox,
      el(
        "span",
        `我已核对方案 v${pending.proposal_revision} 的操作对象、金额和政策；批准后会登记模拟业务记录。`,
      ),
    );
    form.append(checkboxLabel);
  }
  form.append(formError);
  const submit = el(
    "button",
    pending.kind === "customer_info" ? "提交补充资料" : "提交处理决定",
    "primary",
  );
  submit.type = "submit";
  submit.disabled = state.busy;
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.busy) return;
    if (state.ticket?.latest_run?.pending_input?.pending_id !== pending.pending_id) {
      notice("待办已更新，请查看当前方案后再提交。");
      refresh(true);
      return;
    }
    try {
      let payload;
      if (pending.kind === "customer_info") {
        const answers = Object.fromEntries(
          [...inputs].map(([field, input]) => [field, input.value.trim()]),
        );
        if (Object.values(answers).some((value) => !value))
          throw new Error("请填写需要补充的资料。");
        payload = { ...body, answers };
      } else {
        const refund_amounts =
          decision.value === "revise"
            ? Object.fromEntries(
                [...inputs].map(([field, input]) => [field, parseMoney(input.value.trim())]),
              )
            : {};
        payload = { ...body, decision: decision.value, refund_amounts };
      }
      formError.textContent = "";
      mutate(`/runs/${run.run_id}/responses`, payload, pending.pending_id);
    } catch (error) {
      formError.textContent = error.message;
    }
  });
  card.append(form);
}
function renderEvents() {
  const shown = state.allEvents ? state.events : state.events.slice(-60);
  $("timeline").replaceChildren(
    ...shown.map((event) => {
      const item = el(
        "li",
        null,
        event.tool ? "tool" : event.node === "review" || event.role === "reviewer" ? "review" : "",
      );
      item.dataset.sequence = event.sequence;
      item.append(
        el(
          "strong",
          `${event.node ? (nodes[event.node] || event.node) + " · " : ""}${event.tool ? (tools[event.tool] || event.tool) + " · " : ""}${kinds[event.kind] || event.kind}`,
        ),
        el(
          "small",
          `${roles[event.role] || "流程调度"} · #${event.sequence}${event.elapsed_ms === null || event.elapsed_ms === undefined ? "" : ` · ${(event.elapsed_ms / 1000).toFixed(2)}s`}`,
        ),
      );
      return item;
    }),
  );
  $("event-empty").hidden = state.events.length > 0;
  $("event-count").textContent = state.events.length
    ? `${state.events.length} 条事件 · 已同步到 #${state.cursor}${shown.length < state.events.length ? " · 显示最近 60 条" : ""}`
    : "";
  $("all-events").hidden = state.events.length <= 60 || state.allEvents;
  $("roles").replaceChildren(
    ...Object.entries(roles)
      .filter(([key]) => key !== "application" && state.events.some((e) => e.role === key))
      .map(([key, label]) => {
        const latest = state.events.findLast((e) => e.role === key);
        return el(
          "span",
          label,
          `role-chip${latest && ["model_started", "tool_started"].includes(latest.kind) ? " active" : ""}`,
        );
      }),
  );
}
async function readEvents(runId, epoch) {
  // Advance only after receiving a complete page. A failed request retains its cursor.
  for (let page = 0; page < 10; page++) {
    const data = await api(`/runs/${runId}/events?after_seq=${state.cursor}&limit=100`);
    if (state.epoch !== epoch || state.runId !== runId) return;
    const fresh = data.events.filter((e) => e.sequence > state.cursor);
    state.events.push(...fresh);
    state.cursor = data.next_after_seq;
    if (fresh.length) renderEvents();
    if (!data.has_more) return;
  }
}
async function refresh(includeList = false) {
  if (state.refreshing) {
    state.refreshQueued = true;
    return;
  }
  state.refreshing = true;
  const epoch = state.epoch,
    actor = state.actor,
    selected = state.selected;
  try {
    if (includeList || state.tick % 6 === 0) {
      const tickets = await api(`/tickets?limit=20&offset=${state.offset}`, { actor });
      if (state.epoch !== epoch) return;
      state.tickets = tickets;
      renderList();
    }
    if (selected) {
      const ticket = await api(`/tickets/${selected}`, { actor });
      if (state.epoch !== epoch) return;
      renderTicket(ticket);
      if (state.runId) await readEvents(state.runId, epoch);
    }
    if (state.epoch === epoch) $("connection").textContent = "服务已连接 · 自动同步";
  } catch (error) {
    if (state.epoch !== epoch) return;
    $("connection").textContent = "连接中断 · 正在重连";
    if (error.status === 404 || error.status === 403) {
      state.selected = null;
      state.ticket = null;
      store("as.ticket", null);
      $("ticket-content").hidden = true;
      $("empty").hidden = false;
      resetEvents(null);
    }
    // Preserve an uncertain POST retry while polling also fails.
    if (!state.retry) notice(errorText(error), () => refresh(true));
  } finally {
    state.refreshing = false;
    state.tick++;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      queueMicrotask(() => refresh(true));
    }
  }
}
$("identity").addEventListener("change", () => {
  state.actor = $("identity").value;
  state.epoch++;
  state.offset = 0;
  store("as.identity", state.actor);
  state.pendingSignature = "";
  state.tickets = [];
  state.ticket = null;
  renderList();
  $("ticket-content").hidden = true;
  $("empty").hidden = false;
  resetEvents(null);
  state.retry = null;
  $("notice").hidden = true;
  restoreUnconfirmed();
  refresh(true);
});
$("search").addEventListener("input", renderList);
$("filter").addEventListener("change", renderList);
$("prev").addEventListener("click", () => {
  if (state.busy) return;
  state.offset = Math.max(0, state.offset - 20);
  state.epoch++;
  state.tick = 0;
  refresh(true);
});
$("next").addEventListener("click", () => {
  if (state.busy) return;
  state.offset += 20;
  state.epoch++;
  state.tick = 0;
  refresh(true);
});
$("all-events").addEventListener("click", () => {
  state.allEvents = true;
  renderEvents();
});
$("retry").addEventListener("click", () => state.retry?.());
$("dismiss").addEventListener("click", () => {
  $("notice").hidden = true;
  state.retry = null;
});
$("new-ticket").addEventListener("click", () => {
  $("create-error").textContent = "";
  $("create-dialog").showModal();
});
$("close-dialog").addEventListener("click", () => $("create-dialog").close());
$("create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.busy) return;
  const data = new FormData(event.currentTarget),
    message = data.get("message").trim();
  if (!message) {
    $("create-error").textContent = "请描述售后诉求。";
    return;
  }
  const body = {
    type: data.get("type"),
    supplied_order_id: data.get("order").trim() || null,
    message,
  };
  mutate("/tickets", body, "create", (result) => {
    $("create-dialog").close();
    $("create-form").reset();
    state.selected = result.ticket_id;
    store("as.ticket", result.ticket_id);
    state.epoch++;
    state.offset = 0;
  });
});
function restoreUnconfirmed() {
  const request = readStored("as.unconfirmed", null);
  if (request && request.actor === state.actor) {
    notice("上次提交结果尚未确认。重试会使用同一请求键，避免重复执行。", () =>
      mutate(
        request.path,
        request.body,
        "retry",
        request.path === "/tickets"
          ? (result) => {
              $("create-dialog").close();
              state.selected = result.ticket_id;
              store("as.ticket", result.ticket_id);
              state.epoch++;
            }
          : null,
        request,
      ),
    );
  }
}
async function poll() {
  await refresh();
  setTimeout(poll, document.hidden ? 2500 : 800);
}
restoreUnconfirmed();
poll();
