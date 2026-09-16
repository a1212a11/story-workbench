"""作者端口：界面与流程之间的最小合同。

流程只通过这四个方法与作者交互，加上一个执行模型任务的入口；因此旧命令行、对话式界面和
代理工具可以共用同一套门槛流程，而不各自复制规则。
"""

from __future__ import annotations

from typing import Any, Protocol


class AuthorPort(Protocol):
    def say(self, text: str) -> None:
        """输出整行文字。"""

    def emit(self, chunk: str) -> None:
        """输出流式片段，不换行。"""

    def ask(self, prompt: str) -> str:
        """向作者提问并等待一行回答；回车为空串。"""

    def edit(self, initial: str, title: str) -> str | None:
        """打开正文编辑器；保存返回新文本，取消返回 None。"""

    def run_model(self, project: Any, clients: Any, config: Any, operation: Any, *args, **kwargs):
        """执行一个需要模型客户端的用例，负责取消与等待。"""
