# 售后工单多 Agent 工作台：技术方案

版本：0.7（P00～P04 复核修正；P05 未开始）

日期：2026-10-02

实施路线：[plan.md](../plan.md)

阶段复核：[000～004 复核记录](reviews/000-004.md)。当前规则记录为 rules-v2；不适用政策和非退款金额归为工具参数错误，拒绝 / 已有申请也须绑定完整依据并复算。

## 1. 项目目标与范围

通过一个本地可运行的售后系统，学习工具调用、Agent 分工、状态交接、审核返工、人工介入、并行执行和失败恢复。最终交付可演示的应用、可重复执行的测试、评估结果以及架构说明。

首版业务覆盖三类工单：

| 类型 | 示例诉求 | 需要调查的事实 | 预期处理方式 |
| --- | --- | --- | --- |
| 物流延迟 | “预计昨天送到，现在还没收到” | 订单、预计送达时间、物流事件 | 解释进度；必要时提议创建物流调查单 |
| 签收未收到 | “显示签收，但我没有拿到” | 签收事件、凭证、用户陈述、物流调查结果 | 追问或调查；有明确丢件证据时才评估退款 |
| 退货申请 | “收到三天了，商品未拆封，想退货” | 收货时间、商品类别、商品状态、历史售后 | 判断模拟政策适用性，提议登记退货申请 |

这里的售后政策是为学习项目编写的虚构规则，记录版本和生效时间。项目不接入真实支付、快递、客户消息发送服务。第一版输出处理建议和回复草稿；后续阶段增加人工确认后的模拟动作。

首轮暂不建设多租户、生产登录系统、外部消息通道、长期用户画像、向量数据库和分布式任务队列。这些作为后续扩展，避免与学习 Agent 协作争夺实施精力。

### 1.1 最终用户体验

1. 在工单列表选择或创建工单。
2. 启动处理，查看当前 Agent、工具调用和已收集证据。
3. 需要补充信息时填写回答；需要操作员判断时审核具体方案。
4. 查看处理建议、回复草稿、引用、模拟动作结果和执行统计。
5. 关闭程序后重新启动，能够找到等待中的工单并继续处理。

### 1.2 完成标准

- 三类正常场景和关键异常都有固定样例。
- 重要结论关联订单、物流或政策证据；资料不足会产生明确缺口。
- 用户只能访问属于自己的模拟订单；模型不能更改可信身份。
- 审核返工有上限，预算耗尽有明确结束方式。
- 人工确认绑定到具体动作与版本，旧确认不能授权新方案。
- 重复请求和崩溃恢复不会重复产生模拟退款或退货记录。
- 有单 Agent 与多 Agent 的同条件对照报告，结果如实记录。

## 2. 技术选型与职责

| 层次 | 选型 | 用途 |
| --- | --- | --- |
| 运行环境 | Python 3.12，uv | 依赖、虚拟环境和命令入口；P00 校验本机兼容性 |
| 外层编排 | LangGraph `StateGraph` | 显式状态、有限路由、返工、interrupt 和检查点 |
| Agent 循环 | LangChain `create_agent` | 模型调用、工具调用循环和结构化结果 |
| 数据契约 | Pydantic 2 | 校验输入、工具返回、Agent 结果和 API 数据 |
| 业务数据库 | SQLite | 订单、工单、动作账本、事件和运行统计 |
| 执行检查点 | LangGraph SQLite checkpointer | 保存图的运行状态，与业务库分开存储 |
| Web 后端 | FastAPI | 工单、启动处理、补充信息、人工确认和事件接口 |
| 首版界面 | 原生 HTML / CSS / JavaScript | 工单列表、详情、处理过程和审核表单，由后端服务静态资源 |
| 自动化验证 | pytest，HTTPX；界面阶段增加 Playwright | 单元、图集成、HTTP 和浏览器测试 |
| 模型接入 | 当前为 ScriptedChatModel；后续接入一个 LangChain provider adapter | 先验证离线循环；初期各角色使用同一模型，之后再比较配置 |

依赖范围在 `pyproject.toml` 中声明，实际兼容版本由 P00 的 smoke test 验证并锁定到 `uv.lock`，后续升级需要通过回归检查。采用当前 `create_agent` 接口，不以旧教程中的 AgentExecutor 作为新项目入口。[S1]

## 3. 系统架构

```mermaid
flowchart TB
    CLI[命令行] --> APP[应用服务]
    UI[工单页面] --> API[FastAPI]
    API --> APP
    APP --> GRAPH[LangGraph 工单主图]
    GRAPH --> COORD[客服协调 Agent]
    GRAPH --> ORDER[订单物流 Agent]
    GRAPH --> POLICY[售后政策 Agent]
    GRAPH --> REVIEW[审核 Agent]
    ORDER --> READ[订单和物流读取工具]
    POLICY --> RULES[政策检索与规则计算工具]
    REVIEW --> EVIDENCE[证据读取与引用检查]
    GRAPH --> GATE[代码校验和人工介入节点]
    GATE --> ACTION[模拟动作服务]
    READ --> DB[(业务 SQLite)]
    RULES --> DB
    ACTION --> DB
    GRAPH --> CP[(检查点 SQLite)]
    APP --> EVENTS[事件和运行统计]
    EVENTS --> DB
```

### 3.1 调用边界

- `domain` 定义订单、工单、政策、动作和纯业务规则，不依赖 LangChain / LangGraph。
- `repositories` 负责数据访问，不直接调用模型。
- `tools` 为 Agent 包装只读业务能力，输入和输出都经过校验。
- `agents` 定义提示词、允许工具和结构化结果。
- `workflows` 负责角色衔接、上下文裁剪和合法路由。
- `services` 负责启动、恢复、人工输入、模拟动作和运行生命周期。
- CLI 和 HTTP 共同调用 services，避免各自实现一套工单逻辑。

模型输出是候选判断。订单归属、金额、时间边界、动作权限和幂等性由代码检查；任何模型“通过”都不能绕过这些条件。

### 3.2 图节点与 Agent 的区别

图同时包含 Agent 节点和普通代码节点。数据库读取、状态更新、权限验证不需要额外调用模型。审核角色可以先实现为结构化评估节点，后续再增加证据读取工具；避免为了凑角色数量创建无实际职责的 Agent。

第一版采用受约束的协调模式：客服协调 Agent 提出调查计划，外层主图执行允许的路径。后续仍保留明确终止规则，不允许模型生成任意节点名、任意 SQL 或任意工具。

### 3.3 P03 已实现的单 Agent 基线

外层工单主图从 P04 开始。P03 的 `agents/runner.py` 使用真实 `create_agent`，接入 10 个只读业务工具、`ToolStrategy(ResolutionProposal)` 和调用中间件：

```mermaid
flowchart LR
    T[工单与可信上下文] --> A[create_agent]
    A --> M[ScriptedChatModel]
    M -->|业务 tool_calls| TOOLS[只读工具]
    TOOLS -->|ToolMessage + call ID| M
    M -->|结构化输出| S[Pydantic schema]
    S -->|无效：有限反馈| M
    S -->|有效| V[独立代码校验与规则复算]
    V --> R[候选结果、证据、消息与事件 JSON]
```

脚本是有限的模型响应生成器，从收到的 ToolMessage 读取事实并生成下一步调用，不读取 repository 或 gold。它检查预期工具序列、工具是否绑定、call ID 是否匹配或重复；未定义路径直接失败。改动订单实付金额后，脚本会依据新工具结果生成相应退款候选。此机制验证框架和业务约束，不代表真实模型智能或质量。

Pydantic 校验通过后，独立代码验证工单/订单范围、事实声明、引用来源与版本、动作和 assessment 对应关系、完整政策与规则输入，再使用候选金额重新调用 evaluate_policy。复算占用同一工具预算；不通过时保留原始 proposal 和问题，accepted_proposal 为 null。通过后仍是不可执行的候选，动作数为 0。

结果保存在 baseline-run-v1 JSON，可由 report show 跨进程读取；不保存可恢复的运行检查点、不更改工单业务状态。写文件先落临时文件再原子发布且禁止覆盖。读取时验证报告结构，但文件本身不能证明来源，也不能授权动作。详见 [ADR 003](decisions/003-offline-agent-baseline.md) 与 [第 003 轮记录](rounds/003.md)。

用户选择当前只用离线脚本模型，因此 live 工厂边界及 live-smoke 返回 skipped / LIVE_PROVIDER_DEFERRED。真实 provider、网络 smoke 及语言质量评估均未实现或执行。

### 3.4 P04 已实现的串行主图

`workflows/serial.py` 用 StateGraph(TicketState) 建立 intake、order、policy、draft、validate 五个节点。intake 和 draft 是协调角色的两个独立调用，order 和 policy 包装各自的 create_agent 子图；validate 是代码节点。每次角色调用都创建新模型脚本和 messages，父图仅投影必要输入、提取 Pydantic 输出后转为 JSON 更新。[LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)

正常路线为 intake→order→policy→draft→validate→END。缺订单号或未知意图走 intake→draft→validate；无法读取订单走 intake→order→draft→validate。Route 和 TRANSITIONS 限定合法路径，图没有返工环路；P05 才加入审核和 interrupt。

协调角色整理声明意图、原始订单引用和客户陈述，输出 IntakeResult / InvestigationPlan，再依据专员摘要输出共享 ResolutionProposal。当前离线协议要求这些槽位和固定计划与可信输入一致，不把“按类型编排”当作自由文本 NLU 验证。订单角色只有 5 个事实工具；政策角色只有 search_policies、get_policy、evaluate_policy；协调没有业务工具。

OrderInvestigation 包含独立来源的事实快照、引用和工具错误；PolicyAssessment 包含政策快照、规则 assessment 和查询错误。父图核对来源/会话/版本、事实内容，以及摘要与实际子图工具结果是否一致。模型增添事实、替换订单或伪造引用会在交接处停止，已有节点的结果和错误位置保留。政策只接收订单摘要，不接收客服或订单角色的内部 messages。

TicketState 保存 JSON 资料与节点轨迹，不含 repository、ToolSession、模型、数据库连接、密钥或子图 messages。运行依赖保存在节点闭包；JSON 与 LangGraph serializer 往返测试通过。P04 没有业务检查点、跨进程恢复或动作服务。multi-run-v1 报告包含角色输入/输出、角色统计、状态、事件及实际主图 Mermaid；原 baseline-run-v1 仍可读取。

### 3.5 P05 已实现的审核与人工输入图

`workflows/reviewed.py` 提供独立 review-v1 图；CLI 显式选择 `--architecture multi --workflow reviewed`，默认 serial 入口保留。前三个角色的输入、事实交接和代码校验继续复用；审核角色用 create_agent 输出 ReviewResult，只绑定 validate_evidence_refs / get_evidence。应用对规则失败、查询失败、必要缺口和受控中文模板计算最低要求，模型 accept 不能绕过代码检查。当前不验证真实模型质量或任意自然语言改写。

repair / research 节点实现共享最多两次返工和定向补查。订单来源按实际读取结果合并；凭证补查保留已有政策 assessment，其他规则输入刷新后重算政策。纯措辞只改写；持续失败或调用预算耗尽转人工。操作员修改退款金额也消耗该计数，schema 修复仍独立按 P03 全局上限执行。

prepare_customer / prepare_operator 创建绑定版本和内容的 PendingInput；wait_customer / wait_operator 使用真实 interrupt。InMemoryReviewRun 持有图与 InMemorySaver，恢复前检查 role / actor / pending / run / ticket / input_revision / proposal_revision / proposal_hash / action hashes，再用 Command(resume=...)。等待节点重执行不创建新待办；恢复锁和已消费集合防止重复或并发消费。

客户补订单号后在原客户范围核验，其他回答只更新陈述。操作员 approve / reject / revise 当前退款金额；修改后动作逻辑 ID 保持稳定，方案版本、内容哈希与 pending ID 更新，旧批准失效。批准前重新复算当前运行可信快照，确认结果不写业务库。执行前最新数据、时钟与持久恢复留给 P06。

review-run-v1 保留审核/版本/待办历史/确认，核对图状态、方案与批准内容一致。返回快照与运行对象隔离，保存文件不能恢复或授权动作。身份仅为本地演示，认证在 P08；checkpointer 文件路径在 P06 才使用。实现、测试与边界见 [ADR 005](decisions/005-bounded-review-and-human-input.md) 和 [第 005 轮记录](rounds/005.md)。

所有角色共用 P03 的调用预算和默认 1 次 schema 修复预算，包含提前结束后的修复和验证器复算。单模型超时及工具查询限制继续生效；暂停后的预算恢复和全局并发仍是后续工作。实际图和三类轨迹见 [Mermaid](graphs/p04-serial.mmd)、[轨迹 JSON](graphs/p04-demo-traces.json)，设计依据见 [ADR 004](decisions/004-serial-role-handoffs.md)。

## 4. Agent 设计

| 角色 | 接收的上下文 | 允许工具 | 输出契约 | 可决定的事情 |
| --- | --- | --- | --- | --- |
| 客服协调 | 用户诉求、已知槽位、各专员结果、审核意见 | 初期通过图委派专员；不暴露写工具 | `IntakeResult`、`InvestigationPlan`、`ResolutionProposal` | 业务分类、所需调查、追问、回复草稿 |
| 订单物流 | 一个工单对应的可信客户范围、订单 ID、调查问题 | `get_order`、`get_order_products`、`get_tracking`、`get_delivery_proof`、`get_after_sales_history` | `OrderInvestigation` | 查询顺序、是否需要更多订单证据 |
| 售后政策 | 意图、订单事实、商品条件、有效政策目录 | `search_policies`、`get_policy`、`evaluate_policy` | `PolicyAssessment` | 查哪些政策、哪些条件仍缺失 |
| 审核 | 候选方案、规则检查结果、可见证据和审核标准 | `get_evidence`、`validate_evidence_refs` | `ReviewResult` | 接受、要求补查、要求修改或建议人工接手 |

同一模型可承担不同角色。每个专员使用自己的 messages 和工具集，结束后仅返回结构化摘要与引用，不把完整内部对话复制给所有角色。[S2]

提示词文件需要包含：职责、输入解释、工具用途、完成标准、输出 schema、缺失信息处理、引用要求和行动边界。提示词、schema、政策都记录版本，便于复现。

P05 已实现四个角色，角色提示词随代码版本保存于 serial.py；review-v1 提供定向返工和同进程人工确认。离线审核以可信事实、确定性规则和受控中文模板为边界，完整自然语言语义评估与真实模型仍延期。此时只生成候选并记录确认，尚不构成持久恢复和业务执行系统。

## 5. 工单主流程

```mermaid
flowchart TD
    START[载入工单与可信身份] --> INTAKE[理解诉求与制定调查计划]
    INTAKE --> MISSING{订单等必要信息是否缺失}
    MISSING -->|缺失| CUSTOMER[等待用户补充]
    CUSTOMER -->|恢复| INTAKE
    MISSING -->|完整| ORDER[调查订单与物流]
    ORDER --> POLICY[检索政策并计算适用条件]
    POLICY --> DRAFT[形成处理建议和回复草稿]
    DRAFT --> VALIDATE[代码检查权限、规则、金额与引用]
    VALIDATE -->|合法| REVIEW[审核方案]
    VALIDATE -->|需要补查| REPAIR[制定定向补查任务]
    VALIDATE -->|需要改写| DRAFT
    VALIDATE -->|无法继续| HANDOFF[保存方案并人工接手]
    REVIEW -->|通过且无需动作| FINAL[输出建议]
    REVIEW -->|缺证据且仍有预算| REPAIR
    REPAIR --> ORDER
    REPAIR --> POLICY
    REVIEW -->|需要修改| DRAFT
    REVIEW -->|需要动作| HUMAN[等待操作员确认]
    HUMAN -->|批准| REVALIDATE[重新校验事实、政策和动作]
    HUMAN -->|拒绝| HANDOFF
    REVALIDATE -->|仍有效| EXEC[执行模拟动作]
    REVALIDATE -->|已变化| REFRESH[更新事实并使旧方案失效]
    REFRESH --> ORDER
    EXEC --> FINAL
    REVIEW -->|返工耗尽或矛盾无法解决| HANDOFF
```

图中的分叉表达允许的补查目标；单次补查只调用被列入任务的专员。并行阶段先让订单调查和“政策候选检索”并行，合并订单事实后再计算政策适用性，保留依赖关系。

### 5.1 两组状态

工单业务状态与运行状态分别记录：

| 业务状态 | 意义 |
| --- | --- |
| `new` | 尚未开始 |
| `processing` | 正在处理 |
| `waiting_customer` | 等待用户资料 |
| `waiting_review` | 等待操作员决策 |
| `waiting_return` | 退货申请已登记，等待后续履约 |
| `resolved` | 信息答复或模拟退款等已完成当前业务目标 |
| `handed_off` | 交给人工处理，保留证据和建议 |

运行状态：`queued`、`running`、`paused`、`completed`、`failed`、`interrupted`、`cancelled`。

图结束不代表售后业务完成。例如登记退货申请后，本轮 run 可以 `completed`，工单应为 `waiting_return`。返工和工具错误不能把工单误标为 `resolved`。

### 5.2 初始限制

- 最大返工次数：2 次；审核和代码校验触发的补查、改写共享这一计数，避免校验循环绕过上限。
- 一次补充用户输入产生新的 `input_revision`，已被回答的缺口不再追问。
- 有效政策冲突、关键证据矛盾、预算耗尽时保存现有结果并人工接手。
- 未知意图转人工；无法访问订单时不继续查询其物流或其他资料。

## 6. 数据与状态契约

### 6.1 核心业务模型

| 模型 | 关键字段 |
| --- | --- |
| `Ticket` | id、customer_id、supplied_order_id、order_id、type、messages、status、input_revision、version |
| `Order` | id、customer_id、items、paid_cents、refunded_cents、status、received_at、version |
| `TrackingEvent` | order_id、event_type、occurred_at、source、version |
| `DeliveryProof` | order_id、proof_status、source、version；缺失凭证是独立状态 |
| `Policy` | id、version、effective_from/to、scope、conditions、allowed_actions、clauses |
| `Evidence` | id、source_type、source_id、source_version、observed_at、facts、excerpt |
| `ActionProposal` | action_id、type、order_id、amount_cents、reason、evidence_ids、policy_refs、proposal_revision |
| `PendingInput` | id、run_id、kind、requested_fields/action_id、revision、resolved_at |

金额统一为整数分；时间存储为带时区 UTC，页面按 Asia/Shanghai 显示。业务规则接收注入的 `as_of_time`，测试不依赖机器当前时间。

P01 区分 `Ticket.supplied_order_id`（用户填写，可能缺失、错误或跨客户）与 `Ticket.order_id`（核验后属于该客户的订单）。后者与 customer_id 采用复合外键；错误引用只作为原始输入保留，不加载对方订单资料。具体原因和验证见 [ADR 001](decisions/001-fixture-and-order-references.md)。

用户陈述、物流系统事件、政策条款分别标注来源，不能把“用户说未收到”转换成“物流已确认丢失”。证据 ID、版本和来源由程序生成。

### 6.2 Agent 结果模型

- `IntakeResult`：intent、order_id 候选、slots、missing_fields。
- `InvestigationPlan`：任务 ID、owner、目标、所需证据、依赖任务。
- `OrderInvestigation`：verified_facts、user_claims、evidence_ids、gaps、tool_errors。
- `PolicyAssessment`：policy_refs、条件计算结果、allowed_actions、missing_facts、conflicts。
- `ResolutionProposal`（P03 已实现）：ticket_id、order_id、decision、claims、evidence_refs、customer_reply_draft、actions、unresolved_questions、candidate_only。
- `ReviewResult`：outcome、issues、targeted_tasks、revision_instructions。

P04 已实现的交接契约为 IntakeSlots（原始订单引用、客户陈述）、IntakeResult（声明意图、缺口、固定计划）、InvestigationPlan（order-facts→policy-rules 两项任务及工具权限）、OrderInvestigation（outcome、事实快照、错误）、PolicyAssessment（search_completed、政策证据、规则 assessment、错误）。事实快照使用 source_type、source_id、ref、facts，来源和内容与 ToolSession 中的证据核对。后续补查协议需要显式新增版本，不将模型输出任意字符串当路由或工具授权。

路由只接受枚举：`accept`、`research_more`、`revise`、`human_review`、`handoff`。非法结构最多修复一次，仍失败则结束为可诊断的失败或人工接手，不做无限 JSON 重试。[S3]

P03 的 decision 为 inform_progress、propose_logistics_investigation、propose_return、propose_refund、request_information、existing_application、human_review、decline_request。FactClaim 只接受定义的事实字段及其引用；ActionCandidate 关联动作、订单、整数分金额、assessment_ref、policy_refs 和 evidence_refs，并要求操作员确认。schema 验证 decision/action 的一致性；真实事实是否支持、金额是否在余额内由随后代码判断。P05 的字符串草稿只接受受控中文模板；通用语义审核和真实模型验证仍延期。

000～004 复核后，decline_request 只接纳当前退货请求的完整不符合条件依据，不能用退款候选失败拒绝物流调查诉求。decline_request 与 existing_application 都绑定订单、适合当前意图的 assessment 和完整规则输入，并在同一工具预算内复算。ProposalValidation 分别记录 rechecked_actions、rechecked_decisions，旧报告缺少后者时默认 0。运行报告校验已通过建议与原始建议一致、工单一致及状态 / 校验结果一致；失败的原始建议仍保留用于诊断。静态文件一致性不代表来源认证。

### 6.3 图状态

外层 `TicketState` 使用 `TypedDict`；节点输入/输出在边界通过 Pydantic 校验后转为可序列化字典。`create_agent` 的专员状态保留其要求的消息 schema，不直接复用业务 Pydantic 模型作为 Agent state。[S4]

图状态包含：

```text
schema_version, ticket_id, run_id, thread_id
input_revision, proposal_revision, as_of_time
intent, slots, investigation_tasks
order_findings, policy_candidates, policy_assessment
evidence_by_id, proposal, validation_result, review_result
pending_input_id, revision_count, budget_snapshot
errors, final_result
```

运行时依赖对象、数据库连接和模型密钥放入 runtime context，不写入图状态。身份范围从应用服务注入，模型不能指定客户身份。

并行 worker 仅返回本任务结果。证据按 ID 合并；同 ID 同内容去重，同 ID 不同内容产生冲突；任务按 task_id 和 revision 归并。由单个汇总节点生成最终 proposal，避免并行覆盖标量字段。

## 7. 工具与业务规则

### 7.1 工具契约

| 工具 | 业务用途 | 重要检查 |
| --- | --- | --- |
| `get_order(order_id)` | 获取允许访问的订单 | 可信 customer_id 范围、只返回必要字段 |
| `get_order_products(order_id)` | 查询订单商品类别 | 先核验订单归属，商品范围由订单推导 |
| `get_tracking(order_id)` | 获取物流事件 | 与工单订单范围一致，保留事件时间 |
| `get_delivery_proof(order_id)` | 获取签收凭证状态 | 区分凭证缺失、未查询成功、凭证存在 |
| `get_after_sales_history(order_id)` | 查重复售后和已退款情况 | 归属与金额事实 |
| `search_policies(intent)` | 按明确标签检索政策候选 | 商品范围从订单推导，时间从应用注入；首版无向量检索 |
| `get_policy(policy_id, version)` | 读取条款和条件 | 来源可追溯；不存在的 ID 明确报错 |
| `evaluate_policy(order_ref, policy_refs, action, ...)` | 用可信事实计算适用性 | 商品/物流/历史只接受证据引用；退款候选金额为整数分 |
| `get_evidence(ref)` | 审核证据 | 同时验证来源版本、工单和调查会话 |
| `validate_evidence_refs(refs)` | 检查引用存在和版本匹配 | 引用结构有效不代表语义必然正确，仍需审核 |

P02 已实现统一 `ToolResult`：`schema_version`、`ok`、`data`、`evidence_refs`、`error`；引用为 `{evidence_id, source_version}`，错误为 `{code, message, retryable}`。失败结果不携带业务资料与证据。超时、没有记录、缺订单号和归属不符使用不同错误码。工具结果中的外部文本作为资料处理，不能成为系统指令。

`ToolSession` 由应用读取工单后创建；可信上下文包括 customer_id、ticket_id、ticket_version、session_id、订单原始引用、工单类型、业务时间和资料版本。10 个 `StructuredTool` 绑定这份上下文，模型输入 schema 不暴露客户身份、业务时间或依赖对象。工具只接受当前工单订单号，repository 再检查客户归属。P03 已在 Agent 调用入口创建并传递这份会话，session_id 与 run_id 对应。具体契约与测试见 [ADR 002](decisions/002-trusted-tools-and-evidence.md)。工具 schema 使用 LangChain 官方支持的 Pydantic 输入模型，异步结果通过 `ToolMessage` 与 call ID 关联。[LangChain Tools](https://docs.langchain.com/oss/python/langchain/tools)

证据是包含来源类型、来源 ID、来源版本、业务与观察时间、工单/会话范围及事实 JSON 的不可变快照。ID 由上述内容生成，重复读同一快照去重；内容变化生成新 ID。集合使用资料集版本，条目自身版本仍保留在事实里；当前 assessment 使用 `rules-v2` 并保存输入引用，历史阶段快照保留 `rules-v1`。P02 演示的观察时钟与固定业务时钟一致，证据只保存在会话内存；`inspect --json` 可导出，但没有重新导入接口。P06 将随 run 持久化，写操作前须重新核验资料版本。

`get_policy` 可读取指定历史 / 未来版本；`evaluate_policy` 的引用必须适用于当前工单意图、商品、动作和业务时间，混入不适用政策返回 INVALID_ARGUMENT，不转化为客户不符合条件。非退款评估不允许金额参数。工具输入错误与业务 ineligible 分开处理。

P03 将证据快照随静态运行报告保存；读取报告不重新注册为可信工具输入。当前候选复算使用本轮已取得的事实，不能替代执行前读取最新订单/政策、重新计算时间窗口与校验批准版本。

默认单次结果限制为 12000 UTF-8 字节，超长返回 `RESULT_TOO_LARGE`，不截断 JSON、不注册未返回的证据。只读查询采用 3 秒等待时限、每会话 2 个工作槽，无任务积压队列；超时返回 `TOOL_TIMEOUT`，未结束的后台任务继续占用槽，满时返回 `TOOL_BUSY`。Python 线程不能强行终止，因此实际外部适配器也必须配置有限 I/O 时限；迟到结果不进入证据存储。全局调度、重试和运行统计在 P07 完善。

### 7.2 虚构政策与确定性规则

- 退货窗口：`request_time <= received_at + 7 * 24h`，恰好边界可申请；缺失收货时间则结果为 unknown。
- 指定模拟商品类别且未拆封才满足示例无理由退货条件；不满足返回具体条件失败。
- “签收未收到”须先调查；只有模拟物流调查明确确认丢件，才进入退款资格评估。
- 退款金额必须大于 0，且不超过 `paid_cents - refunded_cents`，由代码计算上限。
- 已有有效退货申请时返回已有记录，避免重复登记。
- 存在多份同时有效但互相冲突的政策时返回人工接手，不由模型随意选择。

条件结果使用 `true / false / unknown`，未知不能按 false 自动拒绝，也不能按 true 自动批准。政策判断携带版本和事实引用，供执行前再次校验。

P02 报告的 `disposition` 为 `eligible`、`ineligible`、`needs_information`、`existing_application`、`policy_conflict` 或 `no_policy`。即便另一个条件已知失败，只要存在未确定条件，调查报告仍为 `needs_information`，保留所有条件值。冲突检测会重新检索同动作的适用政策，省略引用不能绕过冲突或获得资格。`eligible` 仅表示候选条件满足，所有结果均为 `candidate_only`。

### 7.3 写操作

`open_logistics_case`、`create_return_request`、`issue_mock_refund` 由动作服务执行。第一版不暴露给专员的工具集；通过图中的确定性执行节点调用。上述模拟写动作均在展示具体方案后由演示操作员确认，客户补充回答不等同于操作员确认。

## 8. 人工介入、持久化与幂等

### 8.1 暂停与恢复

缺资料和确认动作采用不同 `PendingInput.kind`。暂停载荷包括 pending ID、问题或动作明细、输入版本和方案版本。应用服务校验响应者角色、pending ID、版本及是否已处理，再调用 `Command(resume=...)`。

每个 run 有唯一且稳定的 `thread_id`；暂停恢复继续同一 thread，主动重新调查创建新 run。预算在恢复后累计，等待用户的时间不计入活动执行时长。[S5]

`interrupt()` 恢复会从所在节点开头重新执行，因此：

- 人工等待节点只负责构造载荷和接收决定；写动作放到后续执行节点。
- 暂停前创建 pending 记录也必须有稳定 ID 和唯一约束。
- 修改动作参数产生新方案版本，重新校验并再次展示；旧批准不沿用。
- 新政策或订单版本变化导致旧方案失效，重新调查或再次确认。[S6]

### 8.2 数据库职责

`business.sqlite`：customers、orders、order_items、tracking_events、delivery_proofs、policies、tickets、ticket_messages、runs、pending_inputs、action_ledger、refunds、return_requests、logistics_cases、run_events、request_idempotency、schema_migrations。

`checkpoints.sqlite`：由框架管理图检查点，不手工修改内部表。两库分别持久化，不能假设业务提交和图检查点能原子提交。

业务库启用外键、合适的 busy timeout 和明确事务。演示服务只运行一个进程；同工单同一时刻允许一个活跃执行器。用数据库条件更新占用 run，避免两个 HTTP 请求同时启动；内存锁只作辅助。

### 8.3 动作幂等

1. 应用程序为逻辑动作生成稳定 `action_id / operation_key`，并保存 payload hash。
2. 已存在同 key、同 payload 的成功结果，返回原结果；同 key 不同 payload 返回冲突。
3. 首次执行再次检查身份、批准版本、最新订单与政策、退款余额或已有申请；时间条件用执行时的注入时钟重新计算，原 `as_of_time` 只保留为历史决策依据。
4. 在同一个业务数据库事务中写动作账本、模拟业务记录、金额更新和动作事件。
5. 图检查点稍后保存；若此间崩溃，恢复时查动作账本获得已有结果。

模拟系统借助同库事务和唯一约束保证同一动作只产生一次业务变更。未来接真实外部服务时，需要对方幂等键或查询对账机制；本地检查点本身不能保证外部副作用只执行一次。

### 8.4 启动恢复与取消

- 重新启动加载 queued run；原 running run 标记为 interrupted，提供恢复入口。
- paused run 显示对应待办，可补充信息或确认继续。
- 已经完成的动作以账本为准，不因旧图状态再次执行。
- schema / workflow 版本不兼容时保留数据并报告版本冲突，先做明确迁移。
- 取消在节点边界生效；已经提交的模拟动作保留真实状态，取消不表示撤销。

## 9. 错误、预算和可观察性

### 9.1 初始可配置预算

| 限制 | 初始值 | 处理方式 |
| --- | --- | --- |
| 同 run 模型调用 | 20 次，包含重试和结构化修复 | P03 调用前占用预算，耗尽为诊断失败；后续接人工介入 |
| 同 run 工具调用 | 30 次，包含重试和代码规则复算 | P03 调用前检查，记录停止原因 |
| proposal schema 修复 | 1 次，可配置 0～3 | P03 有限反馈，与审核返工分别计数 |
| 审核返工 | 2 次 | 保存最后建议和未解决问题 |
| 并发专员 | 2 个 | 限制模型并发 |
| 单模型请求超时 | 30 秒 | P03 到时停止并保存失败；后续对暂时性错误有限重试 |
| 单工具调用超时 | 3 秒 | P02 已实现，标记资料未取得，不能伪造空结果 |
| 活动执行时长 | 120 秒 | 在可安全停止的边界终止 |
| 单 Agent 输入 | 估算最多 6,000 token | 保留必要事实、摘要和引用 |
| 单次输出 | 最多 1,200 token，按 provider 能力配置 | 避免无界输出 |
| 全 run token 预算 | 初始 50,000，按实际 provider 校准 | 使用估算预留和实际 usage 对账 |

这些是初始工程参数，不是已经验证的性能承诺。模型和工具调用次数是硬上限；token 预算采用估算预留和 usage 对账，存在误差，属于软上限，不能宣称严格限制实际计费 token。无法获得实际 token usage 时标记 unknown / estimated，不能填 0。费用基于用户配置的价格快照计算，无价格配置则不显示货币估算。

当前执行 P02 查询容量/超时、P03 全局调用/schema 修复/模型时限和 P05 共享审核返工上限；全局并发、活动时长、token 预留/对账等在后续阶段实现。P03 保存模型提供的 usage，未报告时为 null / not_reported，不生成金额估计。recursion_limit 只作框架兜底。

并发预算由统一控制器原子预留，恢复时从调用账本校正累计值。`recursion_limit` 作为兜底，不能代替业务预算。provider 的隐式重试需要关闭或纳入明确记录。

### 9.2 错误分类

- 暂时性错误：超时、限流、服务暂不可用；允许有限退避重试，默认最多额外 2 次并受全局预算约束。
- 输入错误：缺订单号或资料，等待用户补充。
- 业务错误：无权访问、规则不满足、订单状态不允许；不重试相同请求。
- 模型结果错误：schema 无效、未知路由、无效证据引用；有限修复或人工接手。
- 程序错误：保留失败 run 和错误位置，禁止伪装业务成功。

### 9.3 事件

统一记录：run/ticket ID、序号、Agent、节点、task ID、revision、event_type、简短决策依据、工具名称、结果摘要、耗时、usage、error_code 和版本信息。

事件覆盖 run_started、node_started/completed、model_called、tool_called、evidence_added、review_finished、input_requested、input_received、action_committed、run_finished/failed。

早期就加入最小事件记录，后续再展示时间线。前端只接收经过筛选的事件 DTO；不直接透出整个图状态、模型密钥、完整联系方式或内部消息。决策依据记录可读摘要，不要求保存模型隐藏推理。

P03 已保存本地最小事件序号、角色、模型/业务工具开始与结束、结果、耗时、schema 修复、代码复算及停止原因；JSON 中同时保留模型输出和工具消息，供学习回看。ResolutionProposal 是框架的结构化输出工具，不算业务工具调用；输出它的模型调用计入模型预算。完整节点/返工/人工输入/动作事件和持久事件表在后续阶段补齐。

P04 增加父图节点开始/结束/失败、角色开始/结束和角色调用统计；消息日志标注 agent_role，日志汇总不意味着消息被传给其他专员。JSON 的 graph_state / node_trace 表示最后完成的节点更新；失败节点另在事件和 error.node 中标记。IntakeResult、OrderInvestigation、PolicyAssessment 同样属于结构化输出合成工具，不算业务工具调用。

## 10. API 与界面

### 10.1 API 草案

| 方法与路径 | 行为 |
| --- | --- |
| `GET /health` | 进程和数据库检查，不调用付费模型 |
| `GET /tickets` | 查询当前演示身份范围内工单 |
| `POST /tickets` | 创建工单，返回 201 |
| `GET /tickets/{ticket_id}` | 工单详情、业务状态、最新 run 和待办 |
| `POST /tickets/{ticket_id}/runs` | 创建 queued run，返回 202；要求 Idempotency-Key |
| `GET /runs/{run_id}` | run 状态和结果 |
| `GET /runs/{run_id}/events?after_seq=N` | 有序增量事件，首版前端轮询 |
| `POST /runs/{run_id}/responses` | 回答 pending input，校验 ID 和版本后恢复；要求 Idempotency-Key |
| `POST /runs/{run_id}/resume` | 恢复 interrupted run；与 paused 回答分开 |
| `POST /runs/{run_id}/cancel` | 设置取消请求，返回当前状态 |

启动和恢复由应用级执行器处理，HTTP 请求不等待完整模型流程。首版为单进程、有上限的本地执行器；queued 状态先写入数据库，恢复行为见 8.4。

错误响应统一包含 code、message、request_id。无权访问和不存在对客户统一为不可访问；活跃 run 冲突、过期确认、同幂等键不同载荷返回 409；schema 无效返回 422。

本地演示使用固定 customer / operator 身份，通过独立演示配置注入。不能把模型输出或请求正文中的 customer_id 当作身份来源。生产鉴权是后续独立阶段。

### 10.2 界面范围

- 工单列表：类型、业务状态、最后更新时间。
- 工单详情：用户诉求、脱敏订单事实、证据、建议和回复草稿。
- 执行面板：当前角色、工具事件、审核返工、调用与耗时统计。
- 待办区域：用户补充表单、操作员确认表单，显示方案版本。
- 运行操作：开始、恢复、取消；重复点击使用同一请求键。

演示页面可切换预设身份，界面说明这是本地演示。首版用事件轮询；需要降低延迟时再增加 SSE。

## 11. 测试与评估

### 11.1 测试分层

| 层级 | 验证内容 | 模型方式 |
| --- | --- | --- |
| 单元 | 金额、时间、三值条件、引用、合并、身份范围 | 无模型 |
| 工具集成 | 真实临时 SQLite、只读工具、错误分类 | 无模型 |
| Agent 契约 | 实际工具调用循环、schema 修复和预算计数 | ScriptedChatModel |
| 图集成 | 路由、返工、暂停恢复、上下文隔离 | ScriptedChatModel |
| 恢复集成 | 新进程恢复、崩溃窗口、动作幂等 | 脚本模型＋故障注入 |
| API / E2E | HTTP 行为、重复请求、页面待办和事件 | 脚本模型 |
| live smoke / eval | provider 兼容性、语义质量和协作收益 | 显式开启的真实模型 |

`ScriptedChatModel` 实现兼容 LangChain 的消息、工具调用 ID 和结构化返回；脚本描述模型行为，预期业务结果在独立 fixture 中人工编写。集成测试运行真实图和真实工具，不直接 mock 掉整张图。[S7]

离线测试默认禁止网络并使用临时数据库。live 测试需要显式开关和模型配置；没有凭据时明确 skipped。低 temperature 不能保证确定性，不以回复全文或真实模型固定工具顺序作为断言。

### 11.2 样例集

准备 30 个版本化案例，20 个开发案例、10 个留出案例。三类正常工单，加上缺信息、政策矛盾、不可访问订单、工具异常、无效引用、过期确认、重复提交和恢复案例。评估 gold 记录合法结果集合、必要证据、禁止动作和允许待办，不强制唯一自然语言答案。

P01 已保存 `demo-v1` 业务资料、`cases-v1` 输入和 `gold-v1` 独立预期。业务 JSON 放在 `src/after_sales/data/` 随包分发；开发输入在 `fixtures/`，留出请求与 gold 在 `evals/`。留出请求使用开发工单以外的订单，P10 在临时库创建评估工单。当前只验证资料完整性，业务行为与故障注入按后续阶段实现。

真实模型评估先输出结果，暂不因小样本波动阻塞早期阶段；P10 再按目标验收：

- 越权读取、未确认写动作、重复写动作、超额退款：0 次，属于必须通过项。
- 正常案例处理结果与 gold 的一致率目标至少 90%；报告样本数和重复次数。
- 程序验证引用存在性；人工抽查语义支持程度、政策正确性和表达质量。
- 记录工具 / 模型调用、已知或估算 token、活动时长、等待时长和费用配置版本。
- 单 Agent 和多 Agent 使用同一资料、工具服务、模型、预算、规则与确认门槛，各跑同一案例至少 3 次；分别比较质量、耗时和成本，不预设多 Agent 胜出。

## 12. 建议目录

```text
.
├── README.md
├── plan.md
├── pyproject.toml                 # P00 创建
├── uv.lock
├── .env.example
├── docs/
│   ├── technical-design.md
│   └── decisions/                 # 重要设计变更记录
├── src/after_sales/
│   ├── domain/                    # models / policy_rules / enums
│   ├── data/                      # 随安装包分发的虚构业务 JSON
│   ├── repositories/              # sqlite / migrations / seed
│   ├── tools/                     # orders / logistics / policies / evidence
│   ├── agents/                    # P03 基线；P04 roles.py；P05 review.py / 审核契约
│   ├── prompts/                   # 版本化角色提示词
│   ├── workflows/                 # P04 serial.py；P05 reviewed.py / interactive.py
│   ├── services/                  # tickets / runs / actions / approvals
│   ├── runtime/                   # budgets / events / executor / clock
│   ├── api/                       # app / routes / DTOs / demo_identity
│   ├── web/                       # HTML / CSS / JS
│   └── cli.py
├── fixtures/                      # 模拟数据和开发案例
├── evals/                         # runner / gold / holdout / reports
├── tests/                         # unit / integration / recovery / api / e2e / live
└── var/                           # 本地数据库和输出，忽略提交
```

目录按阶段创建，暂不生成空目录或预写所有框架。当前路径拼写 `muti-agent` 保留，Python 包名采用 `after_sales`。

## 13. 已确定与待确定事项

已确定：售后工单场景、LangGraph＋LangChain、先本地模拟、四种职责、规则与动作由代码兜底、分阶段实施。

默认设计：Python 3.12、uv、SQLite、FastAPI＋原生页面、中文业务交互。若后续偏好变化，在进入相关阶段前调整设计和 plan。

已确认当前暂时只用离线脚本模型，P03 不接入 provider；多 Agent 编排可继续离线实现。未来 live 接入前确定模型服务商、模型标识、凭据提供方式和实际请求预算，再验证工具调用与结构化输出能力。

## 14. 官方资料与设计依据

框架行为按 2026-10-02 查阅的官方资料核对。下面支持框架能力说明，具体业务规则和工程参数是本项目的设计选择。

- [S1 LangChain Agents / create_agent](https://docs.langchain.com/oss/python/langchain/agents)
- [S2 LangChain Subagents：上下文和职责隔离](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
- [S3 LangChain Structured output](https://docs.langchain.com/oss/python/langchain/structured-output)
- [S4 LangGraph Graph API：State、Nodes、Edges 和 reducers](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [S5 LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [S6 LangGraph Interrupts：恢复与节点重执行](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [S7 LangChain Test](https://docs.langchain.com/oss/python/langchain/test)
- [LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [LangChain Tools](https://docs.langchain.com/oss/python/langchain/tools)
- [LangChain Models](https://docs.langchain.com/oss/python/langchain/models)
- [LangChain Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- [FastAPI Testing](https://fastapi.tiangolo.com/tutorial/testing/)
- [pytest markers](https://docs.pytest.org/en/stable/how-to/mark.html)
