# 运行指南

当前支持 Python 3.12、macOS/Linux、本地 SQLite 和一个 HTTP 服务进程。安装命令见 [首页](../README.md)，演示场景见 [演示指南](demo.md)。

## 两种运行模式

| 模式 | 启动方式 | 时钟与资料 |
| --- | --- | --- |
| 完整演示 | `after-sales demo`，可加 `--data-dir var/demo` | 固定资料时间；临时库或显式持久目录 |
| 实际时钟本地服务 | 显式 seed 后运行 Uvicorn | 当前 UTC；配置指定的持久数据库 |

持续演示用 `--data-dir` 保存完整状态。实际时钟服务会把历史退货案例判定为超期，这是正常规则结果。

```bash
cp .env.example .env
uv run --locked after-sales doctor
uv run --locked after-sales seed --dataset demo --json
uv run --locked uvicorn after_sales.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

`doctor` 只检查配置、依赖与运行能力，不初始化库或请求模型；`seed` 执行迁移并加载模拟资料，重复执行保留已有业务变更。`demo` 覆盖模型与数据库路径，使用离线模型及独立演示库；其余预算/队列配置仍可来自环境。

## 配置

完整配置见 [.env.example](../.env.example) 与 [Settings](../src/after_sales/config.py)。配置使用 `AFTER_SALES_` 前缀，环境变量优先于 `.env`。

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `BUSINESS_DB_PATH` / `CHECKPOINT_DB_PATH` | `var/business.sqlite` / `var/checkpoints.sqlite` | 业务记录与图检查点，必须使用不同文件 |
| `API_WORKERS` / `API_QUEUE_CAPACITY` | 1 / 32 | 一个 HTTP 进程内的执行线程与等待容量 |
| `MAX_MODEL_CALLS` / `MAX_TOOL_CALLS` | 20 / 30 | 全运行共享调用额度，包含复算与恢复 |
| `REVIEW_REPAIR_LIMIT` | 2 | 代码、审核与金额修订共享返工上限 |
| `MAX_CONCURRENCY` | 2 | 运行内调用许可上限 |
| `TOKEN_BUDGET` / `MODEL_TOKEN_RESERVATION` | 50000 / 2048 | 应用预留额度，未知 usage 不释放预留 |
| `ACTIVE_TIME_BUDGET_SECONDS` | 300 | 活动运行时间，正常人工等待不计入 |
| `TRANSIENT_RETRY_LIMIT` / `RETRY_BACKOFF_SECONDS` | 1 / 0.05 | 暂时错误的有限重试与退避 |
| `TOOL_TIMEOUT_SECONDS` / `TOOL_MAX_RESULT_BYTES` | 3 / 12000 | 查询时限与单次工具输出字节数 |
| `PROPOSAL_REPAIR_LIMIT` / `MODEL_TIMEOUT_SECONDS` | 1 / 30 | 输出格式修复上限与单次模型时限 |

`API_WORKERS` 是执行线程数，与 Uvicorn 的 `--workers` 不同。HTTP 必须保持 `--workers 1`；同库多个服务进程会因所有权锁拒绝启动。

## 健康与恢复

`GET /health` 检查业务数据库和执行线程就绪，返回服务版本和模式；未就绪返回 503。`/docs` 与 `/openapi.json` 展示协议。请求 ID 位于 `X-Request-ID`，错误体也包含 `request_id`。

| 重启前状态 | 重启后的处理 |
| --- | --- |
| queued | 执行器继续领取已持久任务 |
| running | 标记 interrupted，检查结果后显式恢复 |
| paused | 保留原待办，用 responses 回答 |
| completed / cancelled | 返回已有结果与回执，不重新执行 |

CLI 的持久入口也可以恢复，例如 `after-sales resume --run RUN-ID`。静态 JSON 报告不能恢复图；恢复依赖可信数据库与版本清单。

## 备份与重置

正常停止服务并等待退出，然后将业务数据库、检查点数据库及目录中存在的 SQLite sidecar 文件一起复制到新备份目录。停止后备份避免两个独立数据库处于不同时间点。恢复时成对还原到原配置路径；修改检查点路径可能触发所有权/版本校验拒绝，不能把单独 JSON 当作恢复资料。

重复 `seed` 保留状态。`seed --reset` 仅针对带应用/demo 标记且结构已知的业务库，清除该库中的业务运行与账本，不处理检查点库。准备一套全新演示时，使用新的 `--data-dir`，或默认临时模式，避免混用旧检查点与新业务资料。

## 运行边界

公开演示令牌仅用于本地角色展示，不构成生产身份系统。真实模型、外部支付与消息发送尚未接入；不要将演示服务直接作为线上客户服务。生产接入的功能方向见 [路线图](roadmap.md)。
