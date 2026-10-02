# ADR 006：持久审核与事务动作账本

日期：2026-10-03。范围：P06，离线模拟业务。

## 业务问题

P05 的客户追问和操作员确认只能在持有 InMemoryReviewRun 的进程内继续。动作真正写入后，业务事务与图检查点之间还存在崩溃窗口：退款已经登记，图却可能再次进入批准节点。

## 决策

增加显式 `--workflow durable`，复用 P05 的职责、最低审核要求、返工限制和人工输入契约；保留 reviewed 的历史行为。图使用当前安装版本支持异步调用的 AsyncSqliteSaver。每个 run 的 ID 同时作为 thread ID；应用记录 workflow/schema/model 版本、检查点路径和启动时的调用限制。

业务 schema v2 增加 workflow_runtime、proposal_plans、pending_inputs、human_inputs、action_ledger、action_events。原 after_sales_history 表作为三类模拟业务记录，增加唯一 operation_key 外键。当前不分别创建 refunds / return_requests / logistics_cases；履约接口到后续业务扩展时再拆分。

应用运行日志保存可信证据、客户输入、消息、统计、角色结果和活动时长。模型、工具、schema 修复计数在继续运行时累计；模型和工具先保存调用占用再执行。图检查点决定从哪个节点继续，应用记录决定人工回答和业务效果是否已经提交。静态 JSON 仅用于查看，不能导入为可信恢复状态。

人工输入先在业务事务中验证并保存；客户回答同时更新输入版本和客户消息。然后向图发送 Command。如果两者之间退出，恢复时从 human_inputs 读取完全相同的 envelope，重放等待节点。确认按 pending ID 去重，客户输入版本不重复递增。

operation key 由 ticket_id、input_revision、动作类型、订单 ID 派生；payload hash 另包括金额。金额修改仍是同一业务请求，不能靠更换方案版本或 run ID 产生第二次效果。同 key、同 payload 返回原回执；同 key 不同金额产生冲突。新的业务请求必须有新的输入版本。数据库以账本主键、历史记录唯一 operation_key 和动作事件主键约束效果数量。

首次执行在 BEGIN IMMEDIATE 事务中核对持久批准与当前方案，读取当前工单、订单、商品、物流、凭证、历史及有效政策目录。来源内容变化，即使未递增版本，也使旧批准失效。规则用执行时 UTC 时钟重新计算，金额同时受订单余额约束和条件 UPDATE 保护。账本、业务记录、金额/状态和 action_committed 事件一起提交。

已提交的同 payload 回执优先于最新事实检查；自身退款导致的余额及历史变化不能让重放变成新退款。未提交且批准失效时，取消旧方案/待办，应用通过已有只读工具刷新事实与规则，再交协调起草、代码校验和审核，展示新版本。此次刷新是确定性执行前复核，不新增一个模型角色，不重置预算。

同一 run 和同一工单的执行器持有 POSIX 文件锁，进程退出后由操作系统释放；SQL 注册还拒绝已有 queued/running/paused/interrupted 工单另开运行。不同工单争用同一订单退款余额，由业务事务串行保护。当前支持 macOS/Linux 本地 CLI；Windows 锁适配和分布式租约未实现。

检查点库有应用所有权和 schema manifest；旧/未知 workflow、state、model、路径或检查点版本明确拒绝并保留数据。业务 v1 可以迁移到 v2，V1 迁移定义及原 fixture 不修改。业务 reset 清除 durable 应用记录，不清除检查点；遗留 thread ID 不允许被当作新 run 重用。

## 恢复规则

| 图位置与应用记录 | 恢复行为 |
| --- | --- |
| 等待节点，有 open pending，无回答 | 显示同一 paused 待办 |
| 等待节点，回答已持久化 | interrupted；自动发送已保存的回答 |
| 其他还有 next 的节点 | interrupted；从检查点继续 |
| 业务效果已提交，图还在 approve | 返回已有回执，不再改变金额 |
| 图已结束 | 返回 completed/handed_off 及真实业务状态 |
| 调用预算、脚本/交接等硬性约束失败 | 持久转人工；重启不重置限制 |

## 验证与限制

真实临时 SQLite、create_agent、StateGraph、interrupt、Command 和独立 Python 进程验证恢复；os._exit(86) 覆盖事务前后、人工输入保存和多个节点检查点前退出；两个线程并发争用真实数据库验证幂等和退款上界。

本地事务只能保证当前模拟数据库内的效果。后续接外部退款接口需要对方幂等键和对账机制。当前演示身份不构成认证；真实模型、P07 token/并发预算、P08 HTTP 服务和生产迁移工具仍在后续阶段。
