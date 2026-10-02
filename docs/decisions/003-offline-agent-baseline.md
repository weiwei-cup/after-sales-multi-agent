# ADR 003：离线模型参与实际 Agent 循环，候选建议独立校验

日期：2026-10-02

状态：已采用（P03）

## 背景

P02 已用代码验证工具和规则，P03 要学习模型→工具→模型循环，并留下可与多 Agent 对照的单 Agent 基线。用户选择暂时只用离线脚本模型，当前没有指定真实模型服务商。

## 决定

1. ScriptedChatModel 继承 LangChain BaseChatModel，支持 bind_tools、AIMessage.tool_calls、同步/异步生成与 ToolMessage。create_agent 负责真正的工具执行与消息回传；不以手工工具调用替代 Agent 集成验证。
2. 演示脚本按有限步骤生成响应，只读实际收到的工具结果、工单元信息和可信上下文，不访问 repository 或 gold。每一步检查预期工具、匹配 call ID、绑定工具和重复调用 ID；脚本用尽或未定义调用直接失败。
3. 用 ToolStrategy＋Pydantic ResolutionProposal 校验输出。schema 错误反馈最多修复一次，随后诊断失败；修复调用计入模型预算。默认次数可配置，和后续审核返工分别管理。
4. schema 通过后独立验证事实声明、证据范围/版本、动作对应 assessment 和完整政策依据，再用候选金额复算确定性规则。复算计入工具预算；不通过不形成 accepted_proposal。通过也只表示可展示的候选，不授权业务写入。
5. 记录模型/工具消息、事件、耗时和调用统计，保存 baseline-run-v1 JSON。临时文件完整写入后原子发布，已有路径拒绝覆盖。report show 验证文件结构并回看；文件不是可信审批输入，也不是图检查点。
6. live provider 和网络 smoke 延期。create_model 的 live 边界、live-smoke 和 run --model live 明确返回 skipped / LIVE_PROVIDER_DEFERRED，调用数为 0。保留配置字段不能视作适配器已经实现。

## 原因与影响

离线脚本让错误路径可复现，同时仍验证框架行为和真实临时业务数据库。更改实付金额后建议随工具结果改变，防止脚本按案例名直接返回正确答案。有限脚本不能支持任意新政策/调用顺序，未支持的路径必须显式扩展；它也不能证明真实模型理解、抗提示注入或语言生成质量。

Pydantic 解决字段、类型和字段之间的关系；业务校验解决事实支持和金额资格，两者需要分别验证。通用回复草稿的语义审核在 P05；执行前刷新事实、批准版本及幂等性在 P06，不能用本轮快照复算替代。全局并发、恢复预算、token 软预算及通用故障执行器在 P07。

## 验证

- 实际 create_agent 收到带匹配 call ID 的 ToolMessage；模型发出与记录收到的 ID 集合一致。
- 20 个现有工单的基础事实路径通过；没有按案例名自动激活未来的故障脚本。
- 第一次 schema 无效收到真实反馈并修复，连续无效有限停止。
- schema 合法的超余额退款被代码拒绝；伪造事实、引用、政策版本和订单也不能被接受。
- 预算包含最终结构化输出的模型调用和应用代码复算；预算不足不形成通过校验的候选。
- 所有工单测试前后业务数据库 dump 一致；live 为 skipped，无网络调用。
- JSON 可在另一 CLI 进程读取，已有文件不覆盖；保存和读取不带动作执行入口。

实现与命令见 [第 003 轮学习记录](../rounds/003.md)。框架依据：[Structured output](https://docs.langchain.com/oss/python/langchain/structured-output)、[Models](https://docs.langchain.com/oss/python/langchain/models)、[Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)。
