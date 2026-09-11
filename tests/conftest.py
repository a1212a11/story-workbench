"""共享 fixture：临时项目、固定模型客户端。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from story_workbench.application import authoring
from story_workbench.application.projects import Project, create_project
from story_workbench.infrastructure.provider import (
    CallOptions,
    CancelSignal,
    ChatMessage,
    CompleteResult,
    EventKind,
    StreamEvent,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "novel"


@pytest.fixture
def project(project_dir: Path) -> Iterator[Project]:
    proj = create_project(project_dir, "井下回声")
    try:
        yield proj
    finally:
        proj.close()


@pytest.fixture
def chapter(project: Project):
    return authoring.create_chapter(project, 1)


@pytest.fixture
def author_message(project: Project, chapter) -> str:
    return authoring.record_message(project, "第一章写沈砚调查旧井。", chapter.id)


def fixed_stream(chunks: list[str], *, truncated: bool = False):
    """返回一个固定输出的 stream 客户端函数。"""

    def _stream(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> Iterator[StreamEvent]:
        for chunk in chunks:
            if cancel.is_set():
                yield StreamEvent(EventKind.ERROR, error="cancelled")
                return
            yield StreamEvent(EventKind.TEXT, text=chunk)
        yield StreamEvent(EventKind.USAGE, usage={"input_tokens": 1, "output_tokens": len(chunks)})
        yield StreamEvent(EventKind.TRUNCATED if truncated else EventKind.COMPLETED)

    return _stream


def fixed_complete(text: str, *, truncated: bool = False):
    """返回一个固定输出的 complete 客户端函数。"""

    def _complete(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> CompleteResult:
        return CompleteResult(
            text=text, usage={"input_tokens": 1, "output_tokens": 1}, truncated=truncated
        )

    return _complete
