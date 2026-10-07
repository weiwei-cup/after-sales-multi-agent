# P10 离线对照评估

模型：ScriptedChatModel；同一业务资料、判断时间、规则、审批和动作服务。单 Agent 使用代码审核，多 Agent 另有模型审核；共享确定性审核下限。

所有运行输出保存后才读取 gold。暂停等待客户属于合法停止；操作员确认由评估驱动器模拟，未接入真实支付。

| 数据集 | 架构 | 案例 | 尝试 | 通过 | 比例 | 模型调用均值 | 工具调用均值 | 活动耗时 P50 ms | 关键错误 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dev | multi | 20 | 60 | 51 | 85.0% | 14.6 | 10.5 | 503.977 | 0 |
| dev | single | 20 | 60 | 54 | 90.0% | 9.6 | 10.2 | 290.022 | 0 |
| holdout | multi | 10 | 30 | 27 | 90.0% | 14.6 | 9.8 | 399.607 | 0 |
| holdout | single | 10 | 30 | 27 | 90.0% | 7.8 | 7.9 | 205.851 | 0 |

## 失败与覆盖缺口

| Case | 架构 | 重复 | 实际结果 | 未通过检查 |
| --- | --- | ---: | --- | --- |
| C11 | single | 1 | handoff_budget_exhausted | allowed_outcome |
| C11 | multi | 1 | handoff_budget_exhausted | allowed_outcome |
| C11 | multi | 2 | handoff_budget_exhausted | allowed_outcome |
| C11 | single | 2 | handoff_budget_exhausted | allowed_outcome |
| C11 | single | 3 | handoff_budget_exhausted | allowed_outcome |
| C11 | multi | 3 | handoff_budget_exhausted | allowed_outcome |
| C14 | single | 1 | handoff_budget_exhausted | allowed_outcome |
| C14 | multi | 1 | handoff_budget_exhausted | allowed_outcome |
| C14 | multi | 2 | handoff_budget_exhausted | allowed_outcome |
| C14 | single | 2 | handoff_budget_exhausted | allowed_outcome |
| C14 | single | 3 | handoff_budget_exhausted | allowed_outcome |
| C14 | multi | 3 | handoff_budget_exhausted | allowed_outcome |
| C17 | multi | 1 | handoff_budget_exhausted | required_evidence |
| C17 | multi | 2 | handoff_budget_exhausted | required_evidence |
| C17 | multi | 3 | handoff_budget_exhausted | required_evidence |
| H10 | single | 1 | propose_logistics_case | allowed_outcome, allowed_actions |
| H10 | multi | 1 | propose_logistics_case | allowed_outcome, allowed_actions |
| H10 | multi | 2 | propose_logistics_case | allowed_outcome, allowed_actions |
| H10 | single | 2 | propose_logistics_case | allowed_outcome, allowed_actions |
| H10 | single | 3 | propose_logistics_case | allowed_outcome, allowed_actions |
| H10 | multi | 3 | propose_logistics_case | allowed_outcome, allowed_actions |

## 解释范围

- 脚本模型输出可重复，次数和耗时反映当前编排的工程开销。不能证明真实模型语言理解、鲁棒性或多 Agent 的普遍优势。
- 单/多 Agent 交替先后执行；P50/P95 来自本次机器的样本，不是速度保证。活动耗时排除人工等待；wall 含准备、协议、恢复和可选浏览器。
- token 未报告时总量与费用为 null；known_total_tokens 只表示已报告部分。价格仅接受用户配置快照，不在线猜测收费。
- C17 两种架构均用两次模型额度；多 Agent 的 intake 后仅一个额度供两分支竞争。单 Agent 依真实调用顺序耗尽同一总额度。
- 未启用 --browser 时 C20 刷新未验证，scenario_exercised=false，计入未通过，不冒充浏览器覆盖。
- 真人证据标注、真实模型每例至少三次及 ≥90% 目标均待执行；模型裁判未使用。
