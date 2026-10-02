# 售后工单多 Agent 工作台

通过物流延迟、签收未收到、退货申请三个业务场景，逐步学习 LangGraph＋LangChain 的工具调用、Agent 分工、审核返工、人工介入、持久恢复和并行协作。

当前已完成 **第 002 轮 / P02：工具、规则和证据**，代码与学习记录已上传 GitHub。提供 10 个 LangChain 只读工具、三值规则、工单范围内的证据引用与调查 CLI；本地 132 个离线测试和 GitHub CI 均通过。下一轮为 P03：单 Agent 基线。

- [技术方案](docs/technical-design.md)：业务范围、系统架构、Agent 职责、状态与数据、工具规则、恢复与幂等、API 和评估。
- [实施计划](plan.md)：P00～P10 的任务、测试、演示和验收条件，后续逐阶段更新。
- [第 001 轮学习记录](docs/rounds/001.md)：实现步骤、验证方法和本轮要理解的概念。
- [第 002 轮学习记录](docs/rounds/002.md)：工具输入、可信上下文、规则计算、证据与错误边界。
- [模拟资料与案例](fixtures/README.md)：数据来源、20 个开发案例与 10 个留出案例。

第一版使用本地模拟订单、物流与虚构售后政策，输出建议与回复草稿；后续加入人工确认后的模拟业务动作。默认离线脚本模型，真实模型接入单独配置。

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

`seed` 自动迁移数据库；可用 `after-sales db migrate` 单独创建 schema。重复 seed 保留已经变化的工单状态。显式 `after-sales seed --dataset demo --reset` 恢复配置指向的演示业务库，要求本应用与 demo 标记且只有已知表；检查点库不受此命令处理。

若本机的 uv 安装在本项目的 `.tools/uv` 隔离目录，可将以上 `uv` 换成 `./scripts/dev`。该脚本优先使用 PATH 中的 uv，再使用项目内工具，并将默认缓存放在忽略目录 `.tools/uv-cache`。

## 验证

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

默认测试禁止互联网 socket 访问，允许 asyncio 所需的本机 Unix socket，并排除 `live` 与 `e2e`。P00 验证配置错误和密钥隐藏、CLI 启动、最小 StateGraph 执行，以及关闭连接后从 SQLite 读取检查点。真实 Agent 工具调用循环在 P03 实现。

P01 新增金额与时间校验、归属与外键、资料引用、重复初始化、失败回滚、受限重置、政策版本、CLI 及开发 / 留出案例结构测试。业务评估 runner 在 P10 实现。

P02 新增 72 个测试实例，覆盖七天整点与超一秒、未收货与收货时间缺失、部分退款余额、超额及负金额、已有申请、冲突政策遗漏、伪造事实与证据、跨客户访问、真实 LangChain `ToolMessage`、超时与有界查询、结果长度和 IPv4/IPv6 禁网。测试使用临时数据库，不改动日常演示数据。

## 按阶段学习与保存

建议先阅读技术方案第 1～5 节，再按照实施计划逐阶段推进。每阶段执行“实现 → 测试 → 更新计划 → 提交与推送 → CI 验证 → 阶段标签”。

仓库：[weiwei-cup/after-sales-multi-agent](https://github.com/weiwei-cup/after-sales-multi-agent)。每个完成阶段保留 `phase-p00`～`phase-p10` 标签，可在独立目录检出对应标签学习。

只上传代码、模拟资料和文档。运行时数据库、`.env`、密钥和虚拟环境由 `.gitignore` 排除。
