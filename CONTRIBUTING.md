# 贡献指南

先阅读 [架构](docs/technical-design.md)、[API](docs/api.md) 与 [质量报告](docs/quality.md)。提交 issue 时描述场景、复现步骤、预期/实际行为与版本；运行日志中保留请求 ID，移除个人资料、凭据与数据库内容。

## 本地开发

```bash
uv sync --locked --python 3.12
uv run --locked after-sales demo
```

开发使用新的临时资料或单独 `--data-dir`，避免把评估和日常演示数据混用。新代码放入对应分层；HTTP handler 不承载 Agent 或业务规则。

## 改动约定

- 金额使用整数分，持久时间统一 UTC；保留输入引用与已核验事实的区分。
- 新工具显式限定输入、可信身份、超时、输出长度与证据契约。
- 新写入复用确认绑定、执行前复核与动作账本，覆盖重复请求和提交窗口。
- 修改持久 schema/workflow/state/model 版本时明确迁移或拒绝策略；保留旧数据。
- 业务评估的 gold 不进入模型上下文，保留全部失败观察；留出集参与调优后用新留出集验收。
- 文档与代码一同更新，描述当前行为；历史归档只用于追溯。

## 验证与提交

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/check_docs.py
uv run --locked pytest
```

影响工作台时运行 [浏览器回归](docs/quality.md)，影响 Agent/规则/评估时执行相应业务案例。真实模型当前尚未适配，`live-smoke` 的 skipped 不能作为验证通过。

Pull request 描述具体业务变化、实现取舍与实际运行的验证；涉及预算、审批和写入时说明失败窗口。只提交源码、模拟资料和选定归档，不提交 `.env`、凭据、数据库、缓存或新评估原始输出目录。
