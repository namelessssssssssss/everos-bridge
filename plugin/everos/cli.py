"""手动检索 EverOS 记忆（纯标准库，配置与 MCP 共用）。"""
from __future__ import annotations

import argparse
import math
import sys


def _query(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 500:
        raise argparse.ArgumentTypeError("检索问题须为 1–500 字")
    return value


def _top_k(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("top-k 须为 -1 或 1–100 的整数") from None
    if number != -1 and not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("top-k 须为 -1 或 1–100 的整数")
    return number


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout 须为正有限秒数") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout 须为正有限秒数")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="everos", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="按问题检索长期记忆")
    search.add_argument("query", type=_query, help="检索问题；含空格时用引号包住（最多 500 字）")
    search.add_argument("--kind", choices=("all", "episodes", "practices"), default="all",
                        help="事件、做法或两者（默认 all）")
    search.add_argument("--top-k", type=_top_k, default=5, metavar="N",
                        help="每类记忆的召回上限：1–100；-1 由服务端决定（默认 5）")
    search.add_argument("--method", choices=("hybrid", "agentic"), default="hybrid",
                        help="检索方法（默认 hybrid；agentic 默认总预算 180 秒，含回退）")
    search.add_argument("--timeout", type=_timeout, metavar="SECONDS",
                        help="本次检索总预算（秒，含回退），优先于 EVEROS_AGENTIC_TIMEOUT 配置")
    args = parser.parse_args(argv)

    # Help and argument validation work without credentials or API access.
    import core

    try:
        if not core.ready():
            raise RuntimeError("未配置：设置 EVEROS_USER_ID 与 EVEROS_API_KEY，"
                               "或写入 $EVEROS_HOME/config.env（默认 ~/.everos/config.env）")
        options = {"method": args.method, "strict": True}
        if args.timeout is not None:
            options["budget"] = args.timeout
        text = core.search_text(args.query, args.top_k, args.kind, **options)
        print(text or "（没有相关记忆）")
    except KeyboardInterrupt:
        print("EverOS 检索已取消", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"EverOS 检索失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
