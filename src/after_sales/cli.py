"""Workbench demos, workflow operations, diagnostics and reproducible evaluation."""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError

from after_sales import __version__
from after_sales.agents.contracts import REPORT_ADAPTER
from after_sales.agents.runner import run_baseline, save_run
from after_sales.config import Settings
from after_sales.doctor import build_report
from after_sales.domain.models import TicketType, UtcTime
from after_sales.repositories.errors import RepositoryError
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, migrate
from after_sales.services.workflows import WorkflowService
from after_sales.tools.inspection import inspect_ticket
from after_sales.tools.service import ToolSession
from after_sales.workflows.interactive import interact, run_interactive
from after_sales.workflows.reviewed import run_reviewed
from after_sales.workflows.serial import run_multi


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出结构化结果（时间为 UTC）")


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须为 1～65535 的整数") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须为 1～65535 的整数")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="after-sales", description="售后协作 · 多 Agent 工单工作台"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="启动完整工作台演示，使用隔离资料和固定业务时间")
    demo.add_argument("--port", type=_port, default=8000)
    demo.add_argument("--data-dir", type=Path, help="保留演示资料与运行记录；默认使用临时目录")
    demo.set_defaults(json=False)
    doctor = commands.add_parser("doctor", help="检查本地配置与依赖，不调用模型")
    _json_flag(doctor)

    inspection = commands.add_parser("inspect", help="用只读工具调查工单、计算规则和生成证据")
    inspection.add_argument("--ticket", required=True, help="当前模拟工单 ID")
    _json_flag(inspection)

    run = commands.add_parser("run", help="运行单 Agent、串行或带审核的多 Agent，保存运行记录")
    run.add_argument("--ticket", required=True)
    run.add_argument("--architecture", choices=["single", "multi"], default="single")
    run.add_argument(
        "--workflow",
        choices=["serial", "reviewed", "durable", "parallel", "single"],
        default="serial",
        help="执行图；parallel 为持久多 Agent，single 为持久单 Agent；默认 serial 仅生成建议",
    )
    run.add_argument("--interactive", action="store_true", help="交互式回答追问、确认或修改动作")
    run.add_argument(
        "--model",
        choices=["scripted", "live"],
        help="默认使用配置；live 适配器尚未实现，显示 skipped",
    )
    run.add_argument("--output", type=Path, help="结果文件；默认 var/runs/运行ID.json，禁止覆盖")
    run.add_argument("--run-id", help="持久工作流的稳定运行 ID")
    _json_flag(run)

    resume = commands.add_parser("resume", help="从可信数据库恢复持久运行")
    resume.add_argument("--run", required=True)
    resume.add_argument("--model", choices=["scripted"], default="scripted")
    resume.add_argument("--response-file", type=Path, help="完整待办答复 JSON，绑定身份与版本")
    resume.add_argument("--output", type=Path, help="另存静态报告，禁止覆盖")
    _json_flag(resume)
    cancel = commands.add_parser("cancel", help="请求取消持久单 Agent / 并行运行；已提交动作保留")
    cancel.add_argument("--run", required=True)
    _json_flag(cancel)

    evaluation = commands.add_parser("eval", help="在临时资料库对照评估；先保存结果再读 gold")
    evaluation.add_argument("operation", choices=["run", "score"])
    evaluation.add_argument("--suite-root", type=Path, default=Path("."))
    evaluation.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    evaluation.add_argument(
        "--architectures", nargs="+", choices=["single", "multi"], default=["single", "multi"]
    )
    evaluation.add_argument("--repetitions", type=int, default=1)
    evaluation.add_argument("--case", dest="case_ids", action="append", default=[])
    evaluation.add_argument("--output-dir", type=Path)
    evaluation.add_argument("--observations", type=Path)
    evaluation.add_argument("--model", choices=["scripted", "live"], default="scripted")
    evaluation.add_argument(
        "--browser", action="store_true", help="使用 Chromium 验证 C20，需 loopback"
    )
    evaluation.add_argument(
        "--price-config", type=Path, help="用户价格快照；未报告 token 不计算费用"
    )
    _json_flag(evaluation)

    reports = commands.add_parser("report", help="读取已保存的运行结果与事件")
    report_commands = reports.add_subparsers(dest="operation", required=True)
    report_show = report_commands.add_parser("show", help="读取单 / 多 / 审核 Agent JSON 运行记录")
    report_show.add_argument("path", type=Path)
    report_show.add_argument("--events-only", action="store_true")
    _json_flag(report_show)

    live = commands.add_parser("live-smoke", help="报告真实模型适配器状态；当前尚未实现")
    _json_flag(live)

    seed = commands.add_parser("seed", help="原子初始化模拟业务数据，重复执行保留已有数据")
    seed.add_argument("--dataset", choices=["demo"], default="demo")
    seed.add_argument("--reset", action="store_true", help="重置已标记的演示业务库中的数据")
    _json_flag(seed)

    database = commands.add_parser("db", help="业务数据库操作")
    db_commands = database.add_subparsers(dest="operation", required=True)
    _json_flag(db_commands.add_parser("migrate", help="执行版本化 schema migration"))

    tickets = commands.add_parser("ticket", help="本地演示操作员查询工单")
    ticket_commands = tickets.add_subparsers(dest="operation", required=True)
    ticket_list = ticket_commands.add_parser("list", help="列出工单")
    ticket_list.add_argument("--customer", help="只列出指定模拟客户的工单")
    _json_flag(ticket_list)
    ticket_show = ticket_commands.add_parser("show", help="展示工单、归属核验和业务资料")
    ticket_show.add_argument("ticket_id")
    _json_flag(ticket_show)

    orders = commands.add_parser("order", help="按模拟客户范围查询订单")
    order_commands = orders.add_subparsers(dest="operation", required=True)
    order_show = order_commands.add_parser("show", help="查询属于指定客户的订单")
    order_show.add_argument("order_id")
    order_show.add_argument(
        "--customer", required=True, help="演示身份；Agent 工具从可信上下文注入身份"
    )
    _json_flag(order_show)

    policies = commands.add_parser("policy", help="查询虚构版本化政策")
    policy_commands = policies.add_subparsers(dest="operation", required=True)
    policy_list = policy_commands.add_parser("list", help="按固定业务时间列出有效政策")
    policy_list.add_argument("--intent", choices=[kind.value for kind in TicketType])
    period = policy_list.add_mutually_exclusive_group()
    period.add_argument("--as-of", help="带时区的 ISO 时间；默认采用数据集时间")
    period.add_argument("--all-versions", action="store_true", help="包含过期和未来版本")
    _json_flag(policy_list)
    policy_show = policy_commands.add_parser("show", help="读取指定政策版本")
    policy_show.add_argument("policy_id")
    policy_show.add_argument("--version", type=int, default=1)
    _json_flag(policy_show)
    return parser


def _local_time(value: str | None) -> str:
    if value is None:
        return "未知"
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()


def _render_ticket(view: dict[str, object]) -> None:
    ticket = view["ticket"]
    print(f"工单 {ticket['id']} | {ticket['type']} | {ticket['status']}")
    print(f"模拟客户：{ticket['customer_id']}；订单引用：{view['order_reference_status']}")
    print(f"业务时间（Asia/Shanghai）：{_local_time(view['as_of_time'])}")
    for message in ticket["messages"]:
        print(f"{message['role']}：{message['content']}")
    order = view["order"]
    if order:
        paid = order["paid_cents"]
        refunded = order["refunded_cents"]
        print(f"订单：{order['id']}；状态：{order['status']}")
        print(f"实付：{paid // 100}.{paid % 100:02d}；已退：{refunded // 100}.{refunded % 100:02d}")
        print(f"预计送达：{_local_time(order['expected_delivery_at'])}")
        print(f"收货时间：{_local_time(order['received_at'])}")
        for item in order["items"]:
            print(f"商品：{item['product_id']} × {item['quantity']}；未拆封：{item['unopened']}")
        for event in view["tracking_events"]:
            print(f"物流 {_local_time(event['occurred_at'])}：{event['description']}")
        if view["delivery_proof"]:
            print(f"签收凭证：{view['delivery_proof']['proof_status']}")
        for record in view["after_sales_history"]:
            print(f"售后历史：{record['id']} | {record['type']} | {record['status']}")
    else:
        print("尚无已核验归属的订单资料。")
    for policy in view["policy_candidates"]:
        print(f"候选政策：{policy['id']} v{policy['version']} | {policy['title']}")
    print("业务资料视图；可用 after-sales inspect --ticket 工单号查看规则与证据。")


def _render_inspection(report: dict[str, object]) -> None:
    print(f"工单 {report['ticket_id']} | 只读调查")
    print(f"业务时间（Asia/Shanghai）：{_local_time(report['as_of_time'])}")
    print("模型调用：0；仅计算条件和动作候选。")
    for name, result in report["results"].items():
        if not result["ok"]:
            error = result["error"]
            print(f"{name}：{error['code']} | {error['message']}")
        elif name.startswith("evaluate_policy:"):
            data = result["data"]
            print(f"候选 {data['action']}：{data['disposition']}")
            for evaluation in data["evaluations"]:
                print(
                    f"  {evaluation['policy_id']} v{evaluation['policy_version']}："
                    f"{evaluation['eligibility']}"
                )
                print(f"  剩余可退：{evaluation['remaining_refund_cents']} 分")
                for condition in evaluation["conditions"]:
                    print(f"    {condition['name']} = {condition['value']} ({condition['reason']})")
            if data["conflicts"]:
                print(f"  政策冲突：{json.dumps(data['conflicts'], ensure_ascii=False)}")
        else:
            print(f"{name}：{json.dumps(result['data'], ensure_ascii=False)}")
        for ref in result["evidence_refs"]:
            print(f"  证据：{ref['evidence_id']} | 来源版本 {ref['source_version']}")
    print(f"证据快照：{len(report['evidence'])}；保存在调查会话内存，--json 可导出。")


def _render_run(report: dict[str, object]) -> None:
    print(f"运行 {report['run_id']} | 工单 {report['ticket_id']} | {report['status']}")
    proposal = report["accepted_proposal"]
    if proposal:
        print(f"建议：{proposal['decision']}")
        print(f"回复草稿：{proposal['customer_reply_draft']}")
        for action in proposal["actions"]:
            amount = f" | {action['amount_cents']} 分" if action["amount_cents"] is not None else ""
            print(f"动作候选：{action['type']} | {action['order_id']}{amount}")
        for question in proposal["unresolved_questions"]:
            print(f"待补资料：{question['field']} | {question['question']}")
    if report["validation"]:
        print(f"代码校验：{report['validation']['ok']}")
        for issue in report["validation"]["issues"]:
            print(f"  {issue['code']}：{issue['message']}")
    if report["error"]:
        print(f"{report['error']['code']}：{report['error']['message']}")
    stats = report["statistics"]
    if report["architecture"] == "multi":
        print("节点轨迹：" + " → ".join(report["node_trace"]))
    print(
        f"模型调用：{stats['model_calls']}；工具调用：{stats['tool_calls']}"
        f"（含代码复算 {stats['validation_tool_calls']}）；schema 修复：{stats['schema_repairs']}"
    )
    if report["schema_version"] == "review-run-v1":
        print(f"共享返工：{report['repair_count']}；人工确认记录：{len(report['confirmations'])}")
        pending = report["pending_input"]
        if pending:
            print(f"等待 {pending['kind']}：{pending['pending_id']}；仅同进程可恢复。")
            print("使用 --interactive 同进程继续；跨进程恢复请使用 durable / parallel / single。")
            print(f"待确认草稿：{report['proposal']['customer_reply_draft']}")
            for question in pending["questions"]:
                print(f"待补资料：{question['field']} | {question['question']}")
            for binding in pending["actions"]:
                candidate = binding["candidate"]
                print(
                    f"待确认动作：{binding['action_id']} | {candidate['type']} | "
                    f"{candidate['order_id']} | 金额 {candidate['amount_cents']} 分"
                )
        if report["graph_state"]["reason"]:
            print(f"转人工原因：{report['graph_state']['reason']}")
    if report["schema_version"] in {
        "persistent-run-v1",
        "parallel-run-v1",
        "single-review-run-v1",
    }:
        print(
            f"业务状态：{report['business_status']}；已提交动作：{len(report['executed_actions'])}"
        )
        for receipt in report["executed_actions"]:
            print(f"动作账本：{receipt['operation_key']} | {receipt['business_record_id']}")
        if report["pending_input"]:
            print(
                f"待办：{report['pending_input']['pending_id']}；"
                f"可用 resume --run {report['run_id']} 继续。"
            )
        if report["status"] == "interrupted":
            print(f"可恢复中断：resume --run {report['run_id']}")
    else:
        print("全部动作均为候选；确认记录不代表业务执行；执行动作数：0。")
    print(f"运行记录：{report['artifact_path']}")


def _business_command(args: argparse.Namespace, settings: Settings) -> object:
    if args.command == "eval":
        from after_sales.evaluation.runner import (
            PriceSnapshot,
            default_output,
            evaluate,
            score_saved,
        )

        output = args.output_dir or default_output()
        price = (
            PriceSnapshot.model_validate_json(args.price_config.read_text()).model_dump(mode="json")
            if args.price_config
            else None
        )
        if args.operation == "score":
            if args.observations is None:
                raise ValueError("eval score requires --observations")
            return score_saved(
                args.suite_root.resolve(), args.observations.resolve(), output, price=price
            )
        return asyncio.run(
            evaluate(
                args.suite_root.resolve(),
                output,
                settings,
                split=args.split,
                architectures=tuple(args.architectures),
                repetitions=args.repetitions,
                case_ids=tuple(args.case_ids),
                browser=args.browser,
                mode=args.model,
                price=price,
            )
        )
    repository = BusinessRepository(settings.business_db_path)
    if args.command in {"run", "resume"} and args.output and args.output.exists():
        raise FileExistsError("result file already exists; choose a new --output")
    if args.command == "live-smoke":
        return {
            "status": "skipped",
            "code": "LIVE_PROVIDER_DEFERRED",
            "model_calls": 0,
            "reason": "当前使用离线脚本模型；真实模型适配器与连通性验证尚未实现。",
        }
    if args.command == "report":
        report = REPORT_ADAPTER.validate_json(args.path.read_text(encoding="utf-8")).model_dump(
            mode="json"
        )
        return report["events"] if args.events_only else report
    if args.command == "run":
        if args.workflow in {"reviewed", "durable", "parallel"} and args.architecture != "multi":
            raise ValueError("reviewed/durable 工作流要求 --architecture multi")
        if args.workflow == "single" and args.architecture != "single":
            raise ValueError("single 工作流要求 --architecture single")
        if args.interactive and (
            args.workflow not in {"reviewed", "durable", "parallel", "single"} or args.json
        ):
            raise ValueError("--interactive 要求 reviewed/durable，且不能与 --json 同用")
        if args.run_id and args.workflow not in {"durable", "parallel", "single"}:
            raise ValueError("--run-id 要求 --workflow durable")
        runner = run_multi if args.architecture == "multi" else run_baseline
        if args.workflow == "reviewed":
            runner = run_interactive if args.interactive else run_reviewed
        if args.workflow in {"durable", "parallel", "single"}:
            report = asyncio.run(_durable_command(repository, settings, args))
        else:
            report = asyncio.run(
                runner(repository, args.ticket, settings, mode=args.model or settings.model_mode)
            )
        path = args.output or Path("var/runs") / f"{report['run_id']}.json"
        report["artifact_path"] = str(path.resolve())
        save_run(report, path)
        return report
    if args.command == "resume":
        report = asyncio.run(_durable_command(repository, settings, args))
        from uuid import uuid4

        path = args.output or Path("var/runs") / f"{report['run_id']}-{uuid4().hex[:8]}.json"
        report["artifact_path"] = str(path.resolve())
        save_run(report, path)
        return report
    if args.command == "cancel":
        return WorkflowService(repository, settings).cancel(args.run)
    if args.command == "inspect":
        session = ToolSession.for_ticket(
            repository,
            args.ticket,
            timeout_seconds=settings.tool_timeout_seconds,
            max_result_bytes=settings.tool_max_result_bytes,
        )
        try:
            return asyncio.run(inspect_ticket(session))
        finally:
            session.close()
    if args.command == "seed":
        return seed_demo(settings.business_db_path, reset=args.reset)
    if args.command == "db":
        return {"ok": True, "schema_version": migrate(settings.business_db_path)}
    if args.command == "ticket":
        if args.operation == "list":
            return [
                ticket.model_dump(mode="json") for ticket in repository.list_tickets(args.customer)
            ]
        return repository.ticket_view(args.ticket_id)
    if args.command == "order":
        return repository.get_order(args.order_id, customer_id=args.customer).model_dump(
            mode="json"
        )
    if args.operation == "show":
        return repository.get_policy(args.policy_id, args.version).model_dump(mode="json")
    as_of = None
    if not args.all_versions:
        value = args.as_of or repository.metadata()["as_of_time"]
        as_of = TypeAdapter(UtcTime).validate_python(value)
    intent = TicketType(args.intent) if args.intent else None
    return [
        policy.model_dump(mode="json")
        for policy in repository.list_policies(
            intent=intent,
            as_of_time=as_of,
        )
    ]


async def _durable_command(repository, settings, args):
    service = WorkflowService(repository, settings)
    if args.command == "resume":
        run = await service.load(args.run)
    else:
        run = await service.create(
            args.ticket,
            workflow=args.workflow,
            mode=args.model or settings.model_mode,
            run_id=args.run_id,
        )
    try:
        report = await run.recover() if args.command == "resume" else await run.start()
        if args.command == "resume" and args.response_file:
            report = await run.resume(json.loads(args.response_file.read_text(encoding="utf-8")))
        if (args.command == "resume" and not args.json and not args.response_file) or (
            args.command == "run" and args.interactive
        ):
            report = await interact(run, report)
        return report
    finally:
        await run.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        if args.command == "demo":
            from after_sales.demo import run_demo

            run_demo(port=args.port, data_dir=args.data_dir)
            return 0
        settings = (
            Settings(_env_file=None, model_mode="scripted")
            if args.command == "eval"
            else Settings()
        )
        if args.command == "doctor":
            report = build_report(settings)
        else:
            result = _business_command(args, settings)
            if args.command in {"run", "resume"} and not args.json:
                _render_run(result)
            elif args.command == "inspect" and not args.json:
                _render_inspection(result)
            elif args.command == "ticket" and args.operation == "show" and not args.json:
                _render_ticket(result)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.command == "eval" and (
                result.get("critical_error_total", 0)
                or not result.get("safety_observation_complete", True)
            ):
                return 1
            if args.command in {"run", "resume"} and result["status"] in {
                "failed",
                "validation_failed",
            }:
                return 1
            if (
                args.command in {"run", "resume"}
                and result["status"] == "handed_off"
                and result["error"]
            ):
                return 1
            return 0
    except ValidationError as exc:
        errors = [
            {"field": ".".join(map(str, item["loc"])) or "settings", "message": item["msg"]}
            for item in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        if args.json:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        else:
            print("输入或配置校验失败：", file=sys.stderr)
            for error in errors:
                print(f"- {error['field']}: {error['message']}", file=sys.stderr)
        return 2
    except (RepositoryError, sqlite3.DatabaseError, OSError, ValueError) as exc:
        error = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        if args.json:
            print(json.dumps(error, ensure_ascii=False))
        else:
            print(f"操作失败：{error['message']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"本地检查：{'通过' if report['ok'] else '失败'}（v{report['version']}）")
        print(f"Python: {report['python']}；运行模式: {report['model_mode']}")
        for name, installed_version in report["packages"].items():
            print(f"  {name}: {installed_version}")
        print("未发送模型请求。可用 seed 初始化资料，再用 run 执行离线单 / 多 Agent。")
    return 0 if report["ok"] else 1
