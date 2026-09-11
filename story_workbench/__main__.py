"""启动入口：解析参数、装配项目资源、退出与清理。

首期只提供 --help 和项目目录参数校验；作品交互入口随 C 阶段接入。
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
    print("作者交互入口尚未接入；当前只完成参数解析。", file=sys.stderr)
    print(f"项目目录：{args.project}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
