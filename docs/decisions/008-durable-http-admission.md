# ADR 008：持久化 HTTP 请求与本地执行器

- 日期：2026-10-07
- 状态：采用
- 对应阶段：P08

## 问题

P07 的 CLI 已能恢复图与模拟业务动作。HTTP 要立即返回，还需要解决请求重试、身份范围、队列容量以及“答复已经接受，执行进程却退出”的窗口。

## 决定

1. 用 FastAPI lifespan 管理服务。业务库必须先明确 seed；启动只迁移、核对资料，不重置数据。单个业务库用 POSIX 文件租约限制为一个 HTTP 服务进程。运行仍保留 P06/P07 的 run 和 ticket 文件锁。
2. `ApplicationService` 负责受身份约束的 admission 与查询；CLI 和执行器使用同一 `WorkflowService` 创建/加载、恢复及取消工作流。HTTP handler 只做 DTO、身份与状态码处理。
3. 初始 run/runtime、queued execution job 和 HTTP 幂等回执在同一业务事务保存，再通知执行器。模型不在请求事务里运行。仅已由 HTTP 登记的 queued run 允许从零初始化 checkpoint，旧 CLI 的 checkpoint 丢失仍按不兼容处理。
4. 人工答复的完整内部 envelope 由服务器补齐 run/ticket/actor/role。待办版本与动作 hash 校验后，将 human input、queued job 和请求回执在同一事务提交。执行器加载后通过既有 `recover()` 消费已接受的答复。
5. 线程数默认 1，范围 1–4；等待容量默认 32，范围 1–1000。线程直接从 SQLite claim，避免另一个无界内存任务队列；每次运行创建自己的 asyncio loop。同步账本操作与模型处理因此不占用 HTTP 的 event loop。
6. 幂等范围为 actor + method/path + key，指纹为已校验请求的规范化 JSON。不同内容返回 409。同工单活跃 run 注册仍在写事务内校验；request key 与业务 operation key 各自独立。
7. startup 自动恢复 queued job；HTTP-owned running job 标记 interrupted，不自行重放不确定的运行；paused 保持待办。CLI 自行运行的记录不被服务 startup 改写。interrupted 由 `/resume` 明确恢复，paused 由 `/responses` 回答。
8. 取消标记和事件原子提交。队列满也接受标记；容量释放后给闲置 paused/interrupted run 安排取消投影。取消任务失败后保留 interrupted 和错误码，不自动无限重试。已提交的业务回执保留。
9. public DTO 只返回工单、精简待办/结果、调用计数和 allowlist 事件。待办只在 execution job 结束后暴露可答状态，避免图已暂停、执行器还没释放的竞争。HTTP 不返回 graph、原始消息日志、证据 payload、令牌或账本 operation key。

## 取舍

这是 POSIX 单机演示。固定公开演示令牌用于学习角色和工单范围，不是生产认证。没有分布式队列、租约超时接管、多进程 HTTP worker 或外部支付。shutdown 等待当前有预算的工作结束，未 claim 的 queued job 留待下一次启动。业务库和 checkpoint 文件仍需一起保存。

## 证据

`tests/test_api.py` 验证真实 ASGI 调用、相同/不同 key 的并发、同工单竞争、身份范围、版本/非法字段、事件投影、执行器容量与健康响应。`tests/test_api_recovery.py` 通过子进程 `os._exit(86)` 验证 admission、运行节点、接受审批及业务提交窗口。`scripts/demo_p08.py` 启动真实 loopback Uvicorn，执行创建→补充→批准→结果。
