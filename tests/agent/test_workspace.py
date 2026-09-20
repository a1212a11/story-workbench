"""作品目录：初始化、路径安全、正式/自由判定、读写搜索、git 历史与降级、对话记录。"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest

from story_agent import workspace as ws_mod
from story_agent.workspace import (
    Git,
    Workspace,
    WorkspaceError,
    chapter_file,
    describe_time,
    is_formal,
    normalize,
)


def test_create_lays_out_directory_skills_and_first_commit(workspace: Workspace):
    root = workspace.root
    for name in ("设定", "人物", "章节", "草稿", "技能", "对话", ".story"):
        assert (root / name).is_dir(), name
    for name in ("作品.md", "大纲.md", "故事状态.md", "决定.md", ".gitignore"):
        assert (root / name).is_file(), name
    assert workspace.title == "井下回声"
    skills = {p.name for p in (root / "技能").glob("*.md")}
    assert skills == {
        "修订.md",
        "写章.md",
        "审读.md",
        "回改.md",
        "定章.md",
        "开书.md",
        "故事状态.md",
    }
    history = workspace.history()
    assert len(history) == 1 and history[0].message == "开书：《井下回声》"
    with pytest.raises(WorkspaceError):
        Workspace.create(root, "再来一次")


def test_open_requires_work_file_and_keeps_author_skill_edits(tmp_path, workspace):
    with pytest.raises(WorkspaceError):
        Workspace.open(tmp_path / "nowhere")
    workspace.write_text("技能/写章.md", "# 我自己的写法\n")
    reopened = Workspace.open(workspace.root)
    assert reopened.read_text("技能/写章.md") == "# 我自己的写法\n"


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../x.md", "章节/../../x", ".git/config", ".story/a", "~/a"]
)
def test_normalize_rejects_escapes_and_hidden_dirs(bad):
    with pytest.raises(WorkspaceError):
        normalize(bad)


def test_normalize_and_formal_classification():
    assert normalize("./章节//第01章.md") == "章节/第01章.md"
    assert normalize("章节\\第01章.md") == "章节/第01章.md"
    for formal in (
        "作品.md",
        "大纲.md",
        "决定.md",
        "设定/世界.md",
        "人物/沈砚.md",
        "章节/第01章.md",
    ):
        assert is_formal(formal), formal
    for free in ("故事状态.md", "草稿/试写.md", "技能/写章.md", "对话/2026-09-20.md"):
        assert not is_formal(free), free
    assert chapter_file(3) == "章节/第03章.md"


def test_read_write_search_and_chapters(workspace: Workspace):
    workspace.write_text("章节/第01章.md", "# 第一章\n\n井底有一具尸体。\n")
    workspace.write_text("章节/第02章.md", "# 第二章\n\n他回到井边。\n")
    workspace.write_text("草稿/备选.md", "井底什么也没有。\n")
    assert workspace.chapters() == [(1, "章节/第01章.md"), (2, "章节/第02章.md")]
    hits = workspace.search("井底")
    assert [(h.path, h.line) for h in hits] == [("章节/第01章.md", 3), ("草稿/备选.md", 1)]
    assert [h.path for h in workspace.search("井底", "章节")] == ["章节/第01章.md"]
    assert [h.line for h in workspace.search(r"第[一二]章", "章节", regex=True)] == [1, 1]
    assert "章节/" in workspace.list_dir() and "第01章.md" in workspace.list_dir("章节")
    with pytest.raises(WorkspaceError):
        workspace.read_text("章节/第09章.md")
    with pytest.raises(WorkspaceError):
        workspace.search("")
    assert not list(workspace.root.glob("章节/.tmp-*"))


def test_commit_history_show_in_plain_words(workspace: Workspace):
    workspace.write_text("章节/第01章.md", "# 第一章\n\n初稿。\n")
    ref1 = workspace.commit("第一章 · 初稿", "章节/第01章.md")
    workspace.write_text("章节/第01章.md", "# 第一章\n\n改过的稿。\n")
    ref2 = workspace.commit("第一章 · 改了开头", "章节/第01章.md")
    assert ref1 and ref2 and ref1 != ref2
    assert workspace.commit("没有变化", "章节/第01章.md") is None
    entries = workspace.history("章节/第01章.md")
    assert [e.message for e in entries] == ["第一章 · 改了开头", "第一章 · 初稿"]
    assert entries[0].files == ("章节/第01章.md",)
    assert describe_time(entries[0].when).startswith("今天 ")
    assert workspace.show("章节/第01章.md", ref1) == "# 第一章\n\n初稿。\n"
    assert workspace.show("章节/第01章.md", "0000000") is None
    with pytest.raises(WorkspaceError):
        workspace.show("章节/第01章.md", "not-a-ref")


def test_describe_time_words():
    now = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    assert describe_time(now - timedelta(hours=1), now) == "今天 21:00"
    assert describe_time(now - timedelta(days=1), now) == "昨天 22:00"
    assert describe_time(datetime(2026, 3, 2, 8, 5, tzinfo=UTC), now) == "3月2日 08:05"
    assert describe_time(datetime(2025, 3, 2, 8, 5, tzinfo=UTC), now) == "2025年3月2日"


def test_without_git_everything_else_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ws = Workspace.create(tmp_path / "无git", "无 git")
    assert not ws.git.available and not (ws.root / ".git").exists()
    ws.write_text("章节/第01章.md", "# 第一章\n")
    assert ws.commit("x", "章节/第01章.md") is None
    assert ws.history() == [] and ws.show("章节/第01章.md", "abcd") is None
    assert ws.read_text("章节/第01章.md") == "# 第一章\n"
    assert isinstance(ws_mod.Git(tmp_path), Git)


def test_transcript_and_usage_records(workspace: Workspace):
    when = datetime(2026, 9, 20, 9, 30, tzinfo=UTC).astimezone()
    workspace.append_transcript("author", "写第一章", when)
    workspace.append_transcript("assistant", "好。", when)
    workspace.append_transcript("system", "审批：修改 x → 批准", when)
    assert workspace.recent_transcript() == [("author", "写第一章"), ("assistant", "好。")]
    daily = workspace.read_text(f"对话/{when:%Y-%m-%d}.md")
    assert "> 写第一章" in daily and "⏺ 好。" in daily and "· 审批" in daily
    workspace.record_usage({"input_tokens": 3, "output_tokens": 4}, "fixed", "对话")
    assert '"input_tokens": 3' in (workspace.root / ".story" / "用量.jsonl").read_text()
    assert ".story" not in workspace.list_dir()
