# 模拟资料与开发案例

业务资料版本为 `demo-v1`，固定 seed 为 42。业务判断时间为 `2026-10-02T04:00:00Z`，即 Asia/Shanghai 的 2026-10-02 12:00。所有客户、商品、物流、政策与售后历史均为虚构。

业务资料保存在 [demo-v1.json](../src/after_sales/data/demo-v1.json)，包含 3 个客户、4 个商品、30 个订单、20 个工单、52 个物流事件、8 条凭证资料、7 份政策版本和 3 条售后历史。文件随 Python wheel 打包；运行时使用 `importlib.resources` 加载，因此与命令所在目录无关。

开发案例输入在 [dev-cases-v1.json](dev-cases-v1.json)，预期结果独立保存在 [dev gold](../evals/gold/dev-v1.json)。运行时业务库和查询接口不读取 gold。

## 开发案例索引

| Case | 工单 | 测试场景 | 首次验证业务行为的阶段 |
| --- | --- | --- | --- |
| C01 | T-DELAY-001 | 物流未超期 | P03 |
| C02 | T-DELAY-002 | 物流超期 | P03 |
| C03 | T-NOTRECEIVED-001 | 签收未收到、缺凭证 | P03 |
| C04 | T-NOTRECEIVED-002 | 物流调查确认丢件 | P06 |
| C05 | T-RETURN-001 | 窗口内、未拆封退货 | P06 |
| C06 | T-RETURN-002 | 超过七天窗口一秒 | P02 |
| C07 | T-MISSING-001 | 没有提供订单号 | P05 |
| C08 | T-CROSS-001 | 提供其他客户的订单号 | P02 |
| C09 | T-CONFLICT-001 | 两份同时有效的政策冲突 | P04 |
| C10 | T-INVALID-EVIDENCE-001 | 候选方案引用不存在 | P05 |
| C11 | T-REVIEW-LOOP-001 | 审核持续不通过 | P05 |
| C12 | T-DUPLICATE-001 | 重复提交同一动作 | P06 |
| C13 | T-TOOL-TIMEOUT-001 | 工具暂时超时 | P07 |
| C14 | T-PROOF-ERROR-001 | 凭证查询持续失败 | P02 |
| C15 | T-STALE-APPROVAL-001 | 批准后事实变化 | P06 |
| C16 | T-CRASH-001 | 业务提交后、检查点前崩溃 | P06 |
| C17 | T-BUDGET-001 | 并行 worker 争夺最后预算 | P07 |
| C18 | T-PROMPT-INJECTION-001 | 物流文本包含指令注入 | P04 |
| C19 | T-API-DUPLICATE-001 | API 重复启动和确认 | P08 |
| C20 | T-UI-RESUME-001 | 页面刷新、已有退货尚未履约 | P09 |

P01 只建立数据和案例定义。`fault_injections` 是后续测试 runner 的输入契约，当前查询命令不会执行超时、崩溃、审核返工或浏览器行为。

## 资料语义

- `Ticket.supplied_order_id` 是用户填写的引用，可缺失、不可访问或不存在。`Ticket.order_id` 是已核验且属于该客户的关联，由数据库复合外键约束。
- 金额用整数分；输入模型拒绝负数、浮点数、布尔值、数字字符串和超过 SQLite 整数范围的数值。
- 时间必须带时区，写入 SQLite 与 JSON 时统一 UTC；工单文本展示使用 Asia/Shanghai。
- 政策有效期采用 `[effective_from, effective_to)`。当前时间下未来 v2 与过期政策不会进入候选；`RETURN-CONFLICT` 只适用于 `P-CONFLICT`。
- 凭证的 `present`、`missing`、`unknown` 是不同资料状态；没有凭证行表示库里没有这项资料，不自动解释为凭证不存在。
- 模拟订单中的商品状态与物流资料是各自来源的记录；工单消息保留客户陈述，后续工具与证据仍需区分来源。

## 留出案例

留出输入在 [cases-v1.json](../evals/holdout/cases-v1.json)，独立预期在 [holdout gold](../evals/gold/holdout-v1.json)。它们使用开发工单以外的订单，覆盖部分退款余额、完整退款余额、恰好边界、超界一秒、已拆封、不适用类别、收货时间未知和不存在的订单等。

留出请求尚未写为日常演示工单；P10 评估 runner 将在临时数据库中创建它们。P01 只检查案例引用与结构完整性，尚未评价 Agent 质量。
