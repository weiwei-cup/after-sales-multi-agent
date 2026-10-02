"""A local interactive demo keeps the InMemorySaver owner alive through every answer."""

from pydantic import ValidationError

from after_sales.workflows.reviewed import InMemoryReviewRun, ResumeRejected, response_envelope


async def run_interactive(repository, ticket_id, settings, **kwargs):
    run = InMemoryReviewRun(repository, ticket_id, settings, **kwargs)
    try:
        report = await run.start()
        return await interact(run, report)
    finally:
        run.close()


async def interact(run, report):
    while report["status"] == "paused":
        pending = report["pending_input"]
        print(
            f"待办 {pending['pending_id']} | {pending['kind']} | "
            f"输入 v{pending['input_revision']} / 方案 v{pending['proposal_revision']}"
        )
        print(f"当前草稿：{report['proposal']['customer_reply_draft']}")
        try:
            if pending["kind"] == "customer_info":
                answers = {
                    q["field"]: input(f"{q['question']} ").strip() for q in pending["questions"]
                }
                raw = response_envelope(pending, answers=answers)
            else:
                for binding in pending["actions"]:
                    action = binding["candidate"]
                    print(
                        f"动作 {binding['action_id']} | {action['type']} | "
                        f"订单 {action['order_id']} | 金额 {action['amount_cents']} 分"
                    )
                    print(f"内容哈希：{binding['content_hash']}")
                print(
                    "本地演示操作员 OP-DEMO；批准后执行模拟业务动作。"
                    if report["phase"] == "P06"
                    else "本地演示操作员 OP-DEMO；批准仅记录确认，业务写入留给 P06。"
                )
                decision = input("选择 approve / reject / revise（修改退款金额）/ quit：").strip()
                if decision == "quit":
                    break
                amounts = {}
                if decision == "revise":
                    for binding in pending["actions"]:
                        if binding["candidate"]["amount_cents"] is not None:
                            amounts[binding["action_id"]] = int(input("新退款金额（整数分）："))
                raw = response_envelope(pending, decision=decision, refund_amounts=amounts)
            report = await run.resume(raw)
        except (ResumeRejected, ValidationError, ValueError):
            print("回答未消费：检查当前角色、字段、版本和整数分金额，再按当前待办输入。")
        except (EOFError, KeyboardInterrupt):
            print(
                "交互结束；可用 resume --run 恢复该待办。"
                if report["phase"] == "P06"
                else "交互结束，保存暂停快照；此进程关闭后不能从文件恢复，持久恢复留给 P06。"
            )
            break
    return report
