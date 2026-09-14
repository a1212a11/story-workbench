"""G05 真实调用验证：显式开启且设置所选供应商密钥时执行，会产生费用。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from story_workbench.application import authoring, generation
from story_workbench.bootstrap import build_clients, build_generation_config, load_settings
from story_workbench.domain.records import Expected, RunStatus, Target
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import CancelSignal, ChatMessage, EventKind

pytestmark = pytest.mark.skipif(
    os.environ.get("STORY_WORKBENCH_LIVE") != "1", reason="需要 STORY_WORKBENCH_LIVE=1"
)


def test_live_stream_complete_and_cancel():
    settings = load_settings()
    if not os.environ.get(settings.api_key_env, "").strip():
        pytest.skip(f"需要 {settings.api_key_env}")
    config = build_generation_config(settings)
    clients = build_clients(settings)
    assert clients is not None
    options = config.options
    messages = [
        ChatMessage("system", "你是一位中文小说助手。只用中文回答。"),
        ChatMessage("user", "用两句话描写一口古井，不超过 60 字。"),
    ]
    # 流式
    started = time.monotonic()
    events = list(clients.stream(messages, options, CancelSignal()))
    elapsed = time.monotonic() - started
    kinds = [e.kind for e in events]
    assert kinds[-1] is EventKind.COMPLETED
    assert EventKind.USAGE in kinds
    text = "".join(e.text for e in events if e.kind is EventKind.TEXT)
    assert text.strip()
    usage = next(e.usage for e in events if e.kind is EventKind.USAGE)
    print(f"\n[live stream] {elapsed:.1f}s usage={usage} text={text!r}")
    # 非流式
    result = clients.complete(messages, options, CancelSignal())
    assert result.text.strip() and not result.truncated
    print(f"[live complete] usage={result.usage}")
    # 取消：第一块后置位
    cancel = CancelSignal()
    seen = []
    for e in clients.stream(messages, options, cancel):
        seen.append(e.kind)
        if e.kind is EventKind.TEXT:
            cancel.set()
    assert seen[-1] is EventKind.ERROR


def test_live_inspection_evidence_locations(project, chapter, author_message):
    settings = load_settings()
    clients = build_clients(settings)
    if clients is None:
        pytest.skip(f"需要 {settings.api_key_env}")
    sample = json.loads(
        (Path(__file__).parent / "fixtures" / "deepseek-inspection-offsets.json").read_text()
    )
    body = sample["body"]
    authoring.save_plan(
        project,
        chapter.id,
        "沈砚来到井边观察井沿",
        "沈砚仍站在井边",
        Expected(),
        author_message,
    )
    saved = authoring.save_draft(project, chapter.id, body, Expected())
    started = time.monotonic()
    check_id = generation.check_draft(
        project,
        Target(project.novel_id, chapter.id, saved.version_id),
        author_message,
        clients,
        build_generation_config(settings),
    )
    facts = database.list_candidate_facts(project.conn, project.novel_id, check_id)
    assert facts
    assert all(body[f.evidence_start : f.evidence_end] == f.quote for f in facts)
    check = database.get_check(project.conn, project.novel_id, check_id)
    run = database.get_run(project.conn, project.novel_id, check.run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert authoring.get_chapter(project, chapter.id).adopted_id is None
    print(
        f"\n[live inspect] {time.monotonic() - started:.2f}s "
        f"facts={len(facts)} issues={len(check.issues)} usage={run.usage_json}"
    )
