"""Small command-line entry point, extended one phase at a time."""

import argparse
import json
import sys

from pydantic import ValidationError

from after_sales.config import Settings
from after_sales.doctor import build_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="after-sales", description="售后工单学习项目")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="检查本地配置与依赖，不调用模型")
    doctor.add_argument("--json", action="store_true", help="输出结构化检查结果")
    args = parser.parse_args(argv)

    try:
        report = build_report(Settings())
    except ValidationError as exc:
        errors = [
            {"field": ".".join(map(str, item["loc"])) or "settings", "message": item["msg"]}
            for item in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        if args.json:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        else:
            print("配置检查失败：", file=sys.stderr)
            for error in errors:
                print(f"- {error['field']}: {error['message']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"本地检查：{'通过' if report['ok'] else '失败'}（P00）")
        print(f"Python: {report['python']}；运行模式: {report['model_mode']}")
        for name, installed_version in report["packages"].items():
            print(f"  {name}: {installed_version}")
        print("未发送模型请求。工单数据和 Agent 功能将按计划逐阶段实现。")
    return 0 if report["ok"] else 1
