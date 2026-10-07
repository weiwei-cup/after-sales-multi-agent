# 技术展示提纲

建议用 5～8 分钟讲解并操作，再围绕一个事务或恢复问题深入代码。所有表述以当前实现和 [验证报告](quality.md) 为依据。

## 30 秒项目介绍

AfterSales 是一个售后工单处理工作台，覆盖物流延迟、签收未收到和退货申请。使用 LangGraph 编排协调、订单、政策与审核角色，LangChain 执行工具调用，建议绑定可追溯证据。客户补资料、操作员审批后，事务执行器复核最新事实并登记模拟业务动作；重启和请求重试可以继续处理且避免重复写入。配套单 Agent 对照、故障执行和浏览器测试，用于验证协作的成本与行为边界。

## 现场展示顺序

1. **业务闭环（2 分钟）**：按 [演示指南](demo.md) 展示签收未收到 → 补订单号 → 证据 → 批准 → 模拟退款。
2. **协作设计（1 分钟）**：查看时间线，解释独立订单调查与候选政策检索并行，政策资格计算必须等待事实。
3. **可靠执行（1 分钟）**：展示新金额重新确认、刷新仍是同一回执；说明持久回答与图检查点不是同一个事务。
4. **验证与取舍（1 分钟）**：展示开发/留出报告，解释单 Agent 开销更少、多角色不保证更快或更准，以及已保留的失败。

## 代码阅读入口

| 要解释的问题 | 实现入口 | 验证依据 |
| --- | --- | --- |
| 哪些任务可以并行，如何合并证据？ | [parallel.py](../src/after_sales/workflows/parallel.py)、[reducers.py](../src/after_sales/workflows/reducers.py) | [并行图测试](../tests/test_parallel_workflow.py) |
| 如何限制角色上下文与工具权限？ | [roles.py](../src/after_sales/agents/roles.py)、[工具契约](../src/after_sales/tools/contracts.py) | [多 Agent 测试](../tests/test_multi_agent.py) |
| 为什么模型认可后仍需代码审核？ | [规则](../src/after_sales/domain/rules.py)、[审核图](../src/after_sales/workflows/reviewed.py) | [审核测试](../tests/test_reviewed_workflow.py) |
| 为什么一次批准不能覆盖修改后的金额？ | [run_store.py](../src/after_sales/repositories/run_store.py) | [持久工作流测试](../tests/test_durable_workflow.py) |
| 如何避免提交后崩溃造成第二次退款？ | [动作事务](../src/after_sales/repositories/actions.py) | [恢复测试](../tests/test_parallel_recovery.py)、案例 C16 |
| 请求回执丢失后如何重试？ | [应用服务](../src/after_sales/services/application.py) | [API 测试](../tests/test_api.py)、[浏览器测试](../tests/test_workbench_e2e.py) |
| 多个角色如何争用同一预算？ | [budgets.py](../src/after_sales/repositories/budgets.py) | [预算测试](../tests/test_parallel_budgets.py) |
| 如何避免评估预期泄漏给 Agent？ | [runner.py](../src/after_sales/evaluation/runner.py)、[scoring.py](../src/after_sales/evaluation/scoring.py) | [评估器测试](../tests/test_evaluation.py) |

## 可用于简历的事实

- 基于 LangGraph / LangChain、FastAPI 与 SQLite 实现售后工单协作系统，覆盖三类售后流程、结构化证据、人工审批与浏览器工作台。
- 设计持久检查点、版本化待办、事务动作账本与两层幂等机制，并通过真实退出/恢复、并发及请求重放验证关键窗口。
- 建立单/多 Agent 共用规则与执行保障的独立评估，30 个案例、180 次离线观察；留出集两种架构均 27/30，四类关键安全错误均为 0。

结合本人实际参与范围描述设计、实现与验证工作。不要将模拟退款写成真实支付，把脚本调用称为真实模型推理，或把离线案例指标描述为线上效果。当前发布有 556 项离线测试和 12 项浏览器测试，业务评分失败与未实现能力可直接从文档核对。

## 常见追问

**为什么采用多 Agent？** 业务职责与数据权限能明确拆分，独立读取可并行，交接可检查；额外模型角色也带来调用与恢复复杂度。对照报告没有证明多 Agent 总体质量更高。

**为什么同时有业务库和检查点库？** 图检查点负责执行位置，业务库记录真实输入、确认和动作效果。两者有提交窗口，必须用动作幂等与持久输入重放弥合，不能依赖 JSON 或图状态推断退款已发生。

**为何不依靠提示词限制退款？** 金额、归属、政策有效性和审批需要确定性检查。模型方案是待验证输入；执行器在事务中核验最新资料和余额。

**接入真实模型/支付还要做什么？** 提供商适配与用量统计、真实采样和新留出评估；身份系统；外部接口幂等、持久投递和对账。当前本地事务保证不能直接推广到外部支付。
