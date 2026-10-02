# 售后工单多 Agent 工作台

通过物流延迟、签收未收到、退货申请三个业务场景，逐步学习 LangGraph＋LangChain 的工具调用、Agent 分工、审核返工、人工介入、持久恢复和并行协作。

当前已完成 **第 004 轮 / P04：多 Agent 串行主图**，代码与学习记录已上传 GitHub。客服协调、订单物流、售后政策三个角色通过 LangGraph 串行交接，共用代码校验和调用预算；本地 250 个离线测试与 [GitHub CI](https://github.com/weiwei-cup/after-sales-multi-agent/actions/runs/37021920152) 均通过，学习快照为 [phase-p04](https://github.com/weiwei-cup/after-sales-multi-agent/tree/phase-p04)。单 Agent 基线继续保留，下一轮为 P05：审核、返工和人工介入。

- [技术方案](docs/technical-design.md)：业务范围、系统架构、Agent 职责、状态与数据、工具规则、恢复与幂等、API 和评估。
- [实施计划](plan.md)：P00～P10 的任务、测试、演示和验收条件，后续逐阶段更新。
- [第 001 轮学习记录](docs/rounds/001.md)：实现步骤、验证方法和本轮要理解的概念。
- [第 002 轮学习记录](docs/rounds/002.md)：工具输入、可信上下文、规则计算、证据与错误边界。
- [第 003 轮学习记录](docs/rounds/003.md)：实际工具循环、结构化建议、有限修复、代码复算和运行记录。
- [第 004 轮学习记录](docs/rounds/004.md)：主图状态、有限路由、专员工具与上下文隔离、结构化交接。
- [模拟资料与案例](fixtures/README.md)：数据来源、20 个开发案例与 10 个留出案例。

第一版使用本地模拟订单、物流与虚构售后政策，输出建议与回复草稿；后续加入人工确认后的模拟业务动作。用户选择当前仅使用离线脚本模型；真实模型适配器和网络 smoke test 暂缓。

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

`eligible` 表示代码条件满足，输出始终为 `candidate_only`，不登记退货或执行退款。P02 证据保存在本次调查会话内存中，`--json` 可导出快照；跨进程保存与恢复在 P06 实现。默认工具查询时限 3 秒，单次工具结果上限 12000 UTF-8 字节，均可通过 `.env.example` 中的配置修改。

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

可用 `run --output 新文件路径 --json` 指定结果文件，已有文件不会被覆盖。文件包含工单输入、框架/规则/资料版本、原始建议、通过校验的建议、证据、模型与工具消息、事件和调用统计。`completed` 表示调查与建议生成结束；`candidate_only=true`、`executable=false`，执行动作数为 0。这是可读取的静态运行记录，恢复图状态和人工确认在 P05 / P06 实现。

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

本轮离线协调按工单声明的类型和订单引用整理槽位/调查计划，不评价自由文本意图识别。专员摘要必须匹配实际工具结果与快照，不能凭客服文字猜收货时间。全运行共用 20/30 次调用上限和默认 1 次 schema 修复，角色切换不重置预算；P05 再引入共享审核返工计数。主图和三类实测轨迹分别保存在 [Mermaid](docs/graphs/p04-serial.mmd) 与 [轨迹 JSON](docs/graphs/p04-demo-traces.json)。

`seed` 自动迁移数据库；可用 `after-sales db migrate` 单独创建 schema。重复 seed 保留已经变化的工单状态。显式 `after-sales seed --dataset demo --reset` 恢复配置指向的演示业务库，要求本应用与 demo 标记且只有已知表；检查点库不受此命令处理。

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

建议先阅读技术方案第 1～5 节，再按照实施计划逐阶段推进。每阶段执行“实现 → 测试 → 更新计划 → 提交与推送 → CI 验证 → 阶段标签”。

仓库：[weiwei-cup/after-sales-multi-agent](https://github.com/weiwei-cup/after-sales-multi-agent)。每个完成阶段保留 `phase-p00`～`phase-p10` 标签，可在独立目录检出对应标签学习。

只上传代码、模拟资料和文档。运行时数据库、`.env`、密钥和虚拟环境由 `.gitignore` 排除。
