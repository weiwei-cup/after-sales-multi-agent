"""Small command-line entry point, extended one phase at a time."""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError

from after_sales.config import Settings
from after_sales.doctor import build_report
from after_sales.domain.models import TicketType, UtcTime
from after_sales.repositories.errors import RepositoryError
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository, migrate
from after_sales.tools.inspection import inspect_ticket
from after_sales.tools.service import ToolSession


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出结构化结果（时间为 UTC）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="after-sales", description="售后工单学习项目")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="检查本地配置与依赖，不调用模型")
    _json_flag(doctor)

    inspection = commands.add_parser("inspect", help="用只读工具调查工单、计算规则和生成证据")
    inspection.add_argument("--ticket", required=True, help="当前模拟工单 ID")
    _json_flag(inspection)

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
        "--customer", required=True, help="演示身份；P02 工具将从可信上下文注入"
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
    print(f"工单 {report['ticket_id']} | P02 只读调查")
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
    print(f"证据快照：{len(report['evidence'])}；本轮保存在调查会话内存，--json 可导出。")


def _business_command(args: argparse.Namespace, settings: Settings) -> object:
    repository = BusinessRepository(settings.business_db_path)
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        settings = Settings()
        if args.command == "doctor":
            report = build_report(settings)
        else:
            result = _business_command(args, settings)
            if args.command == "inspect" and not args.json:
                _render_inspection(result)
            elif args.command == "ticket" and args.operation == "show" and not args.json:
                _render_ticket(result)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
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
    except (RepositoryError, sqlite3.DatabaseError) as exc:
        error = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        if args.json:
            print(json.dumps(error, ensure_ascii=False))
        else:
            print(f"操作失败：{error['message']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"本地检查：{'通过' if report['ok'] else '失败'}（{report['phase']}）")
        print(f"Python: {report['python']}；运行模式: {report['model_mode']}")
        for name, installed_version in report["packages"].items():
            print(f"  {name}: {installed_version}")
        print("未发送模型请求。工单数据和 Agent 功能将按计划逐阶段实现。")
    return 0 if report["ok"] else 1
