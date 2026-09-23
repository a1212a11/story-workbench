"""配色、主题与中文换行：界面库的切词补丁、调色板、Textual 主题。"""

from __future__ import annotations

import re

import rich._wrap as rich_wrap
import textual._wrap as textual_wrap
from textual.theme import Theme

# 界面库按空格切词换行：一整段没有空格的中文会被当成一个词整体挪行，出现「1.」或「⏺」
# 单独占一行。把每个中日韩字符（连同紧跟的收尾标点）当成一个可断的词，就能在字间换行。
_CJK = "\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef"
_CLOSING = "，。、；：！？）」』》〉】…—"
CJK_CHUNK = re.compile(rf"[{_CJK}][{_CLOSING}]*\s*|\S+?(?=[{_CJK}])\s*|\S+\s*|\s+")
# Static 的纯文本走的是 Rich 的切词（textual.content 用 rich._wrap.divide_line），
# 形状是「前导空白 + 词 + 尾随空白」。
CJK_WORD = re.compile(rf"\s*(?:[{_CJK}][{_CLOSING}]*|\S+?(?=[{_CJK}])|\S+)\s*")


def install_cjk_wrapping() -> None:
    textual_wrap.re_chunk = CJK_CHUNK
    rich_wrap.re_word = CJK_WORD


# 配色：背景与正文跟着终端走（和 Claude Code 一样），只用少数几个颜色分辨状态：
# Claude 橙 #D97757 做强调，绿黄红取 Catppuccin Mocha。
PALETTE = {
    "orange": "#D97757",  # 助手圆点、输入框边框、旋转指示、选中项
    "green": "#A6E3A1",  # 完成、新增行
    "yellow": "#F9E2AF",  # 审批卡、列表标题、七成以上的上下文
    "red": "#F38BA8",  # 出错、删除行、八成五以上的上下文
    "blue": "#89B4FA",
    "subtext": "#A6ADC8",  # diff 文件名行
    "muted": "#6C7086",  # diff 位置行
    "surface": "#313244",  # 选中项与列表边框，在深浅终端里都看得见
    "band": "#252533",  # 作者发言的底色：比深色终端的底略浅一档
}
STORY_THEME = Theme(
    name="story",
    ansi=True,  # 背景、正文、面板都用终端自己的颜色
    primary=PALETTE["orange"],
    secondary=PALETTE["blue"],
    accent=PALETTE["orange"],
    success=PALETTE["green"],
    warning=PALETTE["yellow"],
    error=PALETTE["red"],
    foreground="ansi_default",
    background="ansi_default",
    surface="ansi_default",
    panel="ansi_default",
    boost="ansi_default",
    dark=True,
    variables={
        "text-muted": "ansi_bright_black",
        "ansi-background": "ansi_black",
        "ansi-foreground": "ansi_white",
        "border-blurred": "ansi_black",
        "block-cursor-foreground": "ansi_black",
        "block-cursor-background": PALETTE["orange"],
        "input-cursor-background": PALETTE["orange"],
        "input-cursor-foreground": "ansi_black",
        "input-cursor-text-style": "none",
        "input-selection-background": "ansi_bright_blue",
        "input-selection-foreground": "ansi_default",
        "screen-selection-background": "ansi_bright_blue",
        "screen-selection-foreground": "ansi_black",
    },
)
