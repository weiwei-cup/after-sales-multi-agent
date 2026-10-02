# ADR 007：独立候选检索、可信 join 与累计调用账本

日期：2026-10-03。状态：采用。范围：P07 离线、本机 POSIX / SQLite 工作流。

## 问题与决定

P06 的订单调查与适用政策检索串行执行；原来的 `search_policies` 会读取归属已校验的订单来过滤商品范围，不能直接把它放入独立并行分支。P07 新增 `search_policy_candidates`，只按可信工单类型与业务时间读取政策候选，不查询订单、不判断适用性。主图使用 LangGraph fan-out：`order_branch` 与 `candidate_branch` 同一 superstep 执行，`add_edge([两个分支], "join")` 等待二者完成。join 验证结果后，原有政策专员再根据真实订单计算商品范围和规则。

对应官方接口：[StateGraph.add_edge](https://reference.langchain.com/python/langgraph/graph/state/StateGraph/add_edge)。本地锁定 LangGraph 1.2.12 的实际 barrier、持久恢复测试也验证了这一行为。

P04/P05 的 IntakeResult 继续负责可信槽位整理；P07 的 `parallel_plan` 表示实际调查依赖，两个独立 task 无依赖，政策适用性依赖两个结果。新的候选专员有独立 `create_agent`、消息历史、输出 DTO 与工具 allowlist。协调、订单、政策与审核仍是四种角色。

## 合并与恢复

- task key 为 `input_revision:kind`；结果 reducer 用完整规范化内容去重，保留同 task 的不同结果，join 显式转人工。
- evidence reducer 在同来源、来源 ID、业务观察时间下比较版本和内容；相同快照不重复，不同版本或同版本不同内容明确记入 conflicts。后续有意刷新使用不同观察时间。
- `branch_results` 先持久化分支结果及可校验 hash 的证据，再交给 LangGraph checkpoint。恢复遇到已完成 task 直接复用，避免“业务分支完成、异步 checkpoint 未落盘”窗口重复查询。SQL 中出现多个结果变体时仍交给 reducer 判冲突。
- 主状态更新只发生在顺序节点；两个分支只提交带 reducer 的字段，不并发覆盖 route/status/node_trace。分支事件另记录真实执行顺序。
- 程序或模型契约失败转人工，成功分支的结果及证据保留。只读工具的明确暂时失败可有限重试；不把缺失事实当作否定事实。

## 预算与并发

新增 business schema v3，v1/v2 的历史迁移语句与 checksum 保持不变。P06 在 v2/v3 均可恢复。新表 `run_budget`、`call_reservations`、`run_events`、`run_control`、`branch_results` 属于业务应用；物理 checkpoint 文件沿用已有格式，逻辑状态独立标记 `parallel-state-v1` / `parallel-review-v1`。

每次模型/工具调用先在 `BEGIN IMMEDIATE` 事务中预留一个 attempt，再等待统一 asyncio semaphore。失败、超时、排队后取消和 schema 修复中的调用都不退还调用次数。单一计数覆盖各角色、校验、定向补查、返工及执行器；并发争最后一个额度最多一个成功。恢复后的调用、usage、schema 修复、事件与审核返工额度以 SQL 账本为准，旧 runtime JSON 或 checkpoint 不增加可用额度。

每个模型 attempt 默认预留 2048 token。有效 provider usage 可以替换预留值；没有 usage、调用失败或崩溃时保留完整预留。实际 token 在未知时为 null，已知 subtotal 单列；没有验证过价格，费用为 null。预留额度是保守的应用预算，**不是对真实 provider token 上限的证明**：实际报告超过预留或总预算时照实对账，并阻止下一次调用。真实模型仍延期。

活动时间按执行 segment 的墙钟计算，正常 pause / finish 关闭 segment，因此客户思考与进程关闭时间不占额度。进程在活动 segment 内崩溃时没有可信结束时刻，恢复保守地计入至恢复时刻，可能包含故障停机时间。异步调用 deadline 取单次 deadline 与剩余活动预算的较小者；安全节点检查预算。同步 SQLite 写事务和仍运行的只读线程遵守自己的有限 I/O deadline，不强行中断已提交事务。

ReadExecutor 继续只有两个线程，超时线程占用其线程槽直到真实读取结束。统一 semaphore 限制前台模型/工具调用；无法杀死的后台只读线程另受 ReadExecutor 两槽限制。调低 max_concurrency 不会把已有超时线程变成新许可，后台结果不自动生成新证据。

## 错误与事件

模型只对 TimeoutError、ConnectionError 和显式 429/502/503/504 有限退避重试，默认额外一次。工具只重试可信 `TOOL_TIMEOUT` / `TOOL_BUSY`；泛化的 QUERY_FAILED 可能来自程序问题，不盲目重试；已有审核可以在共享返工额度内决定补查。无权访问、政策拒绝、schema 契约错误、程序异常不走暂时错误重试。schema 修复有单独累计计数，下一次模型调用仍要预留。

事件序号由 SQL 事务分配，开始/完成/失败配对使用固定 attempt_id；并发角色统计直接来自账本，不能用全局计数差误算另一个角色的调用。事件记录版本、身份、revision、分支 task、决策摘要、排队时间与 attempt 耗时。耗时合计可包含并发重叠及排队，不能当作端到端墙钟。完整运行墙钟与活动时间另记。

## 取消与事务

`cancel --run` 只提交信号，不等待持有运行锁的执行进程。报告区分 new_request、cancel_requested 与 acknowledged；暂停运行需要下一次 resume 才应用信号。主图在节点边界、调用预留和队列获许可时检查；人工输入事务及新业务动作事务也检查。请求取消不使用 asyncio.Task.cancel 强杀数据库写入。

已有账本回执优先返回，未提交动作在同一业务写事务内检查取消。取消先取得写锁：新动作被拒绝；动作先取得写锁并提交：随后取消保留真实回执、余额与业务状态。run 可为 cancelled，而已退款工单仍 resolved。终止后不能用旧人工待办启动新动作。取消不撤销已登记退货、不回滚已提交退款，不等于外部付款系统的撤销。

## 权衡与验证

增加候选 Agent、强校验与持久账本有开销，并行不保证每个案例都更快。`scripts/demo_p07.py` 对相同案例分别观测无注入延迟、两条独立读取各注入 500 ms；结果记录完整时间线与真实测量值。barrier 测试证明 overlap，性能数据只是单次教学观测。

真实 subprocess / os._exit 覆盖预留后退出、join checkpoint 空隙、业务提交后退出与独立取消进程。另验证最后一次额度竞争、未知 usage、有限重试、scope、冲突合并、正常暂停时间、崩溃活动 segment、旧 P06 迁移恢复。P08 HTTP/鉴权与真实模型、生产费用、分布式调度不在本阶段。
