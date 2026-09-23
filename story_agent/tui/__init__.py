"""Textual 全屏对话界面。

theme 配色与中文换行，text 纯文本小工具，widgets 控件，port 代理线程端口，app 应用本体。
"""

from story_agent.tui.app import WorkbenchApp, run
from story_agent.tui.text import (
    _duration,
    author_line,
    clean_markdown,
    colorize_diff,
    context_gauge,
    copy_text,
    model_label,
)
from story_agent.tui.theme import PALETTE, STORY_THEME, install_cjk_wrapping
from story_agent.tui.widgets import EditorScreen, PromptArea, ThinkingBlock, ToolBlock

install_cjk_wrapping()

__all__ = [
    "PALETTE",
    "STORY_THEME",
    "EditorScreen",
    "PromptArea",
    "ThinkingBlock",
    "ToolBlock",
    "WorkbenchApp",
    "_duration",
    "author_line",
    "clean_markdown",
    "colorize_diff",
    "context_gauge",
    "copy_text",
    "model_label",
    "run",
]
