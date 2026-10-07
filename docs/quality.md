# 质量与评估

测试验证工程行为，业务评估衡量案例预期，两类结果分别报告。所有数据均来自本地模拟业务与 ScriptedChatModel。

## 自动化验证

当前发布验证 **556 项默认离线测试、12 项真实 Chromium 测试**。默认套件禁止互联网 socket；恢复、并发和预算使用真实 SQLite，关键退出窗口使用独立进程。浏览器套件另允许 loopback，并拒绝浏览器外部请求。

| 验证范围 | 代表性代码 |
| --- | --- |
| 金额/时间/政策边界、未知条件和归属 | [规则测试](../tests/test_policy_rules.py)、[工具测试](../tests/test_readonly_tools.py) |
| 角色上下文、工具权限、引用与结构化交接 | [多 Agent 测试](../tests/test_multi_agent.py)、[审核测试](../tests/test_reviewed_workflow.py) |
| 事务、动作重放、真实退出、版本化确认 | [持久工作流](../tests/test_durable_workflow.py)、[恢复](../tests/test_parallel_recovery.py) |
| 额度竞争、有限重试、并发、取消 | [预算](../tests/test_parallel_budgets.py)、[取消](../tests/test_parallel_cancel.py) |
| HTTP 请求幂等、身份范围、队列与启动恢复 | [API](../tests/test_api.py)、[API 恢复](../tests/test_api_recovery.py) |
| 客户补资料、金额修订、旧审批、刷新/游标重连 | [浏览器测试](../tests/test_workbench_e2e.py) |
| 演示隔离与持久重启 | [演示入口测试](../tests/test_demo.py) |
| 观察完整性、独立评分与真实账本安全 | [评估器测试](../tests/test_evaluation.py) |

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/check_docs.py
uv run --locked pytest
```

浏览器测试需要 Chromium。Linux 首次安装可使用 `playwright install --with-deps chromium`。

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers"
uv run --locked playwright install chromium
uv run --locked pytest -m e2e --allow-hosts=127.0.0.1 tests/test_workbench_e2e.py
```

[CI](https://github.com/weiwei-cup/after-sales-multi-agent/actions/workflows/ci.yml) 在 Linux 上重新安装锁定依赖，执行离线/浏览器回归、完整对照评估、兼容 CLI/HTTP 路径及 wheel 构建。浏览器与评估原始资料按工作流配置保留 7 天。

## 独立业务评估

归档 [完整报告](../evals/reports/p10-offline-v1/report.md) 使用 20 个开发案例、10 个留出案例，两种架构各重复 3 次，共 180 次观察。每次使用新数据库、相同规则/审批/预算和固定案例时间。运行端不读取 gold；全部观察保存并校验后，独立评分器才读取预期。

| 数据集 | 架构 | 通过 / 尝试 | 平均模型调用 | 平均工具调用 |
| --- | --- | --- | --- | --- |
| 开发集 | 单 Agent | 54/60（90%） | 9.6 | 10.2 |
| 开发集 | 多 Agent | 51/60（85%） | 14.6 | 10.5 |
| 留出集 | 单 Agent | 27/30（90%） | 7.8 | 7.9 |
| 留出集 | 多 Agent | 27/30（90%） | 14.6 | 9.8 |

实际跨客户读取、未确认业务写入、重复业务动作和超额退款均为 0。安全判定基于真实账本、审批绑定、余额和历史增量，而非仅依赖建议文本。安全观察不完整时单独标识，不能计为“零错误”。

真实故障包含工具超时、无效引用、审核持续拒绝、批准后事实变化、提交后进程退出、调用额度争夺、HTTP 重复请求和浏览器非零游标重连。完整执行方式见 [评估说明](../evals/README.md)。

## 结果的适用范围

30 个案例的三次重复用于观察离线流程稳定性，不能当作 180 个独立业务样本，也不能证明真实模型质量。单 Agent 使用代码协调/审核，多 Agent 多出模型角色与审核；这是应用架构对照，不是只改变并发的实验。

脚本模型未报告实际 token usage，token 与费用保留 `null`；已知小计为 0 不表示零用量或免费服务。报告中的耗时是本机脚本流程开销，不代表真实提供商延迟。没有真实业务吞吐量、客户规模、费用收益或线上 SLA 指标。

引用抽查已经保存 6 个案例的证据 ID、版本和核对依据，reviewer 为 Codex；真人语义复核仍待完成。真实模型适配与重复评估尚未执行。

## 已知业务问题

21 次评分失败来自以下 4 组；所有尝试与证据原样归档，没有删除失败或根据留出结果调整本次报告。

| 案例 | 当前行为 | 与预期的差异 |
| --- | --- | --- |
| C11 | 持续审核拒绝后有限返工，最终调用额度耗尽并转人工 | 停止原因与期望的返工上限停止不同 |
| C14 | 凭证持续超时，有限重试/补查后调用额度耗尽 | 缺资料与停止分类不符合预期 |
| C17 · 多 Agent | 两次模型调用额度下受限停止 | 缺少预期要求的订单证据 |
| H10 | 已全额退款后转为物流调查候选 | 预期应识别已退款并停止；回复措辞与丢件事实不一致 |

这些失败影响业务完整性，未观察到四类关键安全违规。后续优化应新增回归案例，并使用新的留出集重新验证。[路线图](roadmap.md)。

## 归档与重评分

原始观察、截图与执行矩阵见 [observations.tar.gz](../evals/reports/p10-offline-v1/observations.tar.gz)，完整性记录见 [archive-hashes.json](../evals/reports/p10-offline-v1/archive-hashes.json)。[source-hashes.json](../evals/reports/p10-offline-v1/source-hashes.json) 记录当时运行的源码与页面，报告保留当时工作树来源；当前文档/界面整理不会覆盖这份历史观察。

重新评分无需运行 Agent；命令见 [评估说明](../evals/README.md)。哈希检查证明本地归档一致性，不构成外部签名或独立认证。
