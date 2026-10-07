# ADR 010：独立评分与持久单 Agent 对照

- 日期：2026-10-07
- 状态：采用；真实模型与真人标注延期
- 对应阶段：P10

## 问题

P03 单 Agent 只生成建议，P07 多 Agent 已能审批和执行。直接比较两者会把基础设施差异当成 Agent 分工收益，也无法检验跨进程审批与重复写入。评估还必须避免预期答案进入模型上下文，以及因丢失失败案例而高估通过率。

## 决定

1. 增加 `SingleReviewRun`，单个调查 Agent 完成读取与方案生成，代码执行协调和审核。复用 ParallelReviewRun 的预算、checkpoint、人工输入、最新事实复核、事务动作账本与取消。没有额外模型审核员；多 Agent 保留原有分工及模型审核，并共享 `minimum_review` 的代码下限。
2. CLI 显式使用 `--architecture single --workflow single`，HTTP 启动请求可以指定 `workflow: single`，默认仍为 parallel。单 Agent 保存独立 workflow/state/report 版本；加载由 WorkflowService 按持久版本分派。P03/P04/P05/P06/P07 历史入口与报告保留。
3. runner 只把输入案例交给业务执行器。每个案例×架构×重复使用新的临时业务库和 checkpoint，固定输入定义的业务时间；架构顺序逐轮交换。保存全部 observation 及 SHA256、完整执行矩阵后，再打开独立 gold。重新评分不运行 Agent，拒绝输入变化、文件篡改、缺失/重复观察。
4. gold 保留合法结果集合、必要来源、允许动作、禁止行为和上限。评分读取真实 action_ledger、退款余额前后快照及业务历史增量，而不是只看建议或“执行成功”文字。未完整执行的尝试算失败，安全统计为 null；CLI 返回失败退出码。完整观测下的业务评分失败保留在报告里，不使评估命令丢弃结果。
5. 故障实际作用于工具结果、模型候选、审核门槛或批准后事务资料。C16 子进程在动作提交后 `os._exit(86)`，恢复后检查一次写入；C19 使用 ASGI HTTP 启动/审批并重放；C20 可开启真实 Chromium 刷新、断开非零游标请求并要求同游标成功重试。未开启浏览器时 C20 标为未覆盖。
6. 输入预期的 `delivery_proof` 映射到 EvidenceStore 的 `proof` 契约名称。预算停止优先读取实际 error 或 task_results 中的预算错误，不根据案例名字推断结果。浏览器角色标签依据实际事件显示，单Agent不会显示多Agent专员。没有修改 gold 来迁就输出，也没有根据留出失败调整提示词。
7. ScriptedChatModel 未提供 token usage，actual tokens 和费用维持 null；已知 token 小计与未知调用数分别显示。价格文件由用户提供版本、provider/model、币种及每百万输入/输出 token 价格；估算不是实际账单。离线三次重复只检查流程稳定性，不是模型采样质量实验。
8. 语义抽查保存证据 ID/版本、断言与事实依据及 reviewer。当前 reviewer 为 Codex，真人复核状态明确 pending；没有使用模型裁判产生通过率。真实模型评估入口保存 `LIVE_PROVIDER_DEFERRED`，不发网络请求、不产生虚假通过率。

## 取舍

两种架构共用业务与执行保障，但并不是仅改变并发开关的消融实验：单 Agent 使用代码协调/审核，多 Agent 多出协调、专员和模型审核调用。默认预算相同，角色较多可能更早耗尽预算；C17 保留这种差异及缺失证据。180 次是 30 个离线案例的重复观测，不能当成 180 个独立业务样本。性能仅代表本机脚本开销，真实 provider 的延迟、质量和价格未测量。

## 验证

- `tests/test_single_durable.py`：一个 Agent、共用门槛、跨重启批准/重放、客户补充、退款修订和取消。
- `tests/test_evaluation.py`：真实故障、账本安全反例、gold 读取时序、仅重新评分、哈希/矩阵校验、未覆盖浏览器、异常观测、价格精度和 live 延期。
- `tests/test_evaluation_cli.py`：CLI 输出、拒绝覆盖、日常库隔离、真实动作数显示。
- `evals/reports/p10-offline-v1/`：本机完整评估、失败案例与证据抽查；CI 重新执行完整三轮并保留原始资料七天。
