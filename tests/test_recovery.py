"""文件、重启、备份与恢复流程。当前覆盖 S01 的锁、布局与中断标记。"""

from __future__ import annotations

from story_workbench.application.projects import open_project
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
