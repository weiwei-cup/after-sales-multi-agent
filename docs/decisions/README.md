# 设计决策索引

决策文档保留做出选择时的背景与范围，部分“后续”内容已经在后来的决策中实现。当前行为以 [系统架构](../technical-design.md) 为准。

| 决策 | 核心问题 |
| --- | --- |
| [001 · 用户引用与可信订单关联](001-fixture-and-order-references.md) | 订单号是输入还是已核验关联？ |
| [002 · 可信工具与证据](002-trusted-tools-and-evidence.md) | 模型可以指定哪些参数，事实如何验证？ |
| [003 · 离线工具循环](003-offline-agent-baseline.md) | 如何在无密钥条件下复现真实框架调用？ |
| [004 · 结构化角色交接](004-serial-role-handoffs.md) | 如何控制上下文、权限和交接边界？ |
| [005 · 有限审核与人工输入](005-bounded-review-and-human-input.md) | 补查、修订和人工确认如何有界？ |
| [006 · 持久输入与事务动作账本](006-durable-review-and-action-ledger.md) | 如何跨进程恢复并避免重复业务效果？ |
| [007 · 并行汇合与持久预算](007-parallel-joins-and-durable-budgets.md) | 如何合并分支并原子争用预算？ |
| [008 · HTTP 接纳与本地队列](008-durable-http-admission.md) | 接纳、回答和排队之间如何防止丢任务？ |
| [009 · 工作台与公开结果投影](009-browser-workbench-and-public-evidence.md) | 页面能展示什么，刷新/重试如何处理？ |
| [010 · 独立评分与公平基线](010-independent-offline-comparison.md) | 如何避免基础设施差异与预期泄漏？ |
