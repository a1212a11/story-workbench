"""发给模型的固定文案：人设、开场、压缩、审读、重写，各是本目录里的一份 Markdown。

文案只读不填：文件里没有占位符，动态内容（书名、目录树、摘要……）由调用方拼在后面。
所以文件里可以放 JSON 示例这类带花括号的东西，改文案不用碰代码。
本模块不 import 包内任何模块。
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


@cache
def load(name: str) -> str:
    """读一份文案，例如 load("人设")；首尾空白去掉。"""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
