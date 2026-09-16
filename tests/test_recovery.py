"""文件、重启、备份与恢复流程。当前覆盖 S01 的锁、布局与中断标记，S03 的外部文件对账。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from story_workbench.application import authoring
from story_workbench.application.projects import (
    create_workcopy,
    export_version,
    open_project,
    reconcile_workcopy,
    write_recovery,
)
from story_workbench.domain.records import (
    ErrorCode,
    Expected,
    ReconcileOutcome,
    VersionOrigin,
    WorkbenchError,
)
from story_workbench.infrastructure import database, files


def test_layout_and_lock_released_on_close(project, project_dir):
    for name in (files.WORK_DIR, files.EXPORTS_DIR, files.RECOVERY_DIR):
        assert (project_dir / name).is_dir()
    assert (project_dir / files.DB_FILENAME).exists()
    assert project.lock.held
    project.close()
    assert not project.lock.held
    reopened, _ = open_project(project_dir)
    reopened.close()


def test_unfinished_runs_marked_interrupted_on_reopen(
    project, project_dir, chapter, author_message
):
    with database.transaction(project.conn):
        project.conn.execute(
            "INSERT INTO run (id, novel_id, created_at, chapter_id, task, source_message_id,"
            " status, template_version) VALUES (?, ?, ?, ?, 'generate', ?, 'running', 'v0')",
            ("run1", project.novel_id, database.now_utc(), chapter.id, author_message),
        )
    project.close()
    reopened, notice = open_project(project_dir)
    try:
        assert notice.interrupted_run_ids == ("run1",)
        status = reopened.conn.execute("SELECT status FROM run WHERE id = 'run1'").fetchone()[0]
        assert status == "interrupted"
    finally:
        reopened.close()


def test_write_new_never_overwrites(project):
    first = files.write_new(project.root, "exports/ch1.md", "A")
    second = files.write_new(project.root, "exports/ch1.md", "B")
    assert first == "exports/ch1.md"
    assert second == "exports/ch1-1.md"
    assert (project.root / first).read_text() == "A"
    assert (project.root / second).read_text() == "B"


def test_observe_workcopy_reports_change_and_missing(project):
    obs = files.observe_workcopy(project.root, "work/ch1.md", None)
    assert not obs.exists and not obs.changed_from_baseline
    files.write_new(project.root, "work/ch1.md", "沈砚")
    obs2 = files.observe_workcopy(project.root, "work/ch1.md", None)
    assert obs2.exists and obs2.changed_from_baseline and obs2.content == "沈砚"
    obs3 = files.observe_workcopy(project.root, "work/ch1.md", obs2.content_hash)
    assert not obs3.changed_from_baseline


# ---------------------------------------------------------------- S03 外部文件对账


def _draft(project, chapter, text, base=None):

    return authoring.save_draft(project, chapter.id, text, Expected(current_draft_id=base))


def test_create_workcopy_writes_current_draft_and_is_in_sync(project, chapter):

    v = _draft(project, chapter, "沈砚把灯搁在井沿。")
    result = create_workcopy(project, chapter.id)
    assert result.outcome is ReconcileOutcome.IN_SYNC
    assert (project.root / result.relative_path).read_text() == "沈砚把灯搁在井沿。"
    assert reconcile_workcopy(project, chapter.id).outcome is ReconcileOutcome.IN_SYNC
    assert len(authoring.list_versions(project, chapter.id)) == 1
    assert authoring.get_chapter(project, chapter.id).current_draft_id == v.version_id


def test_external_edit_is_imported_as_new_current_draft(project, chapter):

    v1 = _draft(project, chapter, "原稿")
    path = project.root / create_workcopy(project, chapter.id).relative_path
    path.write_text("原稿，外部改过。")
    result = reconcile_workcopy(project, chapter.id)
    assert result.outcome is ReconcileOutcome.IMPORTED
    versions = authoring.list_versions(project, chapter.id)
    assert [v.body for v in versions] == ["原稿", "原稿，外部改过。"]
    assert versions[1].origin is VersionOrigin.EXTERNAL
    assert versions[1].parent_id == v1.version_id
    assert authoring.get_chapter(project, chapter.id).current_draft_id == versions[1].id
    # 再次对账不重复导入
    assert reconcile_workcopy(project, chapter.id).outcome is ReconcileOutcome.IN_SYNC
    assert len(authoring.list_versions(project, chapter.id)) == 2


def test_external_and_internal_edits_both_kept_without_merge(project, chapter):

    v1 = _draft(project, chapter, "原稿")
    path = project.root / create_workcopy(project, chapter.id).relative_path
    # 应用内先保存了第二稿（未导出到文件），文件里又被外部改了
    v2 = authoring.save_draft(
        project, chapter.id, "应用内第二稿", Expected(current_draft_id=v1.version_id)
    )
    path.write_text("外部第二稿")
    result = reconcile_workcopy(project, chapter.id)
    assert result.outcome is ReconcileOutcome.CONFLICT
    assert result.conflicting_current_id == v2.version_id
    bodies = [v.body for v in authoring.list_versions(project, chapter.id)]
    assert bodies == ["原稿", "应用内第二稿", "外部第二稿"]
    assert authoring.get_chapter(project, chapter.id).current_draft_id == v2.version_id
    assert path.read_text() == "外部第二稿"  # 文件未被覆盖


def test_save_draft_reconciles_first_and_keeps_author_input_on_conflict(project, chapter):

    v1 = _draft(project, chapter, "原稿")
    path = project.root / create_workcopy(project, chapter.id).relative_path
    path.write_text("外部改动")
    # 作者在应用里基于 v1 编辑并保存；外部改动先被导入，作者稿保留但不抢指针
    saved = authoring.save_draft(
        project, chapter.id, "应用内改动", Expected(current_draft_id=v1.version_id)
    )
    assert not saved.became_current
    bodies = [v.body for v in authoring.list_versions(project, chapter.id)]
    assert bodies == ["原稿", "外部改动", "应用内改动"]
    current = authoring.get_chapter(project, chapter.id).current_draft_id
    assert current == saved.conflicting_current_id
    assert (
        authoring.get_version(
            project,
            __import__("story_workbench.domain.records", fromlist=["Target"]).Target(
                project.novel_id, chapter.id, current
            ),
        ).body
        == "外部改动"
    )


def test_deleted_file_does_not_become_empty_draft(project, chapter):

    _draft(project, chapter, "原稿")
    path = project.root / create_workcopy(project, chapter.id).relative_path
    path.unlink()
    result = reconcile_workcopy(project, chapter.id)
    assert result.outcome is ReconcileOutcome.FILE_MISSING
    assert [v.body for v in authoring.list_versions(project, chapter.id)] == ["原稿"]


def test_non_utf8_file_is_reported_not_imported(project, chapter):

    _draft(project, chapter, "原稿")
    path = project.root / create_workcopy(project, chapter.id).relative_path
    path.write_bytes(b"\xff\xfe\x00bad")
    result = reconcile_workcopy(project, chapter.id)
    assert result.outcome is ReconcileOutcome.UNREADABLE
    assert len(authoring.list_versions(project, chapter.id)) == 1


def test_existing_file_at_workcopy_creation_is_imported_not_overwritten(project, chapter):

    _draft(project, chapter, "数据库稿")
    pre = project.root / "work" / "chapter-001.md"
    pre.write_text("作者早就放在这里的文件")
    result = create_workcopy(project, chapter.id)
    assert result.outcome is ReconcileOutcome.CONFLICT
    assert pre.read_text() == "作者早就放在这里的文件"
    versions = authoring.list_versions(project, chapter.id)
    assert [v.origin for v in versions] == [VersionOrigin.AUTHOR, VersionOrigin.EXTERNAL]


def test_db_save_failure_keeps_input_recoverable(project, chapter, monkeypatch):

    def boom(*args, **kwargs):
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "磁盘故障")

    monkeypatch.setattr(database, "commit_version", boom)
    with pytest.raises(WorkbenchError) as exc:
        authoring.save_draft(project, chapter.id, "作者刚写的一段", Expected())
    assert exc.value.code is ErrorCode.STORAGE_FAILED
    assert authoring.list_versions(project, chapter.id) == []
    rel = write_recovery(project, chapter.id, "作者刚写的一段")
    assert (project.root / rel).read_text() == "作者刚写的一段"
    assert rel.startswith("recovery/")


def test_export_failure_does_not_lose_saved_version(project, chapter, monkeypatch):

    v = _draft(project, chapter, "已保存正文")

    def boom(*args, **kwargs):
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "导出失败")

    monkeypatch.setattr(files, "write_new", boom)
    with pytest.raises(WorkbenchError):
        export_version(project, chapter.id, v.version_id)
    monkeypatch.undo()
    assert [x.body for x in authoring.list_versions(project, chapter.id)] == ["已保存正文"]
    rel = export_version(project, chapter.id, v.version_id)
    assert (project.root / rel).read_text() == "已保存正文"
    rel2 = export_version(project, chapter.id, v.version_id)
    assert rel2 != rel  # 不覆盖


def test_workcopy_path_rejects_escape(project):

    with pytest.raises(WorkbenchError) as exc:
        files.observe_workcopy(project.root, "../outside.md", None)
    assert exc.value.code is ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------- R03 重启恢复与阶段重试


def _three_facts_project(project_dir):
    """建一本有一章已采用的作品并关闭，返回 (chapter_id, version_id, msg_id)。"""
    from story_workbench.ai.context import ContextLimits
    from story_workbench.application import generation
    from story_workbench.application.projects import create_project
    from story_workbench.domain.records import WriteMode
    from story_workbench.infrastructure.provider import CallOptions
    from tests.conftest import fixed_complete, fixed_stream
    from tests.test_workflow import adopt_current, inspection_json

    proj = create_project(project_dir, "井下回声")
    ch = authoring.create_chapter(proj, 1)
    msg = authoring.record_message(proj, "计划", ch.id)
    authoring.save_plan(proj, ch.id, "调查旧井", "看到尸体", Expected(), msg)
    body = "沈砚下井，看到尸体。"
    config = generation.GenerationConfig(
        options=CallOptions(model="fixed", max_output_tokens=1000, timeout_seconds=5),
        limits=ContextLimits(total_tokens=20000, template_reserve=500, output_reserve=1000),
    )
    clients = generation.Clients(
        stream=fixed_stream([body]),
        complete=fixed_complete(inspection_json(body, plot="调查旧井", ending="看到尸体")),
    )
    r = generation.write_chapter(proj, ch.id, WriteMode.FORMAL, msg, clients, config)
    adopt_current(proj, ch.id, r)
    proj.close()
    return ch.id, r.version_id, msg, config


def test_interrupted_generation_is_reported_and_retry_is_new_run(project_dir):
    from story_workbench.application import generation
    from story_workbench.domain.records import RunStatus, TaskKind
    from tests.conftest import fixed_complete, fixed_stream
    from tests.test_workflow import inspection_json

    ch_id, v_id, msg, config = _three_facts_project(project_dir)
    proj, _ = open_project(project_dir)
    ch2 = authoring.create_chapter(proj, 2)
    msg2 = authoring.record_message(proj, "第二章", ch2.id)
    authoring.save_plan(proj, ch2.id, "进旧屋", "内门前", Expected(), msg2)
    # 模拟进程在生成中途退出：run 停留在 running，带部分输出
    run_id = database.commit_run(
        proj.conn,
        proj.novel_id,
        chapter_id=ch2.id,
        task=TaskKind.GENERATE,
        source_message_id=msg2,
        template_version="x",
        status=RunStatus.RUNNING,
        request_json='{"mode": "formal", "assumptions": []}',
    )
    assert database.commit_run_chunk(proj.conn, proj.novel_id, run_id, "写了一半")
    proj.close()

    reopened, notice = open_project(project_dir)
    try:
        assert notice.interrupted_run_ids == (run_id,)
        run = database.get_run(reopened.conn, reopened.novel_id, run_id)
        assert run.status is RunStatus.INTERRUPTED and run.partial_text == "写了一半"
        assert run.finished_at is not None  # 重新打开时的标记时刻
        assert authoring.list_versions(reopened, ch2.id) == []  # 未落库为版本
        # 迟到的块不能再写入中断的任务
        assert not database.commit_run_chunk(reopened.conn, reopened.novel_id, run_id, "迟到")
        # 作者重试：新 run，引用 retry_of
        body = "沈砚进了旧屋。"
        clients = generation.Clients(
            stream=fixed_stream([body]),
            complete=fixed_complete(inspection_json(body, plot="进旧屋", ending="内门前")),
        )
        result = generation.retry_run(reopened, run_id, msg2, clients, config)
        new_run = database.get_run(reopened.conn, reopened.novel_id, result.run_id)
        assert new_run.retry_of_id == run_id and new_run.status is RunStatus.SUCCEEDED
        assert (new_run.provider, new_run.model) == ("fixed", "fixed")
        assert new_run.finished_at is not None and new_run.finished_at >= new_run.created_at
        assert [v.body for v in authoring.list_versions(reopened, ch2.id)] == [body]
    finally:
        reopened.close()


def test_recompute_failure_survives_restart_and_retry_does_not_readopt(project_dir, monkeypatch):
    from story_workbench.application import adoption
    from story_workbench.domain import rules
    from story_workbench.domain.records import SnapshotStatus

    ch_id, v_id, msg, config = _three_facts_project(project_dir)
    proj, _ = open_project(project_dir)
    # 纠正一条事实使状态过时，然后让重算失败
    snap = authoring.get_chapter(proj, ch_id).current_snapshot_id
    with database.transaction(proj.conn):
        proj.conn.execute("UPDATE snapshot SET status = 'stale' WHERE id = ?", (snap,))
        proj.conn.execute("UPDATE chapter SET current_snapshot_id = NULL WHERE id = ?", (ch_id,))

    def boom(*a, **k):
        raise RuntimeError("超时")

    monkeypatch.setattr(rules, "compute_snapshot", boom)
    with pytest.raises(WorkbenchError):
        adoption.recompute_state(proj, ch_id)
    monkeypatch.undo()
    adoptions_before = proj.conn.execute("SELECT COUNT(*) FROM adoption").fetchone()[0]
    proj.close()

    reopened, _ = open_project(project_dir)
    try:
        ch = authoring.get_chapter(reopened, ch_id)
        assert ch.adopted_id is not None and ch.current_snapshot_id is None  # 采用在，状态待更新
        assert adoption.recompute_state(reopened, ch_id) is SnapshotStatus.READY
        assert (
            reopened.conn.execute("SELECT COUNT(*) FROM adoption").fetchone()[0] == adoptions_before
        )
        assert authoring.get_chapter(reopened, ch_id).current_snapshot_id is not None
    finally:
        reopened.close()


# ---------------------------------------------------------------- R04 备份与恢复


def test_backup_and_restore_round_trip_preserves_content_and_references(project_dir, tmp_path):
    from story_workbench.application.projects import backup, restore_backup

    ch_id, v_id, msg, config = _three_facts_project(project_dir)
    proj, _ = open_project(project_dir)
    try:
        wc = create_workcopy(proj, ch_id)
        export_version(proj, ch_id, v_id)
        write_recovery(proj, ch_id, "未保存的输入")
        report = backup(proj, tmp_path / "backup")
        assert report.file_count == 3
        assert not (tmp_path / "backup" / files.LOCK_FILENAME).exists()
        original = {
            "chapter": authoring.get_chapter(proj, ch_id),
            "versions": [(v.id, v.body) for v in authoring.list_versions(proj, ch_id)],
            "runs": proj.conn.execute("SELECT id, status FROM run ORDER BY id").fetchall(),
        }
    finally:
        proj.close()

    restored = restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert restored.problems == () and restored.file_count == 3
    reopened, _ = open_project(tmp_path / "restored")
    try:
        assert authoring.get_chapter(reopened, ch_id) == original["chapter"]
        assert [(v.id, v.body) for v in authoring.list_versions(reopened, ch_id)] == original[
            "versions"
        ]
        assert [
            tuple(r)
            for r in reopened.conn.execute("SELECT id, status FROM run ORDER BY id").fetchall()
        ] == [tuple(r) for r in original["runs"]]
        assert (tmp_path / "restored" / wc.relative_path).read_text() == "沈砚下井，看到尸体。"
        assert list((tmp_path / "restored" / "recovery").iterdir())
        # 原项目不受影响
        assert (project_dir / files.DB_FILENAME).exists()
    finally:
        reopened.close()
    # 不覆盖已有非空目录
    with pytest.raises(WorkbenchError) as exc:
        restore_backup(tmp_path / "backup", project_dir)
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_backup_refuses_success_when_files_change_midway(project_dir, tmp_path, monkeypatch):
    from story_workbench.application.projects import backup

    ch_id, v_id, msg, config = _three_facts_project(project_dir)
    proj, _ = open_project(project_dir)
    try:
        wc = create_workcopy(proj, ch_id)
        target = proj.root / wc.relative_path
        real_copy = files.copy_files

        def copy_then_mutate(root, destination, rel):
            out = real_copy(root, destination, rel)
            target.write_text("备份期间被外部改了")
            return out

        monkeypatch.setattr(files, "copy_files", copy_then_mutate)
        with pytest.raises(WorkbenchError) as exc:
            backup(proj, tmp_path / "backup")
        assert exc.value.code is ErrorCode.STORAGE_FAILED
        assert not (tmp_path / "backup").exists()  # 未完成的输出已清理
    finally:
        proj.close()


def test_restore_rejects_tampered_backup(project_dir, tmp_path):
    from story_workbench.application.projects import backup, restore_backup

    ch_id, v_id, msg, config = _three_facts_project(project_dir)
    proj, _ = open_project(project_dir)
    try:
        wc = create_workcopy(proj, ch_id)
        backup(proj, tmp_path / "backup")
    finally:
        proj.close()
    (tmp_path / "backup" / wc.relative_path).write_text("篡改")
    with pytest.raises(WorkbenchError) as exc:
        restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert exc.value.code is ErrorCode.STORAGE_FAILED
    assert not (tmp_path / "restored").exists()


# ------------------------------------------------ G05 供应商客户端（假 SDK 验证协议归一化）


class _FakeStream:
    def __init__(self, chunks, stop_reason):
        self.text_stream = iter(chunks)
        self._stop = stop_reason
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = True

    def get_final_message(self):
        class U:
            input_tokens, output_tokens = 12, 34

        class M:
            usage = U()
            stop_reason = self._stop

        return M()


def _fake_anthropic(monkeypatch, chunks, stop_reason="end_turn", raise_error=None):
    import anthropic

    captured = {}

    class Messages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            if raise_error:
                raise raise_error
            return _FakeStream(chunks, stop_reason)

        def create(self, **kwargs):
            captured.update(kwargs)
            if raise_error:
                raise raise_error

            class Block:
                type, text = "text", "".join(chunks)

            class U:
                input_tokens, output_tokens = 5, 6

            class M:
                content = [Block()]
                usage = U()

            M.stop_reason = stop_reason
            return M()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    return captured


@pytest.mark.parametrize("base_url", [None, "https://api.deepseek.com/anthropic"])
def test_anthropic_client_normalizes_events_and_never_leaks_key(monkeypatch, base_url):
    from story_workbench.infrastructure.provider import (
        CallOptions,
        CancelSignal,
        ChatMessage,
        EventKind,
        make_anthropic_clients,
    )

    captured = _fake_anthropic(monkeypatch, ["井口", "很凉"], stop_reason="max_tokens")
    stream, complete = make_anthropic_clients(
        "sk-ant-secret", base_url=base_url, disable_thinking=base_url is not None
    )
    options = CallOptions(model="claude-sonnet-5", max_output_tokens=50, timeout_seconds=9)
    msgs = [ChatMessage("system", "系统"), ChatMessage("user", "写")]
    events = list(stream(msgs, options, CancelSignal()))
    assert [e.kind for e in events] == [
        EventKind.TEXT,
        EventKind.TEXT,
        EventKind.USAGE,
        EventKind.TRUNCATED,
    ]
    assert captured["init"]["max_retries"] == 0 and captured["init"]["timeout"] == 9
    assert captured["init"]["base_url"] == base_url
    assert captured.get("thinking") == ({"type": "disabled"} if base_url else None)
    assert captured["system"] == "系统" and captured["messages"] == [
        {"role": "user", "content": "写"}
    ]
    assert "sk-ant-secret" not in repr(events)
    result = complete(msgs, options, CancelSignal())
    assert (
        result.text == "井口很凉"
        and result.truncated
        and result.usage == {"input_tokens": 5, "output_tokens": 6}
    )
    assert captured.get("thinking") == ({"type": "disabled"} if base_url else None)


def test_anthropic_client_cancel_and_errors(monkeypatch):
    from story_workbench.infrastructure.provider import (
        CallOptions,
        CancelSignal,
        ChatMessage,
        EventKind,
        ProviderError,
        make_anthropic_clients,
    )

    _fake_anthropic(monkeypatch, ["一", "二", "三"])
    stream, complete = make_anthropic_clients("k")
    options = CallOptions(model="m", max_output_tokens=10, timeout_seconds=1)
    cancel = CancelSignal()
    kinds = []
    for e in stream([ChatMessage("user", "x")], options, cancel):
        kinds.append(e.kind)
        cancel.set()
    assert kinds == [EventKind.TEXT, EventKind.ERROR]
    _fake_anthropic(monkeypatch, [], raise_error=TimeoutError("slow"))
    stream2, complete2 = make_anthropic_clients("k")
    events = list(stream2([ChatMessage("user", "x")], options, CancelSignal()))
    assert events[-1].kind is EventKind.ERROR and events[-1].error == "TimeoutError"
    with pytest.raises(ProviderError):
        complete2([ChatMessage("user", "x")], options, CancelSignal())


def test_compatible_provider_rejects_incomplete_end_and_pre_cancelled_requests(monkeypatch):
    from story_workbench.infrastructure.provider import (
        CallOptions,
        CancelSignal,
        ChatMessage,
        EventKind,
        ProviderError,
        make_anthropic_clients,
    )

    captured = _fake_anthropic(monkeypatch, ["一"], stop_reason=None)
    stream, complete = make_anthropic_clients(
        "secret", base_url="https://api.deepseek.com/anthropic", disable_thinking=True
    )
    options = CallOptions(model="deepseek-flash", max_output_tokens=10, timeout_seconds=1)
    messages = [ChatMessage("user", "写")]
    cancel = CancelSignal()
    cancel.set()
    assert list(stream(messages, options, cancel))[-1].error == "cancelled"
    with pytest.raises(ProviderError, match="cancelled"):
        complete(messages, options, cancel)
    assert captured == {}
    events = list(stream(messages, options, CancelSignal()))
    assert events[-1].kind is EventKind.ERROR
    with pytest.raises(ProviderError, match="unexpected_stop_reason"):
        complete(messages, options, CancelSignal())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deepseek_uses_real_sdk_for_stream_and_complete_without_network(monkeypatch, asynchronous):
    import json

    import anthropic
    import httpx2 as httpx

    from story_workbench import bootstrap
    from story_workbench.infrastructure.provider import CancelSignal, ChatMessage, EventKind

    requests = []
    final = {
        "id": "test-message",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-flash",
        "content": [{"type": "text", "text": "井底。"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "disabled"}
        if not payload.get("stream"):
            return httpx.Response(200, json=final)
        events = [
            {"type": "message_start", "message": {**final, "content": [], "stop_reason": None}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "井底。"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 3},
            },
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    settings = bootstrap.Settings(provider="deepseek")
    options = bootstrap.build_generation_config(settings).options
    messages = [ChatMessage("user", "写一句")]
    if asynchronous:
        import asyncio

        original = anthropic.AsyncAnthropic
        connections = []

        def make_client(**kwargs):
            http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            connections.append(http)
            return original(http_client=http, **kwargs)

        monkeypatch.setattr(anthropic, "AsyncAnthropic", make_client)
        clients = bootstrap.build_clients(settings)

        async def call():
            events = [
                event async for event in clients.async_stream(messages, options, CancelSignal())
            ]
            result = await clients.async_complete(messages, options, CancelSignal())
            return events, result

        events, result = asyncio.run(call())
        assert all(http.is_closed for http in connections)
    else:
        original = anthropic.Anthropic
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            monkeypatch.setattr(
                anthropic, "Anthropic", lambda **kw: original(http_client=http, **kw)
            )
            clients = bootstrap.build_clients(settings)
            events = list(clients.stream(messages, options, CancelSignal()))
            result = clients.complete(messages, options, CancelSignal())
    assert events[-1].kind is EventKind.COMPLETED
    assert "".join(e.text for e in events) == "井底。"
    assert next(e.usage for e in events if e.kind is EventKind.USAGE) == final["usage"]
    assert result.text == "井底。" and result.usage == final["usage"] and not result.truncated
    assert len(requests) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deepseek_tool_use_round_trip_without_network(monkeypatch, asynchronous):
    """M5-01：tools、tool_use 与 tool_result 经真实 SDK 序列化；不传 tools 时请求不变。"""
    import json

    import anthropic
    import httpx2 as httpx

    from story_workbench import bootstrap
    from story_workbench.infrastructure.provider import (
        CancelSignal,
        ChatMessage,
        EventKind,
        TextBlock,
        ToolResultBlock,
        ToolSpec,
        ToolUseBlock,
    )

    requests = []
    tool_input = {"chapter": 2, "mode": "formal"}
    final = {
        "id": "test-message",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-flash",
        "content": [
            {"type": "text", "text": "先看计划。"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_chapter", "input": tool_input},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 9},
    }

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if not payload.get("stream"):
            return httpx.Response(200, json=final)
        partial = json.dumps(tool_input)

        def delta(index, **body):
            return {"type": "content_block_delta", "index": index, "delta": body}

        tool_block = {"type": "tool_use", "id": "toolu_1", "name": "get_chapter", "input": {}}
        text_block = {"type": "text", "text": ""}
        events = [
            {"type": "message_start", "message": {**final, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": text_block},
            delta(0, type="text_delta", text="先看计划。"),
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": tool_block},
            delta(1, type="input_json_delta", partial_json=partial[:7]),
            delta(1, type="input_json_delta", partial_json=partial[7:]),
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    settings = bootstrap.Settings(provider="deepseek")
    base_options = bootstrap.build_generation_config(settings).options
    spec = ToolSpec(
        name="get_chapter",
        description="读取某一章的计划与状态。",
        input_schema={"type": "object", "properties": {"chapter": {"type": "integer"}}},
    )
    options = replace(base_options, tools=(spec,))
    first = [ChatMessage("system", "系统"), ChatMessage("user", "写第二章")]
    follow_up = first + [
        ChatMessage(
            "assistant",
            (TextBlock("先看计划。"), ToolUseBlock("toolu_1", "get_chapter", tool_input)),
        ),
        ChatMessage("user", (ToolResultBlock("toolu_1", "第二章：剧情……", is_error=False),)),
    ]

    if asynchronous:
        import asyncio

        original = anthropic.AsyncAnthropic
        monkeypatch.setattr(
            anthropic,
            "AsyncAnthropic",
            lambda **kw: original(
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **kw
            ),
        )
        clients = bootstrap.build_clients(settings)

        async def call():
            events = [e async for e in clients.async_stream(first, options, CancelSignal())]
            result = await clients.async_complete(follow_up, options, CancelSignal())
            plain = await clients.async_complete(first, base_options, CancelSignal())
            return events, result, plain

        events, result, plain = asyncio.run(call())
    else:
        original = anthropic.Anthropic
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            monkeypatch.setattr(
                anthropic, "Anthropic", lambda **kw: original(http_client=http, **kw)
            )
            clients = bootstrap.build_clients(settings)
            events = list(clients.stream(first, options, CancelSignal()))
            result = clients.complete(follow_up, options, CancelSignal())
            plain = clients.complete(first, base_options, CancelSignal())

    # 流式：文本在前，tool_use 在 usage 之前，工具调用结束算 completed
    assert [e.kind for e in events] == [
        EventKind.TEXT,
        EventKind.TOOL_USE,
        EventKind.USAGE,
        EventKind.COMPLETED,
    ]
    assert events[1].tool_use == ToolUseBlock("toolu_1", "get_chapter", tool_input)
    # 请求里带工具定义
    assert requests[0]["tools"] == [
        {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
    ]
    # 非流式：tool_uses 归一化，文本保留
    assert result.tool_uses == (ToolUseBlock("toolu_1", "get_chapter", tool_input),)
    assert result.text == "先看计划。" and not result.truncated
    # 工具往返消息按 Anthropic 协议序列化
    assert requests[1]["messages"][1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "先看计划。"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_chapter", "input": tool_input},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "第二章：剧情……"}
            ],
        },
    ]
    # 不传 tools 时请求没有 tools 字段
    assert "tools" not in requests[2] and plain.text == "先看计划。"
