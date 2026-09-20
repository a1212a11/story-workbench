"""启动入口：解析参数、装配模型客户端、进入全屏对话界面。

用法：uv run python -m story_agent <作品目录>
目录不存在或还不是作品时按目录名新建；书名可以在对话里改。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from story_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="story_agent",
        description="打开一个作品目录，和代理一起写小说。",
    )
    parser.add_argument("project", nargs="?", type=Path, help="作品目录；不存在时按目录名新建")
    parser.add_argument("--version", action="version", version=f"story_agent {__version__}")
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
        print("需要在交互终端里运行。", file=sys.stderr)
        print(f"作品目录：{args.project}", file=sys.stderr)
        return 2

    from story_agent import config as cfg
    from story_agent import tui

    try:
        settings = cfg.load_settings()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    agent_config = cfg.build_agent_config(settings)
    clients = cfg.build_clients(settings)
    if clients is None:
        print(
            f"未检测到 {settings.api_key_env}，本次不接模型；写作与审读将提示尚未接入。",
            file=sys.stderr,
        )
    else:
        print(f"本次模型：{settings.provider} / {settings.model}")
    return tui.run(args.project, clients=clients, config=agent_config)


if __name__ == "__main__":
    sys.exit(main())
