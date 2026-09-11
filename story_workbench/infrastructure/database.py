"""SQLite 读写、命名提交函数与 schema 迁移。唯一的 SQL 写入口。

约定：每个连接开启外键；显式 BEGIN IMMEDIATE 事务；历史表只插入；
所有表带 UNIQUE(novel_id, id)，子表用 (novel_id, x_id) 复合外键拒绝跨作品引用；
章节范围的记录再带 UNIQUE(novel_id, chapter_id, id)，指针用三元组拒绝跨章引用。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from story_workbench.domain import rules
from story_workbench.domain.records import (
    ChapterRecord,
    ErrorCode,
    NovelRecord,
    PlanRecord,
    RunStatus,
    SaveResult,
    VersionOrigin,
    VersionRecord,
    WorkbenchError,
)

SCHEMA_VERSION = 1

# 共有字段说明：id TEXT 主键；created_at UTC ISO 文本；除 novel 外均有 novel_id。
_SCHEMA_V1 = """
CREATE TABLE novel (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    design_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE chapter (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number > 0),
    current_plan_id TEXT,
    current_draft_id TEXT,
    adopted_id TEXT,
    current_snapshot_id TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, number),
    FOREIGN KEY (novel_id, id, current_plan_id) REFERENCES plan(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, id, current_draft_id) REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, id, adopted_id) REFERENCES adoption(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, id, current_snapshot_id) REFERENCES snapshot(novel_id, chapter_id, id)
);

CREATE TABLE message (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('author', 'model', 'system')),
    body TEXT NOT NULL,
    chapter_id TEXT,
    UNIQUE (novel_id, id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id)
);

CREATE TABLE story_item (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    authority TEXT NOT NULL
        CHECK (authority IN ('ai_suggestion', 'author_idea', 'author_decision')),
    lifecycle TEXT NOT NULL
        CHECK (lifecycle IN ('draft', 'candidate', 'decided', 'rejected', 'archived')),
    source_message_id TEXT NOT NULL,
    supersedes_id TEXT,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, logical_id, revision),
    FOREIGN KEY (novel_id, source_message_id) REFERENCES message(novel_id, id),
    FOREIGN KEY (novel_id, supersedes_id) REFERENCES story_item(novel_id, id)
);

CREATE TABLE plan (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    plot TEXT,
    ending TEXT,
    source_message_id TEXT NOT NULL,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id, id),
    UNIQUE (novel_id, chapter_id, revision),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, source_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE version (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    parent_id TEXT,
    restored_from_id TEXT,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('author', 'generated', 'external', 'restore')),
    run_id TEXT,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, chapter_id, parent_id) REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, restored_from_id)
        REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, run_id) REFERENCES run(novel_id, id)
);

CREATE TABLE workcopy (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    base_version_id TEXT,
    base_hash TEXT,
    observed_version_id TEXT,
    sync_status TEXT NOT NULL CHECK (sync_status IN ('in_sync', 'imported', 'conflict', 'pending')),
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, chapter_id, base_version_id)
        REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, observed_version_id)
        REFERENCES version(novel_id, chapter_id, id)
);

CREATE TABLE basis (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    plan_id TEXT,
    previous_snapshot_id TEXT,
    design_revision INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    omitted_sources_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (novel_id, id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, chapter_id, plan_id) REFERENCES plan(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, previous_snapshot_id) REFERENCES snapshot(novel_id, id)
);

CREATE TABLE basis_source (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    basis_id TEXT NOT NULL,
    version_id TEXT,
    story_item_id TEXT,
    snapshot_id TEXT,
    fact_revision_id TEXT,
    message_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('rule', 'fact', 'intent', 'discussion', 'text')),
    excerpt_start INTEGER,
    excerpt_end INTEGER,
    quote TEXT NOT NULL,
    UNIQUE (novel_id, id),
    CHECK (
        (version_id IS NOT NULL) + (story_item_id IS NOT NULL) + (snapshot_id IS NOT NULL)
        + (fact_revision_id IS NOT NULL) + (message_id IS NOT NULL) = 1
    ),
    CHECK ((excerpt_start IS NULL) = (excerpt_end IS NULL)),
    FOREIGN KEY (novel_id, basis_id) REFERENCES basis(novel_id, id),
    FOREIGN KEY (novel_id, version_id) REFERENCES version(novel_id, id),
    FOREIGN KEY (novel_id, story_item_id) REFERENCES story_item(novel_id, id),
    FOREIGN KEY (novel_id, snapshot_id) REFERENCES snapshot(novel_id, id),
    FOREIGN KEY (novel_id, fact_revision_id) REFERENCES fact_revision(novel_id, id),
    FOREIGN KEY (novel_id, message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE run (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT,
    task TEXT NOT NULL CHECK (task IN ('discuss', 'generate', 'inspect', 'diagnose', 'recompute')),
    basis_id TEXT,
    input_version_id TEXT,
    source_message_id TEXT NOT NULL,
    retry_of_id TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    partial_text TEXT NOT NULL DEFAULT '',
    result_version_id TEXT,
    provider TEXT,
    model TEXT,
    template_version TEXT NOT NULL,
    request_json TEXT NOT NULL DEFAULT '{}',
    usage_json TEXT,
    error_code TEXT,
    UNIQUE (novel_id, id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, basis_id) REFERENCES basis(novel_id, id),
    FOREIGN KEY (novel_id, input_version_id) REFERENCES version(novel_id, id),
    FOREIGN KEY (novel_id, source_message_id) REFERENCES message(novel_id, id),
    FOREIGN KEY (novel_id, retry_of_id) REFERENCES run(novel_id, id),
    FOREIGN KEY (novel_id, result_version_id) REFERENCES version(novel_id, id)
);

CREATE TABLE check_result (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    basis_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    actual_plot TEXT NOT NULL,
    actual_ending TEXT NOT NULL,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id, id),
    UNIQUE (novel_id, chapter_id, version_id, basis_id, id),
    UNIQUE (novel_id, run_id),
    FOREIGN KEY (novel_id, chapter_id, version_id) REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, basis_id) REFERENCES basis(novel_id, id),
    FOREIGN KEY (novel_id, run_id) REFERENCES run(novel_id, id)
);

CREATE TABLE issue (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    check_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'new_persistent_setting', 'decision_conflict', 'canon_conflict',
        'plan_deviation', 'rumor_or_belief', 'suspected_error')),
    explanation TEXT NOT NULL,
    proposed_change TEXT,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, check_id, id),
    FOREIGN KEY (novel_id, check_id) REFERENCES check_result(novel_id, id)
);

CREATE TABLE issue_evidence (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    version_id TEXT,
    basis_source_id TEXT,
    start INTEGER,
    end INTEGER,
    quote TEXT NOT NULL,
    UNIQUE (novel_id, id),
    CHECK ((version_id IS NOT NULL) + (basis_source_id IS NOT NULL) = 1),
    CHECK ((start IS NULL) = (end IS NULL)),
    FOREIGN KEY (novel_id, issue_id) REFERENCES issue(novel_id, id),
    FOREIGN KEY (novel_id, version_id) REFERENCES version(novel_id, id),
    FOREIGN KEY (novel_id, basis_source_id) REFERENCES basis_source(novel_id, id)
);

CREATE TABLE candidate_fact (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    check_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'world', 'character_belief', 'reader_information', 'narration', 'uncertain')),
    subject TEXT,
    proposition TEXT NOT NULL,
    story_time TEXT,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    quote TEXT NOT NULL,
    UNIQUE (novel_id, id),
    FOREIGN KEY (novel_id, check_id) REFERENCES check_result(novel_id, id)
);

CREATE TABLE review (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    basis_id TEXT NOT NULL,
    author_message_id TEXT NOT NULL,
    read_declared INTEGER NOT NULL CHECK (read_declared IN (0, 1)),
    accepted_plot TEXT,
    accepted_ending TEXT,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id, id),
    UNIQUE (novel_id, chapter_id, version_id, id),
    FOREIGN KEY (novel_id, chapter_id, version_id, basis_id, check_id)
        REFERENCES check_result(novel_id, chapter_id, version_id, basis_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE resolution (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    review_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'authorize_setting', 'authorize_decision_change', 'keep_as_narrative',
        'correct_extraction', 'accept_deviation', 'keep_open')),
    author_message_id TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    decision_revision_id TEXT,
    fact_revision_id TEXT,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, review_id, issue_id),
    FOREIGN KEY (novel_id, review_id) REFERENCES review(novel_id, id),
    FOREIGN KEY (novel_id, issue_id) REFERENCES issue(novel_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id),
    FOREIGN KEY (novel_id, decision_revision_id) REFERENCES story_item(novel_id, id),
    FOREIGN KEY (novel_id, fact_revision_id) REFERENCES fact_revision(novel_id, id)
);

CREATE TABLE adoption (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    previous_adoption_id TEXT,
    request_id TEXT NOT NULL,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id, id),
    UNIQUE (novel_id, request_id),
    FOREIGN KEY (novel_id, chapter_id, version_id, review_id)
        REFERENCES review(novel_id, chapter_id, version_id, id),
    FOREIGN KEY (novel_id, chapter_id, previous_adoption_id)
        REFERENCES adoption(novel_id, chapter_id, id)
);

CREATE TABLE fact_revision (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    previous_revision_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'world', 'character_belief', 'reader_information', 'narration', 'uncertain')),
    subject TEXT,
    proposition TEXT NOT NULL,
    story_time TEXT,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    quote TEXT NOT NULL,
    author_message_id TEXT NOT NULL,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, candidate_id, id),
    FOREIGN KEY (novel_id, candidate_id) REFERENCES candidate_fact(novel_id, id),
    FOREIGN KEY (novel_id, candidate_id, previous_revision_id)
        REFERENCES fact_revision(novel_id, candidate_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE fact_head (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    current_revision_id TEXT NOT NULL,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, candidate_id),
    FOREIGN KEY (novel_id, candidate_id, current_revision_id)
        REFERENCES fact_revision(novel_id, candidate_id, id)
);

CREATE TABLE snapshot (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    adoption_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    basis_id TEXT NOT NULL,
    previous_snapshot_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'ready', 'failed', 'stale')),
    error_code TEXT,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, adoption_id) REFERENCES adoption(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, check_id) REFERENCES check_result(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, review_id) REFERENCES review(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, basis_id) REFERENCES basis(novel_id, id),
    FOREIGN KEY (novel_id, previous_snapshot_id) REFERENCES snapshot(novel_id, id)
);

CREATE TABLE snapshot_fact (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    candidate_id TEXT,
    fact_revision_id TEXT,
    UNIQUE (novel_id, id),
    CHECK ((candidate_id IS NOT NULL) + (fact_revision_id IS NOT NULL) = 1),
    FOREIGN KEY (novel_id, snapshot_id) REFERENCES snapshot(novel_id, id),
    FOREIGN KEY (novel_id, candidate_id) REFERENCES candidate_fact(novel_id, id),
    FOREIGN KEY (novel_id, fact_revision_id) REFERENCES fact_revision(novel_id, id)
);

CREATE TABLE impact (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    old_snapshot_id TEXT,
    source_adoption_id TEXT,
    source_story_item_id TEXT,
    reason TEXT NOT NULL,
    evidence_issue_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    resolved_review_id TEXT,
    UNIQUE (novel_id, id),
    CHECK ((source_adoption_id IS NOT NULL) + (source_story_item_id IS NOT NULL) = 1),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, old_snapshot_id) REFERENCES snapshot(novel_id, id),
    FOREIGN KEY (novel_id, source_adoption_id) REFERENCES adoption(novel_id, id),
    FOREIGN KEY (novel_id, source_story_item_id) REFERENCES story_item(novel_id, id),
    FOREIGN KEY (novel_id, evidence_issue_id) REFERENCES issue(novel_id, id),
    FOREIGN KEY (novel_id, chapter_id, resolved_review_id)
        REFERENCES review(novel_id, chapter_id, id)
);

CREATE INDEX idx_version_chapter ON version(novel_id, chapter_id, created_at);
CREATE INDEX idx_plan_chapter ON plan(novel_id, chapter_id, revision);
CREATE INDEX idx_run_status ON run(novel_id, status);
CREATE INDEX idx_message_chapter ON message(novel_id, chapter_id, created_at);
CREATE INDEX idx_story_item_logical ON story_item(novel_id, logical_id, revision);
CREATE INDEX idx_snapshot_chapter ON snapshot(novel_id, chapter_id, status);
CREATE INDEX idx_impact_chapter ON impact(novel_id, chapter_id, status);
"""


# ---------------------------------------------------------------- 基础工具


def new_id() -> str:
    return uuid.uuid4().hex


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    """打开数据库；自动提交模式，事务由 transaction() 显式控制。"""
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """短写事务。任何异常都回滚，并转成 STORAGE_FAILED 或原领域错误。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except WorkbenchError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise WorkbenchError(
            ErrorCode.WRONG_SCOPE if "FOREIGN KEY" in str(exc) else ErrorCode.STORAGE_FAILED,
            "数据关系校验失败，本次没有写入。",
            {"sqlite": str(exc)},
        ) from exc
    except Exception as exc:
        conn.execute("ROLLBACK")
        raise WorkbenchError(
            ErrorCode.STORAGE_FAILED, "保存失败，本次没有写入。", {"error": repr(exc)}
        ) from exc
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- 迁移


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """初版建库一个迁移；更高版本只报告不兼容，不自动降级。"""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "项目数据库由更新版本的应用创建，当前应用无法打开。",
            {"db_version": current, "app_version": SCHEMA_VERSION},
        )
    if current == SCHEMA_VERSION:
        return current
    with transaction(conn):
        if current < 1:
            # executescript 会隐式提交，因此逐条执行以留在同一事务内。
            for statement in _SCHEMA_V1.split(";"):
                if statement.strip():
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


def integrity_ok(conn: sqlite3.Connection) -> bool:
    quick = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    return quick == "ok" and not fk


# ---------------------------------------------------------------- 行转记录


def _chapter(row: sqlite3.Row) -> ChapterRecord:
    return ChapterRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        number=row["number"],
        current_plan_id=row["current_plan_id"],
        current_draft_id=row["current_draft_id"],
        adopted_id=row["adopted_id"],
        current_snapshot_id=row["current_snapshot_id"],
        needs_review=bool(row["needs_review"]),
    )


def _version(row: sqlite3.Row) -> VersionRecord:
    return VersionRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        body=row["body"],
        content_hash=row["content_hash"],
        origin=VersionOrigin(row["origin"]),
        created_at=row["created_at"],
        parent_id=row["parent_id"],
        restored_from_id=row["restored_from_id"],
        run_id=row["run_id"],
    )


def _plan(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        revision=row["revision"],
        plot=row["plot"],
        ending=row["ending"],
        source_message_id=row["source_message_id"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------- 读


def get_novel(conn: sqlite3.Connection) -> NovelRecord | None:
    row = conn.execute("SELECT id, title, design_revision FROM novel LIMIT 1").fetchone()
    if row is None:
        return None
    return NovelRecord(id=row["id"], title=row["title"], design_revision=row["design_revision"])


def get_chapter(conn: sqlite3.Connection, novel_id: str, chapter_id: str) -> ChapterRecord:
    row = conn.execute(
        "SELECT * FROM chapter WHERE novel_id = ? AND id = ?", (novel_id, chapter_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "章节不存在。", {"chapter_id": chapter_id})
    return _chapter(row)


def list_chapters(conn: sqlite3.Connection, novel_id: str) -> list[ChapterRecord]:
    rows = conn.execute(
        "SELECT * FROM chapter WHERE novel_id = ? ORDER BY number", (novel_id,)
    ).fetchall()
    return [_chapter(r) for r in rows]


def get_version(conn: sqlite3.Connection, novel_id: str, version_id: str) -> VersionRecord:
    row = conn.execute(
        "SELECT * FROM version WHERE novel_id = ? AND id = ?", (novel_id, version_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "正文版本不存在。", {"version_id": version_id})
    return _version(row)


def list_versions(conn: sqlite3.Connection, novel_id: str, chapter_id: str) -> list[VersionRecord]:
    rows = conn.execute(
        "SELECT * FROM version WHERE novel_id = ? AND chapter_id = ? ORDER BY created_at, rowid",
        (novel_id, chapter_id),
    ).fetchall()
    return [_version(r) for r in rows]


def get_plan(conn: sqlite3.Connection, novel_id: str, plan_id: str) -> PlanRecord:
    row = conn.execute(
        "SELECT * FROM plan WHERE novel_id = ? AND id = ?", (novel_id, plan_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "章节计划不存在。", {"plan_id": plan_id})
    return _plan(row)


def list_plans(conn: sqlite3.Connection, novel_id: str, chapter_id: str) -> list[PlanRecord]:
    rows = conn.execute(
        "SELECT * FROM plan WHERE novel_id = ? AND chapter_id = ? ORDER BY revision",
        (novel_id, chapter_id),
    ).fetchall()
    return [_plan(r) for r in rows]


def list_runs_with_status(conn: sqlite3.Connection, novel_id: str, status: RunStatus) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM run WHERE novel_id = ? AND status = ? ORDER BY created_at",
        (novel_id, status.value),
    ).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- 命名提交函数


def commit_new_novel(conn: sqlite3.Connection, title: str) -> NovelRecord:
    novel_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO novel (id, created_at, title, design_revision) VALUES (?, ?, ?, 0)",
            (novel_id, now_utc(), title),
        )
    return NovelRecord(id=novel_id, title=title, design_revision=0)


def commit_new_chapter(conn: sqlite3.Connection, novel_id: str, number: int) -> ChapterRecord:
    if number <= 0:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "章号必须大于零。", {"number": number})
    chapter_id = new_id()
    with transaction(conn):
        exists = conn.execute(
            "SELECT id FROM chapter WHERE novel_id = ? AND number = ?", (novel_id, number)
        ).fetchone()
        if exists:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "该章号已存在。",
                {"number": number, "chapter_id": exists["id"]},
            )
        conn.execute(
            "INSERT INTO chapter (id, novel_id, created_at, number) VALUES (?, ?, ?, ?)",
            (chapter_id, novel_id, now_utc(), number),
        )
    return ChapterRecord(id=chapter_id, novel_id=novel_id, number=number)


def commit_message(
    conn: sqlite3.Connection, novel_id: str, role: str, body: str, chapter_id: str | None
) -> str:
    message_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO message (id, novel_id, created_at, role, body, chapter_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, novel_id, now_utc(), role, body, chapter_id),
        )
    return message_id


def commit_plan(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    plot: str | None,
    ending: str | None,
    source_message_id: str,
    expected_current_plan_id: str | None,
) -> PlanRecord:
    """追加新计划修订并移动 current_plan 指针；基准不符时拒绝，不写入。"""
    plan_id = new_id()
    with transaction(conn):
        chapter = get_chapter(conn, novel_id, chapter_id)
        if chapter.current_plan_id != expected_current_plan_id:
            raise WorkbenchError(
                ErrorCode.STALE_BASIS,
                "章节计划已被更改，请查看最新计划后再保存。",
                {"current_plan_id": chapter.current_plan_id},
            )
        last = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM plan WHERE novel_id = ? AND chapter_id = ?",
            (novel_id, chapter_id),
        ).fetchone()[0]
        created = now_utc()
        conn.execute(
            "INSERT INTO plan (id, novel_id, created_at, chapter_id, revision, plot, ending,"
            " source_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, novel_id, created, chapter_id, last + 1, plot, ending, source_message_id),
        )
        conn.execute(
            "UPDATE chapter SET current_plan_id = ? WHERE novel_id = ? AND id = ?",
            (plan_id, novel_id, chapter_id),
        )
    return PlanRecord(
        id=plan_id,
        novel_id=novel_id,
        chapter_id=chapter_id,
        revision=last + 1,
        plot=plot,
        ending=ending,
        source_message_id=source_message_id,
        created_at=created,
    )


def commit_version(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    body: str,
    origin: VersionOrigin,
    parent_id: str | None,
    expected_current_draft_id: str | None,
    *,
    restored_from_id: str | None = None,
    run_id: str | None = None,
    move_pointer: bool = True,
) -> SaveResult:
    """追加不可变正文版本。

    版本一定保存；只有当前编辑稿指针仍等于 expected_current_draft_id 且 move_pointer
    为真时才移动指针。基准不符时保留两稿并返回冲突，不丢作者输入、不自动合并。
    """
    version_id = new_id()
    with transaction(conn):
        chapter = get_chapter(conn, novel_id, chapter_id)
        for ref, what in ((parent_id, "父版本"), (restored_from_id, "恢复来源")):
            if ref is not None:
                ref_row = conn.execute(
                    "SELECT chapter_id FROM version WHERE novel_id = ? AND id = ?", (novel_id, ref)
                ).fetchone()
                if ref_row is None or ref_row["chapter_id"] != chapter_id:
                    raise WorkbenchError(
                        ErrorCode.WRONG_SCOPE, f"{what}不属于本章。", {"version_id": ref}
                    )
        conn.execute(
            "INSERT INTO version (id, novel_id, created_at, chapter_id, parent_id,"
            " restored_from_id, body, content_hash, origin, run_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                novel_id,
                now_utc(),
                chapter_id,
                parent_id,
                restored_from_id,
                body,
                rules.content_hash(body),
                origin.value,
                run_id,
            ),
        )
        became_current = move_pointer and chapter.current_draft_id == expected_current_draft_id
        if became_current:
            conn.execute(
                "UPDATE chapter SET current_draft_id = ? WHERE novel_id = ? AND id = ?",
                (version_id, novel_id, chapter_id),
            )
    return SaveResult(
        version_id=version_id,
        became_current=became_current,
        conflicting_current_id=None if became_current else chapter.current_draft_id,
    )


def commit_mark_runs_interrupted(conn: sqlite3.Connection, novel_id: str) -> tuple[str, ...]:
    """打开项目时把上次未结束的 run 标为中断；不自动重新调用模型。"""
    with transaction(conn):
        rows = conn.execute(
            "SELECT id FROM run WHERE novel_id = ? AND status IN ('pending', 'running')",
            (novel_id,),
        ).fetchall()
        ids = tuple(r["id"] for r in rows)
        if ids:
            conn.executemany(
                "UPDATE run SET status = 'interrupted' WHERE novel_id = ? AND id = ?",
                [(novel_id, i) for i in ids],
            )
    return ids
