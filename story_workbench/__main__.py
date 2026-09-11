"""启动入口：解析参数、装配终端输入与项目资源、退出与清理。

作者交互见 cli.run；此处只负责确认终端可用并把项目目录交给 CLI。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from story_workbench import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="story_workbench",
        description="打开一个本地小说项目目录，进入创作工作台。",
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        help="小说项目目录；不存在时按新项目初始化",
    )
    parser.add_argument("--version", action="version", version=f"story_workbench {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.project is None:
        build_parser().print_help()
        return 0
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        interactive = False
    if not interactive:
        print("创作工作台需要在交互终端里运行。", file=sys.stderr)
        print(f"项目目录：{args.project}", file=sys.stderr)
        return 2

    from story_workbench import cli

    return cli.run(args.project)


if __name__ == "__main__":
    sys.exit(main())
