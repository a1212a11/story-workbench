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
    AdoptionContext,
    AdoptionRecord,
    AdoptionResult,
    AuthorAction,
    Authority,
    BasisRecord,
    CandidateFactRecord,
    ChapterRecord,
    CheckInput,
    CheckRecord,
    ErrorCode,
    FactCorrection,
    FactKind,
    IssueKind,
    IssueRecord,
    Lifecycle,
    MessageRecord,
    MessageRole,
    NovelRecord,
    PlanRecord,
    ResolutionAction,
    RunRecord,
    RunStatus,
    SaveResult,
    SnapshotChain,
    SnapshotDraft,
    SnapshotFactDetail,
    SnapshotFactRef,
    SnapshotRecord,
    SnapshotStatus,
    StoryItemRecord,
    SyncStatus,
    TaskKind,
    VersionOrigin,
    VersionRecord,
    WorkbenchError,
    WorkcopyRecord,
)

SCHEMA_VERSION = 2

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

# v2：拒绝稿只追加记录；story_item 可限定适用章节（NULL 表示全书规则）。
_SCHEMA_V2 = """
ALTER TABLE story_item ADD COLUMN chapter_id TEXT REFERENCES chapter(id);

CREATE TABLE draft_rejection (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    version_id TEXT NOT NULL,
    author_message_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, version_id),
    FOREIGN KEY (novel_id, version_id) REFERENCES version(novel_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id)
);
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2}


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
        for target in range(current + 1, SCHEMA_VERSION + 1):
            # executescript 会隐式提交，因此逐条执行以留在同一事务内。
            for statement in _MIGRATIONS[target].split(";"):
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


def _workcopy(row: sqlite3.Row) -> WorkcopyRecord:
    return WorkcopyRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        relative_path=row["relative_path"],
        base_version_id=row["base_version_id"],
        base_hash=row["base_hash"],
        observed_version_id=row["observed_version_id"],
        sync_status=SyncStatus(row["sync_status"]),
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


def get_workcopy(conn: sqlite3.Connection, novel_id: str, chapter_id: str) -> WorkcopyRecord | None:
    row = conn.execute(
        "SELECT * FROM workcopy WHERE novel_id = ? AND chapter_id = ?", (novel_id, chapter_id)
    ).fetchone()
    return None if row is None else _workcopy(row)


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


def _insert_version(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    body: str,
    origin: VersionOrigin,
    parent_id: str | None,
    expected_current_draft_id: str | None,
    restored_from_id: str | None,
    run_id: str | None,
    move_pointer: bool,
) -> SaveResult:
    """事务内的版本追加；调用方负责 transaction()。"""
    version_id = new_id()
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
    with transaction(conn):
        return _insert_version(
            conn,
            novel_id,
            chapter_id,
            body,
            origin,
            parent_id,
            expected_current_draft_id,
            restored_from_id,
            run_id,
            move_pointer,
        )


def commit_workcopy_binding(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    relative_path: str,
    base_version_id: str | None,
    base_hash: str | None,
    sync_status: SyncStatus,
) -> WorkcopyRecord:
    """建立或更新章节与工作文件的绑定；每章一行。"""
    with transaction(conn):
        existing = get_workcopy(conn, novel_id, chapter_id)
        if existing is None:
            row_id = new_id()
            conn.execute(
                "INSERT INTO workcopy (id, novel_id, created_at, chapter_id, relative_path,"
                " base_version_id, base_hash, observed_version_id, sync_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    novel_id,
                    now_utc(),
                    chapter_id,
                    relative_path,
                    base_version_id,
                    base_hash,
                    base_version_id,
                    sync_status.value,
                ),
            )
        else:
            conn.execute(
                "UPDATE workcopy SET relative_path = ?, base_version_id = ?, base_hash = ?,"
                " observed_version_id = ?, sync_status = ? WHERE novel_id = ? AND chapter_id = ?",
                (
                    relative_path,
                    base_version_id,
                    base_hash,
                    base_version_id,
                    sync_status.value,
                    novel_id,
                    chapter_id,
                ),
            )
        result = get_workcopy(conn, novel_id, chapter_id)
    assert result is not None
    return result


def commit_workcopy_status(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str, sync_status: SyncStatus
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE workcopy SET sync_status = ? WHERE novel_id = ? AND chapter_id = ?",
            (sync_status.value, novel_id, chapter_id),
        )


def commit_external_import(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    body: str,
    base_version_id: str | None,
) -> SaveResult:
    """把外部编辑的文件内容导入为 external 版本，并在同一事务更新工作副本基准。

    观察稿仍等于基准时新稿成为当前稿；数据库已有新稿时保留两稿并返回冲突。
    """
    with transaction(conn):
        result = _insert_version(
            conn,
            novel_id,
            chapter_id,
            body,
            VersionOrigin.EXTERNAL,
            base_version_id,
            base_version_id,
            None,
            None,
            True,
        )
        status = SyncStatus.IMPORTED if result.became_current else SyncStatus.CONFLICT
        conn.execute(
            "UPDATE workcopy SET base_version_id = ?, base_hash = ?, observed_version_id = ?,"
            " sync_status = ? WHERE novel_id = ? AND chapter_id = ?",
            (
                result.version_id,
                rules.content_hash(body),
                result.version_id,
                status.value,
                novel_id,
                chapter_id,
            ),
        )
    return result


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


# ---------------------------------------------------------------- 依据、任务与检查（S04）


def commit_basis(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    plan_id: str | None,
    previous_snapshot_id: str | None,
    design_revision: int,
    purpose: str,
    sources: list[tuple[str, dict[str, object]]],
    omitted_json: str = "[]",
) -> str:
    """保存一次任务的不可变依据及其来源清单。

    sources 每项为 (role, {version_id|story_item_id|snapshot_id|fact_revision_id|message_id,
    excerpt_start?, excerpt_end?, quote})；每行恰有一个来源引用，由表约束保证。
    """
    basis_id = new_id()
    with transaction(conn):
        novel = get_novel(conn)
        if novel is None or novel.id != novel_id:
            raise WorkbenchError(ErrorCode.WRONG_SCOPE, "作品不匹配。")
        if novel.design_revision != design_revision:
            raise WorkbenchError(
                ErrorCode.STALE_BASIS,
                "作者规则或决定在构建上下文后发生了变化，请重新开始本次任务。",
                {"design_revision": novel.design_revision},
            )
        chapter = get_chapter(conn, novel_id, chapter_id)
        if plan_id is not None:
            plan = get_plan(conn, novel_id, plan_id)
            if plan.chapter_id != chapter_id:
                raise WorkbenchError(
                    ErrorCode.WRONG_SCOPE, "计划不属于本章。", {"plan_id": plan_id}
                )
        for _role, ref in sources:
            version_id = ref.get("version_id")
            if version_id is not None:
                src = get_version(conn, novel_id, str(version_id))
                src_chapter = get_chapter(conn, novel_id, src.chapter_id)
                if src_chapter.number > chapter.number:
                    raise WorkbenchError(
                        ErrorCode.WRONG_SCOPE,
                        "依据不能引用目标章之后的正文。",
                        {"version_id": version_id, "chapter_number": src_chapter.number},
                    )
        conn.execute(
            "INSERT INTO basis (id, novel_id, created_at, chapter_id, plan_id,"
            " previous_snapshot_id, design_revision, purpose, omitted_sources_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                basis_id,
                novel_id,
                now_utc(),
                chapter_id,
                plan_id,
                previous_snapshot_id,
                design_revision,
                purpose,
                omitted_json,
            ),
        )
        for role, ref in sources:
            conn.execute(
                "INSERT INTO basis_source (id, novel_id, created_at, basis_id, version_id,"
                " story_item_id, snapshot_id, fact_revision_id, message_id, role,"
                " excerpt_start, excerpt_end, quote)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    novel_id,
                    now_utc(),
                    basis_id,
                    ref.get("version_id"),
                    ref.get("story_item_id"),
                    ref.get("snapshot_id"),
                    ref.get("fact_revision_id"),
                    ref.get("message_id"),
                    role,
                    ref.get("excerpt_start"),
                    ref.get("excerpt_end"),
                    ref.get("quote", ""),
                ),
            )
    return basis_id


def get_basis(conn: sqlite3.Connection, novel_id: str, basis_id: str) -> BasisRecord:
    row = conn.execute(
        "SELECT * FROM basis WHERE novel_id = ? AND id = ?", (novel_id, basis_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "任务依据不存在。", {"basis_id": basis_id})
    return BasisRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        plan_id=row["plan_id"],
        previous_snapshot_id=row["previous_snapshot_id"],
        design_revision=row["design_revision"],
        purpose=row["purpose"],
    )


def list_basis_source_ids(conn: sqlite3.Connection, novel_id: str, basis_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT id FROM basis_source WHERE novel_id = ? AND basis_id = ?", (novel_id, basis_id)
    ).fetchall()
    return {r["id"] for r in rows}


def commit_run(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    chapter_id: str | None,
    task: TaskKind,
    source_message_id: str,
    template_version: str,
    status: RunStatus,
    basis_id: str | None = None,
    input_version_id: str | None = None,
    retry_of_id: str | None = None,
    request_json: str = "{}",
) -> str:
    """建立一条任务阶段记录。流保存、取消与迟到保护在 G04 补充。"""
    run_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO run (id, novel_id, created_at, chapter_id, task, basis_id,"
            " input_version_id, source_message_id, retry_of_id, status, template_version,"
            " request_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                novel_id,
                now_utc(),
                chapter_id,
                task.value,
                basis_id,
                input_version_id,
                source_message_id,
                retry_of_id,
                status.value,
                template_version,
                request_json,
            ),
        )
    return run_id


def commit_run_status(
    conn: sqlite3.Connection,
    novel_id: str,
    run_id: str,
    status: RunStatus,
    *,
    error_code: str | None = None,
    usage_json: str | None = None,
) -> None:
    """终态不可回到 running；retry 创建新 run。"""
    with transaction(conn):
        row = conn.execute(
            "SELECT status FROM run WHERE novel_id = ? AND id = ?", (novel_id, run_id)
        ).fetchone()
        if row is None:
            raise WorkbenchError(ErrorCode.NOT_FOUND, "任务不存在。", {"run_id": run_id})
        current = RunStatus(row["status"])
        terminal = {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
        if current in terminal and status is not current:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "任务已结束，不能改变状态；重试请新建任务。",
                {"run_id": run_id, "status": current.value},
            )
        conn.execute(
            "UPDATE run SET status = ?, error_code = COALESCE(?, error_code),"
            " usage_json = COALESCE(?, usage_json) WHERE novel_id = ? AND id = ?",
            (status.value, error_code, usage_json, novel_id, run_id),
        )


def commit_check_result(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    version_id: str,
    basis_id: str,
    run_id: str,
    payload: CheckInput,
) -> str:
    """只保存校验有效的完整结果：每条证据必须逐字匹配本次正文或属于同一依据。

    校验失败抛 MODEL_FAILED 且不写入；run 状态由调用方记录。
    """
    check_id = new_id()
    with transaction(conn):
        version = get_version(conn, novel_id, version_id)
        if version.chapter_id != chapter_id:
            raise WorkbenchError(
                ErrorCode.WRONG_SCOPE, "正文不属于本章。", {"version_id": version_id}
            )
        basis = get_basis(conn, novel_id, basis_id)
        if basis.chapter_id != chapter_id:
            raise WorkbenchError(ErrorCode.WRONG_SCOPE, "依据不属于本章。", {"basis_id": basis_id})
        run_row = conn.execute(
            "SELECT status FROM run WHERE novel_id = ? AND id = ?", (novel_id, run_id)
        ).fetchone()
        if run_row is None:
            raise WorkbenchError(ErrorCode.NOT_FOUND, "检查任务不存在。", {"run_id": run_id})
        allowed_sources = list_basis_source_ids(conn, novel_id, basis_id)
        body = version.body
        for index, fact in enumerate(payload.candidate_facts):
            if not rules.validate_span(body, fact.evidence_start, fact.evidence_end, fact.quote):
                raise WorkbenchError(
                    ErrorCode.MODEL_FAILED,
                    "候选事实的原文引用与正文不符，检查结果无效。",
                    {"candidate_index": index},
                )
        for i_index, issue in enumerate(payload.issues):
            if not issue.evidence:
                raise WorkbenchError(
                    ErrorCode.MODEL_FAILED, "问题缺少证据，检查结果无效。", {"issue_index": i_index}
                )
            for e_index, ev in enumerate(issue.evidence):
                if ev.version_id is not None:
                    if ev.version_id != version_id or ev.start is None or ev.end is None:
                        raise WorkbenchError(
                            ErrorCode.MODEL_FAILED,
                            "问题证据引用了本次检查之外的正文。",
                            {"issue_index": i_index, "evidence_index": e_index},
                        )
                    if not rules.validate_span(body, ev.start, ev.end, ev.quote):
                        raise WorkbenchError(
                            ErrorCode.MODEL_FAILED,
                            "问题证据与正文不符，检查结果无效。",
                            {"issue_index": i_index, "evidence_index": e_index},
                        )
                elif ev.basis_source_id is None or ev.basis_source_id not in allowed_sources:
                    raise WorkbenchError(
                        ErrorCode.MODEL_FAILED,
                        "问题证据引用了不属于本次依据的来源。",
                        {"issue_index": i_index, "evidence_index": e_index},
                    )
        created = now_utc()
        conn.execute(
            "INSERT INTO check_result (id, novel_id, created_at, chapter_id, version_id,"
            " basis_id, run_id, coverage_json, actual_plot, actual_ending)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                check_id,
                novel_id,
                created,
                chapter_id,
                version_id,
                basis_id,
                run_id,
                payload.coverage_json,
                payload.actual_plot,
                payload.actual_ending,
            ),
        )
        for issue in payload.issues:
            issue_id = new_id()
            conn.execute(
                "INSERT INTO issue (id, novel_id, created_at, check_id, kind, explanation,"
                " proposed_change) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    novel_id,
                    created,
                    check_id,
                    issue.kind.value,
                    issue.explanation,
                    issue.proposed_change,
                ),
            )
            for ev in issue.evidence:
                conn.execute(
                    "INSERT INTO issue_evidence (id, novel_id, created_at, issue_id, version_id,"
                    " basis_source_id, start, end, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        novel_id,
                        created,
                        issue_id,
                        ev.version_id,
                        ev.basis_source_id,
                        ev.start,
                        ev.end,
                        ev.quote,
                    ),
                )
        for fact in payload.candidate_facts:
            conn.execute(
                "INSERT INTO candidate_fact (id, novel_id, created_at, check_id, kind, subject,"
                " proposition, story_time, evidence_start, evidence_end, quote)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    novel_id,
                    created,
                    check_id,
                    fact.kind.value,
                    fact.subject,
                    fact.proposition,
                    fact.story_time,
                    fact.evidence_start,
                    fact.evidence_end,
                    fact.quote,
                ),
            )
    return check_id


def get_check(conn: sqlite3.Connection, novel_id: str, check_id: str) -> CheckRecord:
    row = conn.execute(
        "SELECT * FROM check_result WHERE novel_id = ? AND id = ?", (novel_id, check_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "检查结果不存在。", {"check_id": check_id})
    issue_rows = conn.execute(
        "SELECT * FROM issue WHERE novel_id = ? AND check_id = ? ORDER BY rowid",
        (novel_id, check_id),
    ).fetchall()
    return CheckRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        version_id=row["version_id"],
        basis_id=row["basis_id"],
        run_id=row["run_id"],
        actual_plot=row["actual_plot"],
        actual_ending=row["actual_ending"],
        issues=tuple(
            IssueRecord(
                id=r["id"],
                check_id=r["check_id"],
                kind=IssueKind(r["kind"]),
                explanation=r["explanation"],
                proposed_change=r["proposed_change"],
            )
            for r in issue_rows
        ),
    )


# ---------------------------------------------------------------- 采用（S05）


def _adoption(row: sqlite3.Row) -> AdoptionRecord:
    return AdoptionRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        version_id=row["version_id"],
        review_id=row["review_id"],
        request_id=row["request_id"],
        previous_adoption_id=row["previous_adoption_id"],
    )


def get_adoption(conn: sqlite3.Connection, novel_id: str, adoption_id: str) -> AdoptionRecord:
    row = conn.execute(
        "SELECT * FROM adoption WHERE novel_id = ? AND id = ?", (novel_id, adoption_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "采用记录不存在。", {"adoption_id": adoption_id})
    return _adoption(row)


def _snapshot(row: sqlite3.Row) -> SnapshotRecord:
    return SnapshotRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        adoption_id=row["adoption_id"],
        status=SnapshotStatus(row["status"]),
        previous_snapshot_id=row["previous_snapshot_id"],
    )


def get_snapshot(conn: sqlite3.Connection, novel_id: str, snapshot_id: str) -> SnapshotRecord:
    row = conn.execute(
        "SELECT * FROM snapshot WHERE novel_id = ? AND id = ?", (novel_id, snapshot_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "状态快照不存在。", {"snapshot_id": snapshot_id})
    return _snapshot(row)


def _previous_chapter(
    conn: sqlite3.Connection, novel_id: str, chapter: ChapterRecord
) -> ChapterRecord | None:
    row = conn.execute(
        "SELECT * FROM chapter WHERE novel_id = ? AND number = ?", (novel_id, chapter.number - 1)
    ).fetchone()
    return None if row is None else _chapter(row)


def load_adoption_context(conn: sqlite3.Connection, action: AuthorAction) -> AdoptionContext:
    """读取采用校验所需的当前记录；预检与事务内复核共用。"""
    novel_id = action.target.novel_id
    novel = get_novel(conn)
    if novel is None or novel.id != novel_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "采用目标不属于当前作品。")
    if action.check_id is None:
        raise WorkbenchError(ErrorCode.REVIEW_REQUIRED, "采用需要一份对应稿件的有效检查。")
    chapter = get_chapter(conn, novel_id, action.target.chapter_id)
    check = get_check(conn, novel_id, action.check_id)
    basis = get_basis(conn, novel_id, check.basis_id)
    previous = _previous_chapter(conn, novel_id, chapter)
    previous_snapshot = None
    if previous is not None and previous.current_snapshot_id is not None:
        previous_snapshot = get_snapshot(conn, novel_id, previous.current_snapshot_id)
    existing = conn.execute(
        "SELECT * FROM adoption WHERE novel_id = ? AND request_id = ?",
        (novel_id, action.request_id),
    ).fetchone()
    current_adoption = (
        get_adoption(conn, novel_id, chapter.adopted_id) if chapter.adopted_id else None
    )
    return AdoptionContext(
        novel=novel,
        chapter=chapter,
        check=check,
        basis=basis,
        previous_chapter_snapshot=previous_snapshot,
        previous_chapter_exists=chapter.number > 1,
        existing_adoption_for_request=None if existing is None else _adoption(existing),
        current_adoption=current_adoption,
    )


def commit_adoption(conn: sqlite3.Connection, action: AuthorAction) -> AdoptionResult:
    """采用事务：重读依据与权限，追加 review/resolution/adoption/candidate snapshot，
    移动采用指针，使后续章节状态过时并登记影响。模型不在事务内调用。

    重复 request_id 返回原结果；同一当前版本再次采用返回既有 adoption，不清除过时状态。
    """
    novel_id = action.target.novel_id
    with transaction(conn):
        context = load_adoption_context(conn, action)
        existing = context.existing_adoption_for_request
        if existing is not None:
            return AdoptionResult(
                adoption_id=existing.id,
                version_id=existing.version_id,
                state_status=_state_status_for(conn, novel_id, context.chapter),
                already_adopted=True,
            )
        current = context.current_adoption
        if current is not None and current.version_id == action.target.version_id:
            return AdoptionResult(
                adoption_id=current.id,
                version_id=current.version_id,
                state_status=_state_status_for(conn, novel_id, context.chapter),
                already_adopted=True,
            )
        rules.validate_adoption(action, context)

        created = now_utc()
        chapter = context.chapter
        review_id = new_id()
        conn.execute(
            "INSERT INTO review (id, novel_id, created_at, chapter_id, version_id, check_id,"
            " basis_id, author_message_id, read_declared, accepted_plot, accepted_ending)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                review_id,
                novel_id,
                created,
                chapter.id,
                action.target.version_id,
                context.check.id,
                context.basis.id,
                action.source_message_id,
                action.accepted_plot,
                action.accepted_ending,
            ),
        )
        for res in action.issue_resolutions:
            conn.execute(
                "INSERT INTO resolution (id, novel_id, created_at, review_id, issue_id, action,"
                " author_message_id, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    novel_id,
                    created,
                    review_id,
                    res.issue_id,
                    res.action.value,
                    action.source_message_id,
                    res.explanation,
                ),
            )
        adoption_id = new_id()
        conn.execute(
            "INSERT INTO adoption (id, novel_id, created_at, chapter_id, version_id, review_id,"
            " previous_adoption_id, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                adoption_id,
                novel_id,
                created,
                chapter.id,
                action.target.version_id,
                review_id,
                chapter.adopted_id,
                action.request_id,
            ),
        )
        # 本章的候选快照：状态待更新，直到 R01 重算成功才成为 current_snapshot。
        snapshot_id = new_id()
        conn.execute(
            "INSERT INTO snapshot (id, novel_id, created_at, chapter_id, adoption_id, check_id,"
            " review_id, basis_id, previous_snapshot_id, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate')",
            (
                snapshot_id,
                novel_id,
                created,
                chapter.id,
                adoption_id,
                context.check.id,
                review_id,
                context.basis.id,
                context.basis.previous_snapshot_id,
            ),
        )
        old_snapshot_id = chapter.current_snapshot_id
        conn.execute(
            "UPDATE chapter SET adopted_id = ?, current_snapshot_id = NULL"
            " WHERE novel_id = ? AND id = ?",
            (adoption_id, novel_id, chapter.id),
        )
        if old_snapshot_id is not None:
            conn.execute(
                "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND id = ?"
                " AND status = 'ready'",
                (novel_id, old_snapshot_id),
            )
        # 后续章节：累计快照依赖链保守失效，登记影响并标记待复核。
        later = conn.execute(
            "SELECT * FROM chapter WHERE novel_id = ? AND number > ? ORDER BY number",
            (novel_id, chapter.number),
        ).fetchall()
        for row in later:
            later_chapter = _chapter(row)
            if later_chapter.adopted_id is None:
                continue
            if later_chapter.current_snapshot_id is not None:
                conn.execute(
                    "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND id = ?"
                    " AND status = 'ready'",
                    (novel_id, later_chapter.current_snapshot_id),
                )
            conn.execute(
                "UPDATE chapter SET needs_review = 1 WHERE novel_id = ? AND id = ?",
                (novel_id, later_chapter.id),
            )
            conn.execute(
                "INSERT INTO impact (id, novel_id, created_at, chapter_id, old_snapshot_id,"
                " source_adoption_id, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    new_id(),
                    novel_id,
                    created,
                    later_chapter.id,
                    later_chapter.current_snapshot_id,
                    adoption_id,
                    f"第 {chapter.number} 章重新采用，本章依赖的前文状态需要复核。",
                ),
            )
    return AdoptionResult(
        adoption_id=adoption_id,
        version_id=action.target.version_id,
        state_status=SnapshotStatus.CANDIDATE,
        already_adopted=False,
    )


def _state_status_for(
    conn: sqlite3.Connection, novel_id: str, chapter: ChapterRecord
) -> SnapshotStatus:
    if chapter.current_snapshot_id is not None:
        return get_snapshot(conn, novel_id, chapter.current_snapshot_id).status
    if chapter.adopted_id is None:
        return SnapshotStatus.CANDIDATE
    row = conn.execute(
        "SELECT status FROM snapshot WHERE novel_id = ? AND adoption_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (novel_id, chapter.adopted_id),
    ).fetchone()
    return SnapshotStatus(row["status"]) if row else SnapshotStatus.CANDIDATE


# ---------------------------------------------------------------- 快照与纠正（R01）


def list_candidate_facts(
    conn: sqlite3.Connection, novel_id: str, check_id: str
) -> tuple[CandidateFactRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM candidate_fact WHERE novel_id = ? AND check_id = ? ORDER BY rowid",
        (novel_id, check_id),
    ).fetchall()
    return tuple(
        CandidateFactRecord(
            id=r["id"],
            check_id=r["check_id"],
            kind=FactKind(r["kind"]),
            proposition=r["proposition"],
            evidence_start=r["evidence_start"],
            evidence_end=r["evidence_end"],
            quote=r["quote"],
            subject=r["subject"],
            story_time=r["story_time"],
        )
        for r in rows
    )


def get_fact_heads(
    conn: sqlite3.Connection, novel_id: str, candidate_ids: tuple[str, ...]
) -> dict[str, str]:
    if not candidate_ids:
        return {}
    marks = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"SELECT candidate_id, current_revision_id FROM fact_head"
        f" WHERE novel_id = ? AND candidate_id IN ({marks})",
        (novel_id, *candidate_ids),
    ).fetchall()
    return {r["candidate_id"]: r["current_revision_id"] for r in rows}


def list_snapshot_facts(
    conn: sqlite3.Connection, novel_id: str, snapshot_id: str
) -> tuple[SnapshotFactRef, ...]:
    rows = conn.execute(
        "SELECT candidate_id, fact_revision_id FROM snapshot_fact"
        " WHERE novel_id = ? AND snapshot_id = ? ORDER BY rowid",
        (novel_id, snapshot_id),
    ).fetchall()
    return tuple(
        SnapshotFactRef(candidate_id=r["candidate_id"], fact_revision_id=r["fact_revision_id"])
        for r in rows
    )


def get_snapshot_check_id(conn: sqlite3.Connection, novel_id: str, snapshot_id: str) -> str:
    row = conn.execute(
        "SELECT check_id FROM snapshot WHERE novel_id = ? AND id = ?", (novel_id, snapshot_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "状态快照不存在。", {"snapshot_id": snapshot_id})
    return row["check_id"]


def latest_snapshot_for_adoption(
    conn: sqlite3.Connection, novel_id: str, adoption_id: str
) -> SnapshotRecord | None:
    row = conn.execute(
        "SELECT * FROM snapshot WHERE novel_id = ? AND adoption_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (novel_id, adoption_id),
    ).fetchone()
    return None if row is None else _snapshot(row)


def commit_snapshot(
    conn: sqlite3.Connection,
    novel_id: str,
    snapshot_id: str,
    draft: SnapshotDraft,
    expected_chain: SnapshotChain,
) -> SnapshotStatus:
    """比较整个来源链后提交：匹配则 ready 并成为章节当前状态；不匹配只存历史（stale）。"""
    with transaction(conn):
        snapshot = get_snapshot(conn, novel_id, snapshot_id)
        if snapshot.status is SnapshotStatus.READY:
            return SnapshotStatus.READY
        chapter = get_chapter(conn, novel_id, snapshot.chapter_id)
        previous = _previous_chapter(conn, novel_id, chapter)
        previous_current = None if previous is None else previous.current_snapshot_id
        heads = get_fact_heads(conn, novel_id, tuple(c for c, _ in expected_chain.head_revisions))
        chain_ok = (
            chapter.adopted_id == expected_chain.adoption_id == snapshot.adoption_id
            and previous_current == expected_chain.previous_snapshot_id
            and snapshot.previous_snapshot_id == expected_chain.previous_snapshot_id
            and all(heads.get(c) == r for c, r in expected_chain.head_revisions)
            and (previous is None or previous_current is not None)
        )
        if previous is not None and previous_current is not None:
            chain_ok = chain_ok and (
                get_snapshot(conn, novel_id, previous_current).status is SnapshotStatus.READY
            )
        if not chain_ok:
            conn.execute(
                "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND id = ?",
                (novel_id, snapshot_id),
            )
            return SnapshotStatus.STALE
        created = now_utc()
        for ref in draft.facts:
            conn.execute(
                "INSERT INTO snapshot_fact (id, novel_id, created_at, snapshot_id, candidate_id,"
                " fact_revision_id) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(), novel_id, created, snapshot_id, ref.candidate_id, ref.fact_revision_id),
            )
        conn.execute(
            "UPDATE snapshot SET status = 'ready', error_code = NULL WHERE novel_id = ? AND id = ?",
            (novel_id, snapshot_id),
        )
        conn.execute(
            "UPDATE chapter SET current_snapshot_id = ? WHERE novel_id = ? AND id = ?",
            (snapshot_id, novel_id, chapter.id),
        )
    return SnapshotStatus.READY


def commit_snapshot_failed(
    conn: sqlite3.Connection, novel_id: str, snapshot_id: str, error_code: str
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE snapshot SET status = 'failed', error_code = ? WHERE novel_id = ? AND id = ?"
            " AND status IN ('candidate', 'failed')",
            (error_code, novel_id, snapshot_id),
        )


def commit_snapshot_retry(conn: sqlite3.Connection, novel_id: str, failed_snapshot_id: str) -> str:
    """为失败或过时的快照建立新的候选记录（同一采用），不新增采用。"""
    new_snapshot_id = new_id()
    with transaction(conn):
        old = conn.execute(
            "SELECT * FROM snapshot WHERE novel_id = ? AND id = ?", (novel_id, failed_snapshot_id)
        ).fetchone()
        if old is None:
            raise WorkbenchError(ErrorCode.NOT_FOUND, "状态快照不存在。")
        chapter = get_chapter(conn, novel_id, old["chapter_id"])
        previous = _previous_chapter(conn, novel_id, chapter)
        conn.execute(
            "INSERT INTO snapshot (id, novel_id, created_at, chapter_id, adoption_id, check_id,"
            " review_id, basis_id, previous_snapshot_id, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate')",
            (
                new_snapshot_id,
                novel_id,
                now_utc(),
                old["chapter_id"],
                old["adoption_id"],
                old["check_id"],
                old["review_id"],
                old["basis_id"],
                None if previous is None else previous.current_snapshot_id,
            ),
        )
    return new_snapshot_id


def commit_fact_correction(
    conn: sqlite3.Connection,
    novel_id: str,
    candidate_id: str,
    correction: FactCorrection,
    author_message_id: str,
    expected_head_revision_id: str | None,
) -> str:
    """追加 fact_revision 并原子更新 fact_head；使用该候选的 ready 快照过时。"""
    revision_id = new_id()
    with transaction(conn):
        cand = conn.execute(
            "SELECT check_id FROM candidate_fact WHERE novel_id = ? AND id = ?",
            (novel_id, candidate_id),
        ).fetchone()
        if cand is None:
            raise WorkbenchError(
                ErrorCode.NOT_FOUND, "候选事实不存在。", {"candidate_id": candidate_id}
            )
        check = get_check(conn, novel_id, cand["check_id"])
        body = get_version(conn, novel_id, check.version_id).body
        if not rules.validate_span(
            body, correction.evidence_start, correction.evidence_end, correction.quote
        ):
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "纠正的原文引用与正文不符。")
        head = conn.execute(
            "SELECT current_revision_id FROM fact_head WHERE novel_id = ? AND candidate_id = ?",
            (novel_id, candidate_id),
        ).fetchone()
        current_head = None if head is None else head["current_revision_id"]
        if current_head != expected_head_revision_id:
            raise WorkbenchError(
                ErrorCode.STALE_BASIS,
                "这条事实已有更新的纠正，请查看后再操作。",
                {"current_revision_id": current_head},
            )
        created = now_utc()
        conn.execute(
            "INSERT INTO fact_revision (id, novel_id, created_at, candidate_id,"
            " previous_revision_id, kind, subject, proposition, story_time, evidence_start,"
            " evidence_end, quote, author_message_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                novel_id,
                created,
                candidate_id,
                current_head,
                correction.kind.value,
                correction.subject,
                correction.proposition,
                correction.story_time,
                correction.evidence_start,
                correction.evidence_end,
                correction.quote,
                author_message_id,
            ),
        )
        if head is None:
            conn.execute(
                "INSERT INTO fact_head (id, novel_id, created_at, candidate_id,"
                " current_revision_id) VALUES (?, ?, ?, ?, ?)",
                (new_id(), novel_id, created, candidate_id, revision_id),
            )
        else:
            conn.execute(
                "UPDATE fact_head SET current_revision_id = ?"
                " WHERE novel_id = ? AND candidate_id = ?",
                (revision_id, novel_id, candidate_id),
            )
        # 依赖该候选（原始或旧修订）的 ready 快照过时；历史快照仍引用旧修订，不漂移。
        conn.execute(
            "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND status = 'ready'"
            " AND id IN ("
            " SELECT sf.snapshot_id FROM snapshot_fact sf"
            " LEFT JOIN fact_revision fr ON fr.id = sf.fact_revision_id"
            " WHERE sf.novel_id = ? AND (sf.candidate_id = ? OR fr.candidate_id = ?))",
            (novel_id, novel_id, candidate_id, candidate_id),
        )
        conn.execute(
            "UPDATE chapter SET current_snapshot_id = NULL WHERE novel_id = ?"
            " AND current_snapshot_id IN"
            " (SELECT id FROM snapshot WHERE novel_id = ? AND status = 'stale')",
            (novel_id, novel_id),
        )
    return revision_id


# ---------------------------------------------------------------- 故事条目与拒绝稿（schema v2）


def _story_item(row: sqlite3.Row) -> StoryItemRecord:
    return StoryItemRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        logical_id=row["logical_id"],
        revision=row["revision"],
        kind=row["kind"],
        body=row["body"],
        authority=Authority(row["authority"]),
        lifecycle=Lifecycle(row["lifecycle"]),
        source_message_id=row["source_message_id"],
        chapter_id=row["chapter_id"],
        supersedes_id=row["supersedes_id"],
    )


def list_story_items_current(conn: sqlite3.Connection, novel_id: str) -> list[StoryItemRecord]:
    """每个 logical_id 的最新修订。"""
    rows = conn.execute(
        "SELECT s.* FROM story_item s JOIN ("
        "  SELECT logical_id, MAX(revision) AS revision FROM story_item"
        "  WHERE novel_id = ? GROUP BY logical_id"
        ") m ON s.logical_id = m.logical_id AND s.revision = m.revision"
        " WHERE s.novel_id = ? ORDER BY s.created_at, s.rowid",
        (novel_id, novel_id),
    ).fetchall()
    return [_story_item(r) for r in rows]


def get_story_item(conn: sqlite3.Connection, novel_id: str, item_id: str) -> StoryItemRecord:
    row = conn.execute(
        "SELECT * FROM story_item WHERE novel_id = ? AND id = ?", (novel_id, item_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "故事条目不存在。", {"story_item_id": item_id})
    return _story_item(row)


def commit_story_item(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    kind: str,
    body: str,
    authority: Authority,
    lifecycle: Lifecycle,
    source_message_id: str,
    chapter_id: str | None,
    logical_id: str | None = None,
    expected_revision_id: str | None = None,
) -> StoryItemRecord:
    """追加故事条目修订。作者决定的新增或变更会递增 design_revision，使旧依据保守过时。"""
    with transaction(conn):
        if logical_id is None:
            logical_id = new_id()
            revision = 1
            if expected_revision_id is not None:
                raise WorkbenchError(ErrorCode.INVALID_INPUT, "新条目不能带修订基准。")
            previous: StoryItemRecord | None = None
        else:
            rows = [
                r for r in list_story_items_current(conn, novel_id) if r.logical_id == logical_id
            ]
            if not rows:
                raise WorkbenchError(
                    ErrorCode.NOT_FOUND, "故事条目不存在。", {"logical_id": logical_id}
                )
            previous = rows[0]
            if previous.id != expected_revision_id:
                raise WorkbenchError(
                    ErrorCode.STALE_BASIS,
                    "这条内容已有更新的修订，请查看后再操作。",
                    {"current_revision_id": previous.id},
                )
            revision = previous.revision + 1
        if chapter_id is not None:
            get_chapter(conn, novel_id, chapter_id)
        item_id = new_id()
        conn.execute(
            "INSERT INTO story_item (id, novel_id, created_at, logical_id, revision, kind, body,"
            " authority, lifecycle, source_message_id, supersedes_id, chapter_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                novel_id,
                now_utc(),
                logical_id,
                revision,
                kind,
                body,
                authority.value,
                lifecycle.value,
                source_message_id,
                None if previous is None else previous.id,
                chapter_id,
            ),
        )
        affects_rules = authority is Authority.AUTHOR_DECISION or (
            previous is not None and previous.authority is Authority.AUTHOR_DECISION
        )
        if affects_rules:
            conn.execute(
                "UPDATE novel SET design_revision = design_revision + 1 WHERE id = ?", (novel_id,)
            )
        return get_story_item(conn, novel_id, item_id)


def commit_draft_rejection(
    conn: sqlite3.Connection, novel_id: str, version_id: str, author_message_id: str, reason: str
) -> str:
    """标记拒绝稿；只追加，不删除版本。已采用版本不能拒绝。"""
    rejection_id = new_id()
    with transaction(conn):
        version = get_version(conn, novel_id, version_id)
        chapter = get_chapter(conn, novel_id, version.chapter_id)
        if chapter.adopted_id is not None:
            adopted = get_adoption(conn, novel_id, chapter.adopted_id)
            if adopted.version_id == version_id:
                raise WorkbenchError(ErrorCode.INVALID_INPUT, "已采用的版本不能标记拒绝。")
        exists = conn.execute(
            "SELECT id FROM draft_rejection WHERE novel_id = ? AND version_id = ?",
            (novel_id, version_id),
        ).fetchone()
        if exists:
            return exists["id"]
        conn.execute(
            "INSERT INTO draft_rejection (id, novel_id, created_at, version_id, author_message_id,"
            " reason) VALUES (?, ?, ?, ?, ?, ?)",
            (rejection_id, novel_id, now_utc(), version_id, author_message_id, reason),
        )
        if chapter.current_draft_id == version_id:
            parent = version.parent_id
            conn.execute(
                "UPDATE chapter SET current_draft_id = ? WHERE novel_id = ? AND id = ?",
                (parent, novel_id, chapter.id),
            )
    return rejection_id


def list_rejected_version_ids(conn: sqlite3.Connection, novel_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT version_id FROM draft_rejection WHERE novel_id = ?", (novel_id,)
    ).fetchall()
    return {r["version_id"] for r in rows}


def get_adopted_version_id(
    conn: sqlite3.Connection, novel_id: str, chapter: ChapterRecord
) -> str | None:
    if chapter.adopted_id is None:
        return None
    return get_adoption(conn, novel_id, chapter.adopted_id).version_id


# ---------------------------------------------------------------- 上下文来源读取与当前稿选择（G04）


def list_snapshot_fact_details(
    conn: sqlite3.Connection, novel_id: str, snapshot_id: str
) -> tuple[SnapshotFactDetail, ...]:
    """快照成员展开为可读事实（有效修订优先），附来源章节。"""
    rows = conn.execute(
        "SELECT sf.candidate_id, sf.fact_revision_id,"
        " COALESCE(fr.kind, cf.kind) AS kind, COALESCE(fr.proposition, cf.proposition) AS prop,"
        " COALESCE(fr.subject, cf.subject) AS subject,"
        " COALESCE(fr.story_time, cf.story_time) AS story_time, cr.chapter_id AS chapter_id"
        " FROM snapshot_fact sf"
        " LEFT JOIN fact_revision fr ON fr.id = sf.fact_revision_id"
        " LEFT JOIN candidate_fact cf ON cf.id = COALESCE(sf.candidate_id, fr.candidate_id)"
        " JOIN check_result cr ON cr.id = cf.check_id"
        " WHERE sf.novel_id = ? AND sf.snapshot_id = ? ORDER BY sf.rowid",
        (novel_id, snapshot_id),
    ).fetchall()
    return tuple(
        SnapshotFactDetail(
            candidate_id=r["candidate_id"],
            fact_revision_id=r["fact_revision_id"],
            kind=FactKind(r["kind"]),
            proposition=r["prop"],
            subject=r["subject"],
            story_time=r["story_time"],
            chapter_id=r["chapter_id"],
        )
        for r in rows
    )


def latest_snapshot_status_for_chapter(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str
) -> SnapshotStatus | None:
    row = conn.execute(
        "SELECT status FROM snapshot WHERE novel_id = ? AND chapter_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (novel_id, chapter_id),
    ).fetchone()
    return None if row is None else SnapshotStatus(row["status"])


def list_messages(conn: sqlite3.Connection, novel_id: str) -> tuple[MessageRecord, ...]:
    rows = conn.execute(
        "SELECT id, role, body, chapter_id, created_at FROM message WHERE novel_id = ?"
        " ORDER BY created_at, rowid",
        (novel_id,),
    ).fetchall()
    return tuple(
        MessageRecord(r["id"], MessageRole(r["role"]), r["body"], r["chapter_id"], r["created_at"])
        for r in rows
    )


def get_review_check_id(conn: sqlite3.Connection, novel_id: str, review_id: str) -> str:
    row = conn.execute(
        "SELECT check_id FROM review WHERE novel_id = ? AND id = ?", (novel_id, review_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "审阅记录不存在。", {"review_id": review_id})
    return row["check_id"]


def list_resolved_issue_ids(conn: sqlite3.Connection, novel_id: str, review_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT issue_id FROM resolution WHERE novel_id = ? AND review_id = ? AND action != ?",
        (novel_id, review_id, ResolutionAction.KEEP_OPEN.value),
    ).fetchall()
    return {r["issue_id"] for r in rows}


def commit_choose_current_draft(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    version_id: str,
    expected_current_draft_id: str | None,
) -> None:
    """作者明确选择某一稿作为当前编辑稿；不新建版本。"""
    with transaction(conn):
        chapter = get_chapter(conn, novel_id, chapter_id)
        version = get_version(conn, novel_id, version_id)
        if version.chapter_id != chapter_id:
            raise WorkbenchError(
                ErrorCode.WRONG_SCOPE, "版本不属于本章。", {"version_id": version_id}
            )
        if chapter.current_draft_id != expected_current_draft_id:
            raise WorkbenchError(
                ErrorCode.STALE_BASIS,
                "当前编辑稿已变化，请重新查看后选择。",
                {"current_draft_id": chapter.current_draft_id},
            )
        conn.execute(
            "UPDATE chapter SET current_draft_id = ? WHERE novel_id = ? AND id = ?",
            (version_id, novel_id, chapter_id),
        )


# ---------------------------------------------------------------- run 流保存、取消与迟到保护（G04）


def get_run(conn: sqlite3.Connection, novel_id: str, run_id: str) -> RunRecord:
    row = conn.execute(
        "SELECT * FROM run WHERE novel_id = ? AND id = ?", (novel_id, run_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "任务不存在。", {"run_id": run_id})
    return RunRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        task=TaskKind(row["task"]),
        status=RunStatus(row["status"]),
        basis_id=row["basis_id"],
        input_version_id=row["input_version_id"],
        source_message_id=row["source_message_id"],
        retry_of_id=row["retry_of_id"],
        partial_text=row["partial_text"],
        result_version_id=row["result_version_id"],
        template_version=row["template_version"],
        request_json=row["request_json"],
        usage_json=row["usage_json"],
        error_code=row["error_code"],
        created_at=row["created_at"],
    )


def any_run_in_flight(conn: sqlite3.Connection, novel_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM run WHERE novel_id = ? AND status IN ('pending', 'running')"
        " ORDER BY created_at LIMIT 1",
        (novel_id,),
    ).fetchone()
    return None if row is None else row["id"]


def commit_run_chunk(conn: sqlite3.Connection, novel_id: str, run_id: str, text: str) -> bool:
    """只有 running 的 run 可以追加流块；返回是否已保存（取消后返回 False）。"""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE run SET partial_text = partial_text || ? WHERE novel_id = ? AND id = ?"
            " AND status = 'running'",
            (text, novel_id, run_id),
        )
        return cur.rowcount == 1


def commit_run_result(
    conn: sqlite3.Connection,
    novel_id: str,
    run_id: str,
    body: str,
    expected_current_draft_id: str | None,
    usage_json: str | None,
) -> SaveResult | None:
    """生成完成：run 仍 running 时创建唯一结果版本并标 succeeded；否则（已取消/中断）返回 None。

    若输入已变（当前编辑稿与任务开始时不同），保存为历史草稿并不移动指针。
    """
    with transaction(conn):
        run = get_run(conn, novel_id, run_id)
        if run.status is not RunStatus.RUNNING:
            return None
        if run.result_version_id is not None:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT, "该任务已有结果版本。", {"run_id": run_id}
            )
        assert run.chapter_id is not None
        result = _insert_version(
            conn,
            novel_id,
            run.chapter_id,
            body,
            VersionOrigin.GENERATED,
            run.input_version_id,
            expected_current_draft_id,
            None,
            run_id,
            True,
        )
        conn.execute(
            "UPDATE run SET status = 'succeeded', result_version_id = ?, partial_text = ?,"
            " usage_json = COALESCE(?, usage_json) WHERE novel_id = ? AND id = ?",
            (result.version_id, body, usage_json, novel_id, run_id),
        )
        return result


def commit_run_cancel(conn: sqlite3.Connection, novel_id: str, run_id: str) -> bool:
    """取消先落库：只有 pending/running 可取消；返回是否真的改变了状态。"""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE run SET status = 'cancelled' WHERE novel_id = ? AND id = ?"
            " AND status IN ('pending', 'running')",
            (novel_id, run_id),
        )
        return cur.rowcount == 1


def list_open_issues_for_check(
    conn: sqlite3.Connection, novel_id: str, check_id: str
) -> tuple[IssueRecord, ...]:
    return get_check(conn, novel_id, check_id).issues


def list_issue_evidence(
    conn: sqlite3.Connection, novel_id: str, issue_id: str
) -> tuple[tuple[int | None, int | None, str], ...]:
    rows = conn.execute(
        "SELECT start, end, quote FROM issue_evidence WHERE novel_id = ? AND issue_id = ?"
        " ORDER BY rowid",
        (novel_id, issue_id),
    ).fetchall()
    return tuple((r["start"], r["end"], r["quote"]) for r in rows)


def latest_check_for_version(
    conn: sqlite3.Connection, novel_id: str, version_id: str
) -> CheckRecord | None:
    row = conn.execute(
        "SELECT id FROM check_result WHERE novel_id = ? AND version_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (novel_id, version_id),
    ).fetchone()
    return None if row is None else get_check(conn, novel_id, row["id"])
