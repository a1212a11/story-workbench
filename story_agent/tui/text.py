"""纯文本小工具：Markdown 清洗、diff 着色、上下文小条、时长与数字的写法、剪贴板。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from rich.text import Text
from textual.app import App

from story_agent.config import AgentConfig
from story_agent.tui.theme import PALETTE

_PUNCT_OPEN = "“「『‘（(《〈【"
_PUNCT_CLOSE = "”」』’）)》〉】，。、；：！？…"
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def clean_markdown(text: str) -> str:
    """让模型写的 Markdown 在终端里干净：

    - `**` 紧挨着中文标点时按 CommonMark 不算加粗，会原样露出来；把标点挪到 `**` 外面。
    - 单独一行的分隔线去掉，聊天里不需要。
    """

    def fix(match: re.Match) -> str:
        inner = match.group(1)
        lead = ""
        while inner and inner[0] in _PUNCT_OPEN:
            lead, inner = lead + inner[0], inner[1:]
        tail = ""
        while inner and inner[-1] in _PUNCT_CLOSE:
            tail, inner = inner[-1] + tail, inner[:-1]
        return f"{lead}**{inner}**{tail}" if inner.strip() else lead + inner + tail

    lines = []
    for line in text.split("\n"):
        if re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", line):
            continue
        lines.append(_BOLD.sub(fix, line))
    return "\n".join(lines)


GAUGE_CELLS = 10


def author_line(text: str) -> Text:
    """作者说的话：正文用终端的正常字色，只有开头的 > 用强调色，好和助手的回复区分开。"""
    return Text.assemble(("> ", PALETTE["orange"]), text)


def colorize_diff(text: str) -> Text:
    """把统一 diff 着色：删除行红、新增行绿、位置行暗；其他文字原样。"""
    result = Text()
    for index, line in enumerate(text.split("\n")):
        if index:
            result.append("\n")
        if line.startswith("+++") or line.startswith("---"):
            result.append(line, style=f"bold {PALETTE['subtext']}")
        elif line.startswith("+"):
            result.append(line, style=PALETTE["green"])
        elif line.startswith("-"):
            result.append(line, style=PALETTE["red"])
        elif line.startswith("@@"):
            result.append(line, style=PALETTE["muted"])
        else:
            result.append(line)
    return result


def context_gauge(used: int, limit: int) -> tuple[str, float]:
    """上下文占用的十格小条与比例，例如「▮▮▮▮▯▯▯▯▯▯ 42%」。"""
    if limit <= 0:
        return "", 0.0
    ratio = min(used / limit, 1.0)
    filled = round(ratio * GAUGE_CELLS)
    bar = "▮" * filled + "▯" * (GAUGE_CELLS - filled)
    return f"{bar} {ratio * 100:.0f}%", ratio


def copy_text(app: App, text: str) -> None:
    """复制到系统剪贴板：优先用本机命令，拿不到就退回终端的 OSC 52。"""
    for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(command, input=text, text=True, check=True, timeout=5)
            return
        except (OSError, subprocess.SubprocessError):
            break
    app.copy_to_clipboard(text)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} 分 {rest} 秒"


def _short_path(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def model_label(config: AgentConfig) -> str:
    """状态栏里的模型名：写作模型不同时显示「对话 ▸ 写作」。"""
    if config.writing_options is None:
        return config.options.model
    return f"{config.options.model} ▸ {config.writing.model}"


def _short(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)
