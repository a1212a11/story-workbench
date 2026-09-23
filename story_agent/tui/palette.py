"""输入框上方的选项面板：斜杠命令、代理的提问选项、撤回列表共用这一块。

面板只管列什么、高亮哪一项、怎么画；选中之后做什么由应用决定（回答代理、撤回、执行命令）。
"""

from __future__ import annotations

from typing import Any

from textual.containers import Vertical
from textual.widgets import Static


class CommandPalette(Vertical):
    def __init__(self) -> None:
        super().__init__(id="palette")
        self.mode = "commands"  # commands / choice / rewind / rewind-scope
        self.items: list[tuple[str, str]] = []  # 每一项：（名字，说明）
        self.payload: list[Any] = []  # 与 items 一一对应的值；命令模式不用
        self.index = 0
        self.title = ""

    @property
    def is_open(self) -> bool:
        return bool(self.items) and self.display

    @property
    def selected_payload(self) -> Any:
        return self.payload[self.index]

    @property
    def selected_name(self) -> str:
        return self.items[self.index][0]

    def show(self, mode: str, items: list[tuple[str, str]], payload: list, title: str = "") -> None:
        self.mode = mode
        self.items = items
        self.payload = payload
        self.index = 0
        self.title = title
        self.display = True
        self._redraw()

    def close(self) -> None:
        self.items = []
        self.payload = []
        self.mode = "commands"
        self.title = ""
        self.display = False

    def filter_commands(self, text: str, commands: tuple[tuple[str, str], ...]) -> None:
        """命令模式：输入以 / 开头的一行时，列出名字对得上的命令。"""
        head = text.split("\n", 1)[0].strip().lower()
        items: list[tuple[str, str]] = []
        if text.startswith("/") and "\n" not in text:
            items = [c for c in commands if c[0].startswith(head)]
        if items != self.items:
            self.items = items
            self.index = 0
        self.display = bool(items)
        if items:
            self._redraw()

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)
        self._redraw()

    def _redraw(self) -> None:
        self.remove_children()
        if self.title:
            self.mount(Static(self.title, classes="palette-title", markup=False))
        for index, (name, description) in enumerate(self.items):
            classes = "palette-item selected" if index == self.index else "palette-item"
            marker = "❯" if index == self.index else " "
            line = f"{marker} {name:<7} {description}".replace("\n", " ")
            self.mount(Static(line, classes=classes, markup=False))
