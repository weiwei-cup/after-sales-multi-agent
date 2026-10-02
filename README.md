# 售后工单多 Agent 工作台

通过物流延迟、签收未收到、退货申请三个业务场景，逐步学习 LangGraph＋LangChain 的工具调用、Agent 分工、审核返工、人工介入、持久恢复和并行协作。

当前 **第 006 轮 / P06：持久恢复与模拟动作**已完成并上传 GitHub；本地 **429 个离线测试**与 [实现 CI](https://github.com/weiwei-cup/after-sales-multi-agent/actions/runs/37041984137) 通过，达到 **M2 多 Agent MVP**。阶段交付标签为 [phase-p06](https://github.com/weiwei-cup/after-sales-multi-agent/tree/phase-p06)。四个 Agent 的审核流程可跨进程恢复，人工输入、证据与预算持久保存，物流调查、退货登记和模拟退款通过事务动作账本执行。保留 single、serial 和 P05 reviewed 入口；下一轮为 P07：有界并行与预算完善。

000～004 复核已完成。[复核记录](docs/reviews/000-004.md) 列出要求覆盖、3 类已修复问题及延期边界。当前代码使用 `rules-v2`，复核时的 285 项回归与 [补修 CI](https://github.com/weiwei-cup/after-sales-multi-agent/actions/runs/37025757749) 均通过；P05 在该版本上继续。原有阶段标签保留历史实现。

- [技术方案](docs/technical-design.md)：业务范围、系统架构、Agent 职责、状态与数据、工具规则、恢复与幂等、API 和评估。
- [实施计划](plan.md)：P00～P10 的任务、测试、演示和验收条件，后续逐阶段更新。
- [第 001 轮学习记录](docs/rounds/001.md)：实现步骤、验证方法和本轮要理解的概念。
- [第 002 轮学习记录](docs/rounds/002.md)：工具输入、可信上下文、规则计算、证据与错误边界。
- [第 003 轮学习记录](docs/rounds/003.md)：实际工具循环、结构化建议、有限修复、代码复算和运行记录。
- [第 004 轮学习记录](docs/rounds/004.md)：主图状态、有限路由、专员工具与上下文隔离、结构化交接。
- [第 005 轮学习记录](docs/rounds/005.md)：审核反馈、定向返工、interrupt、版本绑定和人工输入。
- [第 006 轮学习记录](docs/rounds/006.md)：跨进程恢复、事务边界、动作幂等、执行前复核与业务状态。
- [模拟资料与案例](fixtures/README.md)：数据来源、20 个开发案例与 10 个留出案例。

第一版使用本地模拟订单、物流与虚构售后政策，输出建议与回复草稿；durable 入口会在人工确认后执行模拟业务动作。用户选择当前仅使用离线脚本模型；真实模型适配器和网络 smoke test 暂缓。

## 本地运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在项目根目录执行：

```bash
uv sync --locked --python 3.12
uv run --locked after-sales doctor
uv run --locked after-sales doctor --json
```

默认不需要密钥，也不会发送模型请求。可将 `.env.example` 复制为 `.env` 修改配置；环境变量以 `AFTER_SALES_` 开头。`doctor` 检查依赖和配置，不创建业务数据库，也不验证模型连通性。

初始化并查询售后业务资料：

```bash
uv run --locked after-sales seed --dataset demo
uv run --locked after-sales ticket list
uv run --locked after-sales ticket show T-DELAY-001
uv run --locked after-sales ticket show T-NOTRECEIVED-001
uv run --locked after-sales ticket show T-RETURN-001
uv run --locked after-sales order show ORD-005 --customer CUST-B --json
uv run --locked after-sales policy list --intent return_request --json
uv run --locked after-sales policy show RETURN-STANDARD --version 2 --json
uv run --locked after-sales inspect --ticket T-RETURN-001
uv run --locked after-sales inspect --ticket T-CONFLICT-001 --json
uv run --locked after-sales inspect --ticket T-CROSS-001 --json
```

默认业务时间固定为 2026-10-02 12:00（Asia/Shanghai），JSON 和数据库统一 UTC。`inspect` 使用与后续 Agent 相同的工具服务，返回事实、条件、候选资格和证据；模型调用为 0。已有申请返回 `existing_application`，资料不全返回 `needs_information`，政策冲突返回 `policy_conflict`，跨客户查询返回 `NOT_OWNED`。

`eligible` 表示代码条件满足，输出始终为 `candidate_only`，不登记退货或执行退款。P02 证据保存在本次调查会话内存中，`--json` 可导出快照；durable 运行会随 run 持久保存并恢复证据。默认工具查询时限 3 秒，单次工具结果上限 12000 UTF-8 字节，均可通过 `.env.example` 中的配置修改。

运行单 Agent 基线：

```bash
uv run --locked after-sales run --ticket T-DELAY-001 --architecture single --model scripted
uv run --locked after-sales run --ticket T-NOTRECEIVED-002 --model scripted
uv run --locked after-sales run --ticket T-RETURN-001 --model scripted
uv run --locked after-sales run --ticket T-MISSING-001 --model scripted
uv run --locked after-sales live-smoke --json
```

每次运行保存到 `var/runs/运行ID.json`，终端打印具体路径。把路径代入以下命令回看结果或事件：

```bash
uv run --locked after-sales report show var/runs/运行ID.json
uv run --locked after-sales report show var/runs/运行ID.json --events-only
```

可用 `run --output 新文件路径 --json` 指定结果文件，已有文件不会被覆盖。文件包含工单输入、框架/规则/资料版本、原始建议、通过校验的建议、证据、模型与工具消息、事件和调用统计。`completed` 表示调查与建议生成结束；`candidate_only=true`、`executable=false`，执行动作数为 0。这是可读取的静态运行记录；P05 的交互式入口支持同进程人工输入，durable 入口支持跨进程恢复。

脚本根据真实 `ToolMessage` 生成下一步调用及建议，不读取 gold；它验证框架和业务约束，不能衡量真实模型的语言理解能力。默认最多 20 次模型调用、30 次业务工具调用（含代码复算），schema 最多修复 1 次，模型请求时限 30 秒。脚本未报告 token usage 时保存 `null`，不填假 token 或费用。`live-smoke` 和显式 `run --model live` 显示 `skipped / LIVE_PROVIDER_DEFERRED`，不发网络请求；不要将保留的 live 配置当作已可用的适配器。

运行串行多 Agent：

```bash
uv run --locked after-sales run --ticket T-DELAY-001 --architecture multi --model scripted
uv run --locked after-sales run --ticket T-NOTRECEIVED-002 --architecture multi --model scripted
uv run --locked after-sales run --ticket T-RETURN-001 --architecture multi --model scripted
uv run --locked after-sales run --ticket T-MISSING-001 --architecture multi --model scripted
uv run --locked after-sales run --ticket T-CROSS-001 --architecture multi --model scripted
```

正常节点为 `intake → order → policy → draft → validate`。缺订单号、未知意图跳过专员；订单不可访问时跳过政策。每个角色只看到显式投影的输入，交接只传结构化事实/错误和证据，内部 messages 不传给其他角色。`multi-run-v1` 文件另含主图状态、节点轨迹、角色输入/输出及统计、实际 Mermaid 图，沿用 `report show` 回看；仍只生成候选，没有暂停、恢复或执行动作。

本轮离线协调按工单声明的类型和订单引用整理槽位/调查计划，不评价自由文本意图识别。专员摘要必须匹配实际工具结果与快照，不能凭客服文字猜收货时间。全运行共用 20/30 次调用上限和默认 1 次 schema 修复，角色切换不重置预算；reviewed 入口另有共享审核返工计数。主图和三类实测轨迹分别保存在 [Mermaid](docs/graphs/p04-serial.mmd) 与 [轨迹 JSON](docs/graphs/p04-demo-traces.json)。

运行带审核及人工介入的多 Agent：

```bash
uv run --locked after-sales run --ticket T-DELAY-001 --architecture multi --workflow reviewed
uv run --locked after-sales run --ticket T-MISSING-001 --architecture multi --workflow reviewed --interactive
uv run --locked after-sales run --ticket T-RETURN-001 --architecture multi --workflow reviewed --interactive
uv run --locked after-sales run --ticket T-NOTRECEIVED-002 --architecture multi --workflow reviewed --interactive
```

缺订单号样例输入 `ORD-019`，查看退货候选后选 `approve` / `reject`。退款样例可选 `revise`，输入 `9000` 分，查看新版本后再次确认。只有必要信息缺失才追问；客户回答保留为陈述，补订单号后重新核验归属。四角色的代码校验、审核和金额修改共享最多两次返工，纯措辞只改写，凭证失败只定向补查；默认调用预算跨恢复累计。

`--interactive` 保持 `InMemoryReviewRun` 和 `InMemorySaver` 存活，使用真实 interrupt / Command resume。普通 reviewed 命令遇到待办会保存 paused JSON 并退出；关闭进程后无法恢复，文件仅用于回看。人工输入绑定身份、待办、输入/方案版本和动作内容；金额改变时旧批准被拒绝。当前身份只用于本地演示，中文措辞校验采用受控模板，真实模型语义评估未实现。

review-run-v1 保存审核、共享返工计数、待办历史、确认记录及图状态，沿用 report show 查看。completed 表示建议和必要确认完成，业务执行数仍为 0。该入口保留 P05 行为；当前 durable 入口实现持久恢复、执行前最新资料与时钟复核、模拟动作。详见 [第 005 轮](docs/rounds/005.md)、[ADR 005](docs/decisions/005-bounded-review-and-human-input.md)、[实际图](docs/graphs/p05-reviewed.mmd)。

运行持久审核与模拟动作：

```bash
uv run --locked after-sales run --ticket T-NOTRECEIVED-002 --architecture multi --workflow durable --run-id learning-006
# 退出后在另一终端继续；看清动作明细后输入 approve。
uv run --locked after-sales resume --run learning-006 --model scripted
uv run --locked after-sales resume --run learning-006 --json
uv run --locked after-sales ticket show T-NOTRECEIVED-002
uv run --locked after-sales order show ORD-004 --customer CUST-A --json
```

持久恢复从 business.sqlite 和 checkpoints.sqlite 载入可信记录。每次另存静态报告；JSON 文件不能恢复图。客户补充信息和操作员回答均先持久化，再进入 Command；已保存的回答遇到崩溃会自动重放。已有活跃运行的工单必须 resume，不能另开 run 绕过待办或预算。

批准动作在同一事务中复核最新事实、有效政策及执行时钟，并写入动作账本、模拟 history、金额/状态和动作事件。同 key、同 payload 返回原回执，同 key 换金额冲突；提交后图未保存时恢复也不会重复退款。退货登记后业务状态 waiting_return，模拟退款后 resolved，物流调查登记后 processing；图均可以 completed。

P06 默认使用实际 UTC 时钟，退货资格会随 fixture 日期过去失效。测试与学习轨迹注入固定时钟；`uv run --locked python scripts/demo_p06.py` 在临时数据库和独立进程中复现三类动作、客户补订单和提交后强制退出恢复。身份仍为本地演示；当前文件锁支持 macOS/Linux。详细步骤见 [第 006 轮](docs/rounds/006.md)、[ADR 006](docs/decisions/006-durable-review-and-action-ledger.md)。

`seed` 自动迁移数据库；可用 `after-sales db migrate` 单独创建 schema。重复 seed 保留已经变化的工单状态；业务 v1 可以迁移到 v2，原数据保留。显式 `after-sales seed --dataset demo --reset` 恢复配置指向的演示业务库，要求本应用与 demo 标记且只有已知表；检查点库不受此命令处理；reset 会清除 durable 的应用资料和动作账本，旧 thread ID 不允许作为新 run 重用。

若本机的 uv 安装在本项目的 `.tools/uv` 隔离目录，可将以上 `uv` 换成 `./scripts/dev`。该脚本优先使用 PATH 中的 uv，再使用项目内工具，并将默认缓存放在忽略目录 `.tools/uv-cache`。

## 验证

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

默认测试禁止互联网 socket 访问，允许 asyncio 所需的本机 Unix socket，并排除 `live` 与 `e2e`。P00 验证配置错误和密钥隐藏、CLI 启动、最小 StateGraph 执行，以及关闭连接后从 SQLite 读取检查点。

P01 新增金额与时间校验、归属与外键、资料引用、重复初始化、失败回滚、受限重置、政策版本、CLI 及开发 / 留出案例结构测试。业务评估 runner 在 P10 实现。

P02 新增 72 个测试实例，覆盖七天整点与超一秒、未收货与收货时间缺失、部分退款余额、超额及负金额、已有申请、冲突政策遗漏、伪造事实与证据、跨客户访问、真实 LangChain `ToolMessage`、超时与有界查询、结果长度和 IPv4/IPv6 禁网。测试使用临时数据库，不改动日常演示数据。

P03 新增 69 个测试实例，完整套件 201 passed：实际 `create_agent` 循环和 call ID 关联、20 个工单的基础事实路径、改动事实后建议相应改变、缺订单号、有限 schema 修复、超余额退款与伪造引用被拦截、调用上限含复算、模型超时、查询失败、JSON 保存/读取与禁止覆盖、live 跳过。案例名含故障的工单在这 20 条测试中只验证基础事实，故障行为由单独的受控脚本/查询替换测试；尚未实现通用案例故障执行器。

P04 新增 49 个测试实例，完整套件 250 passed：三类串行路径、缺订单号/未知意图/不可访问订单的分支、实际角色工具白名单和越界调用、输入投影与独立 messages、摘要事实/引用伪造、跨角色预算与修复上限、查询错误保留、提前结束后的 schema 修复、部分状态诊断、JSON / LangGraph 序列化、单/多 Agent 契约一致、CLI 保存和回看。测试运行前后业务库 dump 一致；安装包在独立环境运行多 Agent 退货样例通过。

## 按阶段学习与保存

P05 新增 85 个测试实例，完整套件 370 passed：定向补查和事实保留、依赖政策重算、共享返工上限、硬规则覆盖模型接受、真实 interrupt/resume、输入身份/版本/内容错配、重复及并发消费、金额修改与重新展示、无业务写入、静态报告一致性、交互式 CLI 和 live 跳过。安装包在独立环境验证补订单批准/拒绝、退款改金额后批准及旧 single / serial 入口。

P06 新增 59 个测试实例，完整套件 429 passed：真实进程退出与持久恢复、事务回滚/重放、同动作重复与并发、不同金额冲突、两个工单并发退款、资料及执行时钟变化、版本拒绝与数据保留、预算延续、业务状态、CLI 和 v1→v2 迁移。日常演示数据库未用于测试。

建议先阅读技术方案第 1～5 节，再按照实施计划逐阶段推进。每阶段执行“实现 → 测试 → 更新计划 → 提交与推送 → CI 验证 → 阶段标签”。

仓库：[weiwei-cup/after-sales-multi-agent](https://github.com/weiwei-cup/after-sales-multi-agent)。每个完成阶段保留 `phase-p00`～`phase-p10` 标签，可在独立目录检出对应标签学习。

只上传代码、模拟资料和文档。运行时数据库、`.env`、密钥和虚拟环境由 `.gitignore` 排除。
