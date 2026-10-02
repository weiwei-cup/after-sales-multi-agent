# 售后工单多 Agent 工作台

通过物流延迟、签收未收到、退货申请三个业务场景，逐步学习 LangGraph＋LangChain 的工具调用、Agent 分工、审核返工、人工介入、持久恢复和并行协作。

当前正在交付 **第 001 轮 / P01：业务模型与模拟资料**。P00 已完成；本轮已实现 30 条订单、20 个工单、版本化政策和查询 CLI，本地 60 个离线测试通过。

- [技术方案](docs/technical-design.md)：业务范围、系统架构、Agent 职责、状态与数据、工具规则、恢复与幂等、API 和评估。
- [实施计划](plan.md)：P00～P10 的任务、测试、演示和验收条件，后续逐阶段更新。
- [第 001 轮学习记录](docs/rounds/001.md)：实现步骤、验证方法和本轮要理解的概念。
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
```

默认业务时间固定为 2026-10-02 12:00（Asia/Shanghai），JSON 和数据库统一 UTC。P01 查询返回业务资料及政策候选，处理建议与条件计算在 P02 / P03 加入。

`seed` 自动迁移数据库；可用 `after-sales db migrate` 单独创建 schema。重复 seed 保留已经变化的工单状态。显式 `after-sales seed --dataset demo --reset` 恢复配置指向的演示业务库，要求本应用与 demo 标记且只有已知表；检查点库不受此命令处理。

若本机的 uv 安装在本项目的 `.tools/uv` 隔离目录，可将以上 `uv` 换成 `./scripts/dev`。该脚本优先使用 PATH 中的 uv，再使用项目内工具，并将默认缓存放在忽略目录 `.tools/uv-cache`。

## 验证

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

默认测试禁止 socket 网络访问，并排除 `live` 与 `e2e`。P00 验证配置错误和密钥隐藏、CLI 启动、最小 StateGraph 执行，以及关闭连接后从 SQLite 读取检查点。真实 Agent 工具调用循环在 P03 实现。

P01 新增金额与时间校验、归属与外键、资料引用、重复初始化、失败回滚、受限重置、政策版本、CLI 及开发 / 留出案例结构测试。业务评估 runner 在 P10 实现。

## 按阶段学习与保存

建议先阅读技术方案第 1～5 节，再按照实施计划逐阶段推进。每阶段执行“实现 → 测试 → 更新计划 → 提交与推送 → CI 验证 → 阶段标签”。

仓库：[weiwei-cup/after-sales-multi-agent](https://github.com/weiwei-cup/after-sales-multi-agent)。每个完成阶段保留 `phase-p00`～`phase-p10` 标签，可在独立目录检出对应标签学习。

只上传代码、模拟资料和文档。运行时数据库、`.env`、密钥和虚拟环境由 `.gitignore` 排除。
