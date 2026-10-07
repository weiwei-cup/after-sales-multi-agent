# 对照评估资料

P10 已实现离线对照评估，使用 P01 保存的版本化案例与独立预期。

- [开发案例输入](../fixtures/dev-cases-v1.json)：20 个案例，映射到演示工单。
- [开发 gold](gold/dev-v1.json)：人工编写的合法结果、动作门槛、必要来源与禁止行为。
- [留出输入](holdout/cases-v1.json)：10 个独立请求，使用开发工单以外的订单。
- [留出 gold](gold/holdout-v1.json)：独立保存的预期，用于最终对照评估。

gold 使用业务结果代码，不强制自然语言全文。动作列表示可以提出并在操作员确认后执行的动作，不表示资料一匹配就直接写入业务。

完整本机报告见 [p10-offline-v1](reports/p10-offline-v1/report.md)，机器结果见 [evaluation.json](reports/p10-offline-v1/evaluation.json)。30 个案例×两种架构×三次重复，共180次；开发集单 Agent 54/60、多 Agent 51/60，留出集各27/30。关键安全错误均0，业务失败全部保留。原始记录和6张刷新截图压缩保存为 [observations.tar.gz](reports/p10-offline-v1/observations.tar.gz)，SHA256 在 [archive-hashes.json](reports/p10-offline-v1/archive-hashes.json)。

Agent 和业务执行器不加载 gold。runner 保存并校验全部系统输出、输入哈希和执行矩阵后，评分器才打开 gold。留出失败没有用于本轮提示词或规则调优。测试和新评估输出保存于忽略目录 `evals/output/`，每次必须选择新的输出路径。

## 复现

完整评估需要锁定开发依赖中的 httpx 和 Playwright；业务 wheel 不包含本目录的输入/gold。首次安装浏览器需网络，后续业务和脚本模型均离线运行。

```bash
uv sync --locked --python 3.12
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers"
uv run --locked playwright install chromium
uv run --locked after-sales eval run --repetitions 3 --browser --output-dir evals/output/my-p10 --json
```

每格创建临时 SQLite 和 checkpoint，保持30案例定义的业务时间，不修改日常库。C16真进程退出后恢复，C19真实ASGI请求重放，C20真实浏览器刷新/非零游标断线重连；不加 `--browser` 时 C20明确未覆盖并使该案例评分失败。默认20次模型调用/30次工具调用、50000 token预留预算、并发2；C17两种架构同为2次模型调用，结果保留角色调度差异。

快速试一个业务，或仅对已有记录重新评分：

```bash
uv run --locked after-sales eval run --split dev --case C04 --output-dir evals/output/my-refund --json
uv run --locked after-sales eval score --observations evals/output/my-p10 --output-dir evals/output/my-rescore --json
mkdir -p evals/output/p10-archived
tar -xzf evals/reports/p10-offline-v1/observations.tar.gz -C evals/output/p10-archived
uv run --locked after-sales eval score --observations evals/output/p10-archived --output-dir evals/output/p10-archived-rescore --json
```

评分检查结果集合、必要来源、引用版本、合法动作、审批、停止、实际调用预算、退款/返工/写入上限与禁止行为。实际写入安全从账本、批准绑定、余额和业务记录增量判断。`delivery_proof` 对应程序证据名称 `proof`。哈希检查用于重评分完整性，不是外部签名认证。

退出码：0表示评估已完成且安全观察完整、关键错误为0，业务评分失败仍见报告；1表示关键错误或执行异常导致安全观察不完整；2表示输入/配置/文件错误。不要仅凭退出码宣称全部业务案例通过。

## 边界

报告记录每组真实分母、每例三次结果是否一致、调用、活动/墙钟耗时分位数和未知用量。ScriptedChatModel实际tokens与费用为null，known小计0不代表零用量。价格配置可使用 `--price-config 路径`，JSON字段为 version、provider、model、currency、unit=`per_million_tokens`、input_per_million、output_per_million；缺usage不能估算，不使用在线价格或编造账单。

[引用核对](reports/p10-offline-v1/citation-audit.md) 保存预选5例和失败H10的真实证据/完整ID；reviewer为Codex，真人标注pending，不计入程序评分。真实模型至少三次重复与≥90%目标待执行。`eval run --model live`仅保存LIVE_PROVIDER_DEFERRED，不请求模型，不产生通过率。脚本重复稳定不能推断真实模型质量、抗注入或收益。

本机归档来自P09基线上的P10工作树（source.working_tree_dirty=true），保留这个真实来源。阶段提交CI会在确切Git SHA上重新跑180次并保存完整资料7天；本机性能数字不是CI或真实provider性能。
