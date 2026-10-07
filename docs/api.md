# API 说明

启动 `after-sales demo` 后访问 [交互文档](http://127.0.0.1:8000/docs) 与 [OpenAPI](http://127.0.0.1:8000/openapi.json)。完整字段以 [请求与响应模型](../src/after_sales/api/contracts.py) 为准。

## 身份与写请求

本地演示使用 `Authorization: Bearer TOKEN`：`demo-customer-a`、`demo-customer-b` 与 `demo-operator`。客户只能访问自己的工单与运行；操作员确认业务动作。身份在服务端注入，客户端不能用 JSON 修改客户归属。

所有 POST 要求 `Idempotency-Key`，最长 128 字符，允许字母、数字、`_ . : -`。同身份、路径、key 和内容返回相同回执；同键不同内容返回 409。网络失败后重试必须保留原 key 与请求内容。

## 接口索引

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 服务就绪状态，无需身份 |
| `GET /tickets?limit=50&offset=0` | 当前身份可访问的工单 |
| `POST /tickets` | 客户创建工单，201 |
| `GET /tickets/{ticket_id}` | 工单与最新运行 |
| `POST /tickets/{ticket_id}/runs` | 启动 parallel 或 single，202 |
| `GET /runs/{run_id}` | 运行状态、待办与结果 |
| `GET /runs/{run_id}/events?after_seq=0&limit=100` | 允许公开的有序事件 |
| `POST /runs/{run_id}/responses` | 提交当前客户/操作员待办，202 |
| `POST /runs/{run_id}/resume` | 恢复 interrupted，202 |
| `POST /runs/{run_id}/cancel` | 提交取消信号，202 |

`202` 表示已持久接纳和排队，客户端查询 run 等待后续结果。`paused` 用 responses，`interrupted` 用 resume。事件返回 `next_after_seq` 与 `has_more`；刷新/断线后保留游标，避免丢失和重复显示。

## 创建并启动工单

```bash
curl -sS http://127.0.0.1:8000/tickets \
  -H 'Authorization: Bearer demo-customer-a' \
  -H 'Idempotency-Key: showcase-ticket-01' \
  -H 'Content-Type: application/json' \
  -d '{"type":"delivered_not_received","supplied_order_id":"ORD-004","message":"显示签收但我没有收到，请核实。"}'
```

复制响应中的 `ticket_id`，替换以下占位值：

```bash
curl -sS http://127.0.0.1:8000/tickets/TICKET-ID/runs \
  -H 'Authorization: Bearer demo-customer-a' \
  -H 'Idempotency-Key: showcase-run-01' \
  -H 'Content-Type: application/json' \
  -d '{"workflow":"parallel","expected_input_revision":1}'
```

省略 workflow 时默认 parallel；single 使用相同审批、预算与动作协议。工单类型支持 `logistics_delay`、`delivered_not_received`、`return_request`，模型不负责当前演示的自由文本分类。

## 人工回答契约

从当前 run 的 `pending_input` 取得 `pending_id`、`input_revision`、`proposal_revision`、`proposal_hash`。动作确认还必须提供 `action_hashes`：每个 action_id 对应当前内容哈希。服务端补齐可信运行/身份信息，不接受客户端伪造审批对象。

客户回答在 `answers` 中提供所需字段，例如 `order_id`。操作员使用 `decision=approve/reject/revise`；修改退款时 `refund_amounts` 按 action_id 指定整数分。新方案生成后要获取新的待办与哈希，再确认。前端已经实现这一协议；现场演示优先使用工作台。

公开结果只含允许展示的证据摘要、动作回执、业务状态和统计，不返回完整 Agent messages、图状态或凭据。

## 错误约定

错误响应形如 `{"code":"CONFLICT","message":"…","request_id":"…"}`，同时返回 `X-Request-ID`。

| 状态码 | 含义 |
| --- | --- |
| 401 / 403 | 演示身份无效或当前角色无权限 |
| 404 | 资源不存在，或当前身份不可访问 |
| 409 | 活跃运行冲突、旧待办/版本/哈希、幂等内容变化 |
| 422 | 字段、金额、格式或字段组合无效 |
| 503 | 队列容量不足或执行器未就绪 |
| 500 | 内部错误；记录 request_id 后检查服务日志 |

同一工单有活跃运行时应继续原 run，不能通过换 key 另开运行。取消不撤销已经提交的模拟业务动作。
