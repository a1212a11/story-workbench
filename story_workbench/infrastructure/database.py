"""SQLite 读写、命名提交函数与 schema 迁移。唯一的 SQL 写入口。

约定：每个连接开启外键；显式 BEGIN IMMEDIATE 事务；历史表只插入；
所有表带 UNIQUE(novel_id, id)，子表用 (novel_id, x_id) 复合外键拒绝跨作品引用；
章节范围的记录再带 UNIQUE(novel_id, chapter_id, id)，指针用三元组拒绝跨章引用。
"""

from __future__ import annotations

import json
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
    BranchMaterializationStatus,
    BranchRecord,
    CandidateFactRecord,
    ChapterRecord,
    CheckInput,
    CheckRecord,
    ErrorCode,
    EvidenceInput,
    FactCorrection,
    FactKind,
    ForeshadowEventRecord,
    ForeshadowStage,
    ImpactRecord,
    ImpactStatus,
    IssueKind,
    IssueRecord,
    Lifecycle,
    MessageRecord,
    MessageRole,
    NovelRecord,
    PlanRecord,
    PublicationRecord,
    RemediationPlanRecord,
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

SCHEMA_VERSION = 4

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
    task TEXT NOT NULL CHECK (task IN (
        'discuss', 'generate', 'inspect', 'impact', 'diagnose', 'recompute')),
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
    adoption_id TEXT,
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

_SCHEMA_V3 = """
CREATE TABLE run_v3 (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT,
    task TEXT NOT NULL CHECK (task IN (
        'discuss', 'generate', 'inspect', 'impact', 'diagnose', 'recompute')),
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
    FOREIGN KEY (novel_id, retry_of_id) REFERENCES run_v3(novel_id, id),
    FOREIGN KEY (novel_id, result_version_id) REFERENCES version(novel_id, id)
);
INSERT INTO run_v3 (
    id, novel_id, created_at, chapter_id, task, basis_id, input_version_id,
    source_message_id, retry_of_id, status, partial_text, result_version_id,
    provider, model, template_version, request_json, usage_json, error_code
) SELECT
    id, novel_id, created_at, chapter_id, task, basis_id, input_version_id,
    source_message_id, retry_of_id, status, partial_text, result_version_id,
    provider, model, template_version, request_json, usage_json, error_code
FROM run;
DROP TABLE run;
ALTER TABLE run_v3 RENAME TO run;
CREATE INDEX idx_run_status ON run(novel_id, status);

CREATE TABLE branch (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    name TEXT NOT NULL,
    parent_branch_id TEXT REFERENCES branch(id),
    fork_adoption_id TEXT,
    fork_version_id TEXT,
    fork_snapshot_id TEXT,
    materialization_status TEXT NOT NULL
        CHECK (materialization_status IN ('staging', 'ready', 'failed')),
    work_subdir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    root_novel_id TEXT NOT NULL,
    forked_from_novel_id TEXT,
    forked_from_adoption_id TEXT,
    UNIQUE (root_novel_id, id),
    UNIQUE (root_novel_id, name),
    UNIQUE (novel_id)
);

CREATE TABLE project_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    main_novel_id TEXT NOT NULL REFERENCES novel(id),
    active_novel_id TEXT NOT NULL REFERENCES novel(id),
    current_branch_id TEXT NOT NULL REFERENCES branch(id)
);

CREATE TABLE publication (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    adoption_id TEXT NOT NULL,
    ready_snapshot_id TEXT NOT NULL,
    author_message_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, request_id),
    UNIQUE (novel_id, chapter_id, version_id),
    FOREIGN KEY (novel_id, chapter_id, version_id) REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, adoption_id) REFERENCES adoption(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, ready_snapshot_id) REFERENCES snapshot(novel_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE publication_divergence (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    old_adoption_id TEXT NOT NULL,
    new_adoption_id TEXT NOT NULL,
    author_message_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, new_adoption_id),
    FOREIGN KEY (novel_id, publication_id) REFERENCES publication(novel_id, id),
    FOREIGN KEY (novel_id, old_adoption_id) REFERENCES adoption(novel_id, id),
    FOREIGN KEY (novel_id, new_adoption_id) REFERENCES adoption(novel_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE remediation_plan (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'adopted')),
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, request_id),
    FOREIGN KEY (novel_id, publication_id) REFERENCES publication(novel_id, id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, source_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE impact_analysis (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    source_chapter_id TEXT NOT NULL,
    baseline_adoption_id TEXT NOT NULL,
    candidate_adoption_id TEXT NOT NULL,
    target_chapter_id TEXT NOT NULL,
    impact_class TEXT NOT NULL CHECK (impact_class IN ('affected', 'unrelated', 'unchecked')),
    summary TEXT NOT NULL,
    author_resolution TEXT,
    run_id TEXT,
    coverage_json TEXT NOT NULL DEFAULT '{"checked":[],"unchecked":[]}',
    UNIQUE (novel_id, id),
    FOREIGN KEY (novel_id, source_chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, target_chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, baseline_adoption_id) REFERENCES adoption(novel_id, id),
    FOREIGN KEY (novel_id, candidate_adoption_id) REFERENCES adoption(novel_id, id),
    FOREIGN KEY (novel_id, run_id) REFERENCES run(novel_id, id)
);

CREATE TABLE impact_evidence (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    impact_id TEXT,
    analysis_id TEXT,
    version_id TEXT,
    start INTEGER,
    end INTEGER,
    quote TEXT NOT NULL DEFAULT '',
    UNIQUE (novel_id, id),
    CHECK ((impact_id IS NOT NULL) + (analysis_id IS NOT NULL) = 1),
    CHECK ((start IS NULL) = (end IS NULL)),
    FOREIGN KEY (novel_id, impact_id) REFERENCES impact(novel_id, id),
    FOREIGN KEY (novel_id, analysis_id) REFERENCES impact_analysis(novel_id, id),
    FOREIGN KEY (novel_id, version_id) REFERENCES version(novel_id, id)
);

CREATE TABLE impact_resolution (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    impact_id TEXT NOT NULL,
    analysis_id TEXT,
    classification TEXT NOT NULL CHECK (classification IN ('affected', 'unrelated', 'unchecked')),
    author_message_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (novel_id, id),
    FOREIGN KEY (novel_id, impact_id) REFERENCES impact(novel_id, id),
    FOREIGN KEY (novel_id, analysis_id) REFERENCES impact_analysis(novel_id, id),
    FOREIGN KEY (novel_id, author_message_id) REFERENCES message(novel_id, id)
);

CREATE TABLE foreshadow_event (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novel(id),
    created_at TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('setup', 'develop', 'reinterpret', 'payoff')),
    chapter_id TEXT NOT NULL,
    adoption_id TEXT NOT NULL,
    version_id TEXT,
    source_message_id TEXT NOT NULL,
    body TEXT NOT NULL,
    quote TEXT NOT NULL,
    UNIQUE (novel_id, id),
    UNIQUE (novel_id, logical_id, id),
    FOREIGN KEY (novel_id, chapter_id) REFERENCES chapter(novel_id, id),
    FOREIGN KEY (novel_id, chapter_id, adoption_id) REFERENCES adoption(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, chapter_id, version_id) REFERENCES version(novel_id, chapter_id, id),
    FOREIGN KEY (novel_id, source_message_id) REFERENCES message(novel_id, id)
);

CREATE INDEX idx_publication_chapter ON publication(novel_id, chapter_id, created_at);
CREATE INDEX idx_foreshadow_chapter ON foreshadow_event(novel_id, chapter_id, created_at);
"""

# v4：run 增加 finished_at（任务进入终态的落库时刻）。用 ALTER TABLE 追加，
# 历史行保持 NULL，不伪造结束时间。
_SCHEMA_V4 = """
ALTER TABLE run ADD COLUMN finished_at TEXT;
"""

_MIGRATIONS: dict[int, str] = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
    3: _SCHEMA_V3,
    4: _SCHEMA_V4,
}

_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}
)


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
    # 重建表（如 v3 的 run）要 DROP 被其他表引用的旧表，外键开启时会在有数据的库上失败。
    # 按 SQLite 的表重建流程：事务外关闭外键，迁移完成后在同一事务内做外键复核，
    # 有违规即回滚；无论成败都恢复外键开关。PRAGMA foreign_keys 在事务内无效，故放在事务外。
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        _migrate_in_transaction(conn, current)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return SCHEMA_VERSION


def _migrate_in_transaction(conn: sqlite3.Connection, current: int) -> None:
    with transaction(conn):
        for target in range(current + 1, SCHEMA_VERSION + 1):
            # executescript 会隐式提交，因此逐条执行以留在同一事务内。
            for statement in _MIGRATIONS[target].split(";"):
                if statement.strip():
                    conn.execute(statement)
        if current < 3:
            novel = conn.execute(
                "SELECT id FROM novel ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if novel is not None:
                exists = conn.execute("SELECT id FROM branch LIMIT 1").fetchone()
                if exists is None:
                    branch_id = new_id()
                    conn.execute(
                        "INSERT INTO branch (id, novel_id, name, parent_branch_id,"
                        " fork_adoption_id, fork_version_id, fork_snapshot_id,"
                        " materialization_status, work_subdir, created_at, root_novel_id,"
                        " forked_from_novel_id, forked_from_adoption_id)"
                        " VALUES (?, ?, '主分支', NULL, NULL, NULL, NULL, 'ready', '', ?, ?,"
                        " NULL, NULL)",
                        (branch_id, novel["id"], now_utc(), novel["id"]),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO project_state"
                        " (id, main_novel_id, active_novel_id, current_branch_id)"
                        " VALUES (1, ?, ?, ?)",
                        (novel["id"], novel["id"], branch_id),
                    )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise WorkbenchError(
                ErrorCode.STORAGE_FAILED,
                "数据库升级后的数据关系校验未通过，本次没有写入。",
                {"violations": [tuple(row) for row in violations[:10]]},
            )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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


def _branch(row: sqlite3.Row, current_id: str | None = None) -> BranchRecord:
    materialization_status = row["materialization_status"]
    if materialization_status == "materialized":
        materialization_status = BranchMaterializationStatus.READY.value
    return BranchRecord(
        id=row["id"],
        name=row["name"],
        root_novel_id=row["root_novel_id"],
        novel_id=row["novel_id"],
        forked_from_novel_id=row["forked_from_novel_id"],
        forked_from_adoption_id=row["forked_from_adoption_id"],
        created_at=row["created_at"],
        is_current=row["id"] == current_id,
        parent_branch_id=row["parent_branch_id"],
        fork_adoption_id=row["fork_adoption_id"],
        fork_version_id=row["fork_version_id"],
        fork_snapshot_id=row["fork_snapshot_id"],
        materialization_status=BranchMaterializationStatus(materialization_status),
        work_subdir=row["work_subdir"],
    )


# ---------------------------------------------------------------- 读


def get_novel(conn: sqlite3.Connection) -> NovelRecord | None:
    branch = conn.execute(
        "SELECT b.* FROM project_state ps JOIN branch b ON b.id = ps.current_branch_id"
        " WHERE ps.id = 1"
    ).fetchone()
    if branch is None:
        row = conn.execute("SELECT id, title, design_revision FROM novel LIMIT 1").fetchone()
    else:
        row = conn.execute(
            "SELECT id, title, design_revision FROM novel WHERE id = ?", (branch["novel_id"],)
        ).fetchone()
    if row is None:
        return None
    return NovelRecord(id=row["id"], title=row["title"], design_revision=row["design_revision"])


def get_novel_by_id(conn: sqlite3.Connection, novel_id: str) -> NovelRecord:
    row = conn.execute(
        "SELECT id, title, design_revision FROM novel WHERE id = ?", (novel_id,)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "作品分支不存在。", {"novel_id": novel_id})
    return NovelRecord(id=row["id"], title=row["title"], design_revision=row["design_revision"])


def current_branch(conn: sqlite3.Connection) -> BranchRecord:
    current = conn.execute("SELECT current_branch_id FROM project_state WHERE id = 1").fetchone()
    if current is None:
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "项目缺少当前分支记录。")
    row = conn.execute(
        "SELECT * FROM branch WHERE id = ?", (current["current_branch_id"],)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "当前分支记录不存在。")
    return _branch(row, current["current_branch_id"])


def list_branches(conn: sqlite3.Connection) -> tuple[BranchRecord, ...]:
    current = conn.execute("SELECT current_branch_id FROM project_state WHERE id = 1").fetchone()
    current_id = None if current is None else current["current_branch_id"]
    root = conn.execute(
        "SELECT root_novel_id FROM branch WHERE id = COALESCE(?, id) LIMIT 1", (current_id,)
    ).fetchone()
    if root is None:
        return ()
    rows = conn.execute(
        "SELECT * FROM branch WHERE root_novel_id = ? ORDER BY created_at, rowid",
        (root["root_novel_id"],),
    ).fetchall()
    return tuple(_branch(r, current_id) for r in rows)


def get_branch(conn: sqlite3.Connection, branch_id: str) -> BranchRecord:
    current = conn.execute("SELECT current_branch_id FROM project_state WHERE id = 1").fetchone()
    row = conn.execute("SELECT * FROM branch WHERE id = ?", (branch_id,)).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "分支不存在。", {"branch_id": branch_id})
    return _branch(row, None if current is None else current["current_branch_id"])


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


def list_workcopies(conn: sqlite3.Connection, novel_id: str) -> tuple[WorkcopyRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM workcopy WHERE novel_id = ? ORDER BY created_at, rowid", (novel_id,)
    ).fetchall()
    return tuple(_workcopy(row) for row in rows)


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
        branch_id = new_id()
        conn.execute(
            "INSERT INTO branch (id, novel_id, name, parent_branch_id, fork_adoption_id,"
            " fork_version_id, fork_snapshot_id, materialization_status, work_subdir,"
            " created_at, root_novel_id, forked_from_novel_id, forked_from_adoption_id)"
            " VALUES (?, ?, '主分支', NULL, NULL, NULL, NULL,"
            " 'ready', '', ?, ?, NULL, NULL)",
            (branch_id, novel_id, now_utc(), novel_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO project_state"
            " (id, main_novel_id, active_novel_id, current_branch_id) VALUES (1, ?, ?, ?)",
            (novel_id, novel_id, branch_id),
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
            # finished_at 记录的是重新打开时的标记时刻，不是模型真实结束时间。
            marked = now_utc()
            conn.executemany(
                "UPDATE run SET status = 'interrupted',"
                " finished_at = COALESCE(finished_at, ?) WHERE novel_id = ? AND id = ?",
                [(marked, novel_id, i) for i in ids],
            )
    return ids


def commit_mark_project_runs_interrupted(conn: sqlite3.Connection) -> tuple[str, ...]:
    """打开项目时把所有分支上次未结束的 run 标为中断；不自动重新调用模型。"""
    with transaction(conn):
        rows = conn.execute(
            "SELECT run.id, run.novel_id FROM run"
            " JOIN branch ON branch.novel_id = run.novel_id"
            " WHERE run.status IN ('pending', 'running')"
            " ORDER BY run.created_at, run.rowid"
        ).fetchall()
        ids = tuple(row["id"] for row in rows)
        if rows:
            # finished_at 记录的是重新打开时的标记时刻，不是模型真实结束时间。
            marked = now_utc()
            conn.executemany(
                "UPDATE run SET status = 'interrupted',"
                " finished_at = COALESCE(finished_at, ?) WHERE novel_id = ? AND id = ?",
                [(marked, row["novel_id"], row["id"]) for row in rows],
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
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """建立一条任务阶段记录；provider/model 记录本次请求使用的供应商与模型名。

    建立时已是终态的记录，finished_at 等于 created_at。
    """
    run_id = new_id()
    created = now_utc()
    finished = created if status in _TERMINAL_RUN_STATUSES else None
    with transaction(conn):
        conn.execute(
            "INSERT INTO run (id, novel_id, created_at, chapter_id, task, basis_id,"
            " input_version_id, source_message_id, retry_of_id, status, template_version,"
            " request_json, provider, model, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                novel_id,
                created,
                chapter_id,
                task.value,
                basis_id,
                input_version_id,
                source_message_id,
                retry_of_id,
                status.value,
                template_version,
                request_json,
                provider,
                model,
                finished,
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
        if current in _TERMINAL_RUN_STATUSES and status is not current:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "任务已结束，不能改变状态；重试请新建任务。",
                {"run_id": run_id, "status": current.value},
            )
        finished = now_utc() if status in _TERMINAL_RUN_STATUSES else None
        conn.execute(
            "UPDATE run SET status = ?, error_code = COALESCE(?, error_code),"
            " usage_json = COALESCE(?, usage_json), finished_at = COALESCE(finished_at, ?)"
            " WHERE novel_id = ? AND id = ?",
            (status.value, error_code, usage_json, finished, novel_id, run_id),
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
    try:
        coverage = json.loads(row["coverage_json"] or "{}")
    except ValueError:
        coverage = {}
    if not isinstance(coverage, dict):
        coverage = {}
    return CheckRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        version_id=row["version_id"],
        basis_id=row["basis_id"],
        run_id=row["run_id"],
        actual_plot=row["actual_plot"],
        actual_ending=row["actual_ending"],
        coverage_checked=tuple(coverage.get("checked", ())),
        coverage_unchecked=tuple(coverage.get("unchecked", ())),
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
        published = None
        if current is not None:
            published = conn.execute(
                "SELECT id FROM publication WHERE novel_id = ? AND adoption_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (novel_id, current.id),
            ).fetchone()
            if published is not None and not action.allow_published_revision:
                raise WorkbenchError(
                    ErrorCode.AUTHORIZATION_REQUIRED,
                    "这一章已有发布记录。若要改已发布原文，请先明确选择发布补救或授权修订已发布原文。",
                    {"publication_id": published["id"], "adoption_id": current.id},
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
            "UPDATE chapter SET adopted_id = ?, current_snapshot_id = NULL, needs_review = 0"
            " WHERE novel_id = ? AND id = ?",
            (adoption_id, novel_id, chapter.id),
        )
        _resolve_pending_impacts(conn, novel_id, chapter.id, review_id)
        if old_snapshot_id is not None:
            conn.execute(
                "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND id = ?"
                " AND status = 'ready'",
                (novel_id, old_snapshot_id),
            )
        if published is not None and current is not None:
            conn.execute(
                "INSERT INTO publication_divergence (id, novel_id, created_at, publication_id,"
                " old_adoption_id, new_adoption_id, author_message_id, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    novel_id,
                    created,
                    published["id"],
                    current.id,
                    adoption_id,
                    action.source_message_id,
                    action.divergence_reason or "作者授权修订已发布原文。",
                ),
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
                "UPDATE chapter SET needs_review = 1, current_snapshot_id = NULL"
                " WHERE novel_id = ? AND id = ?",
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


def commit_publication(
    conn: sqlite3.Connection,
    novel_id: str,
    chapter_id: str,
    version_id: str,
    author_message_id: str,
    note: str = "",
    request_id: str | None = None,
) -> PublicationRecord:
    """标记准确的当前已采用版本为已发布；发布记录只追加，不随本地采用移动。"""
    request = request_id or new_id()
    publication_id = new_id()
    with transaction(conn):
        existing_request = conn.execute(
            "SELECT * FROM publication WHERE novel_id = ? AND request_id = ?",
            (novel_id, request),
        ).fetchone()
        if existing_request is not None:
            return _publication(existing_request)
        chapter = get_chapter(conn, novel_id, chapter_id)
        if chapter.adopted_id is None:
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "本章尚未采用，不能标记发布。")
        adoption = get_adoption(conn, novel_id, chapter.adopted_id)
        if adoption.version_id != version_id:
            raise WorkbenchError(
                ErrorCode.AUTHORIZATION_REQUIRED,
                "发布只能绑定当前已采用的准确版本；修改已发布原文需要明确授权。",
                {"published_version_id": version_id, "adopted_version_id": adoption.version_id},
            )
        if chapter.current_snapshot_id is None:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "故事状态尚未更新完成，不能标记发布。")
        snapshot = get_snapshot(conn, novel_id, chapter.current_snapshot_id)
        if snapshot.status is not SnapshotStatus.READY or snapshot.adoption_id != adoption.id:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "发布需要当前采用版本的 ready 状态快照。")
        existing = conn.execute(
            "SELECT * FROM publication WHERE novel_id = ? AND chapter_id = ? AND version_id = ?",
            (novel_id, chapter_id, version_id),
        ).fetchone()
        if existing is not None:
            return _publication(existing)
        created = now_utc()
        conn.execute(
            "INSERT INTO publication (id, novel_id, created_at, chapter_id, version_id,"
            " adoption_id, ready_snapshot_id, author_message_id, request_id, published_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                publication_id,
                novel_id,
                created,
                chapter_id,
                version_id,
                adoption.id,
                snapshot.id,
                author_message_id,
                request,
                created,
                note,
            ),
        )
    return get_publication(conn, novel_id, publication_id)


def _publication(row: sqlite3.Row) -> PublicationRecord:
    return PublicationRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        version_id=row["version_id"],
        adoption_id=row["adoption_id"],
        ready_snapshot_id=row["ready_snapshot_id"],
        author_message_id=row["author_message_id"],
        request_id=row["request_id"],
        published_at=row["published_at"],
        note=row["note"],
    )


def get_publication(
    conn: sqlite3.Connection, novel_id: str, publication_id: str
) -> PublicationRecord:
    row = conn.execute(
        "SELECT * FROM publication WHERE novel_id = ? AND id = ?", (novel_id, publication_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(
            ErrorCode.NOT_FOUND, "发布记录不存在。", {"publication_id": publication_id}
        )
    return _publication(row)


def list_publications(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str | None = None
) -> tuple[PublicationRecord, ...]:
    if chapter_id is None:
        rows = conn.execute(
            "SELECT * FROM publication WHERE novel_id = ? ORDER BY published_at, rowid",
            (novel_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM publication WHERE novel_id = ? AND chapter_id = ?"
            " ORDER BY published_at, rowid",
            (novel_id, chapter_id),
        ).fetchall()
    return tuple(_publication(r) for r in rows)


def latest_publication_for_chapter(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str
) -> PublicationRecord | None:
    row = conn.execute(
        "SELECT * FROM publication WHERE novel_id = ? AND chapter_id = ?"
        " ORDER BY published_at DESC, rowid DESC LIMIT 1",
        (novel_id, chapter_id),
    ).fetchone()
    return None if row is None else _publication(row)


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
        provider=row["provider"],
        model=row["model"],
        finished_at=row["finished_at"],
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
    *,
    move_pointer: bool = True,
) -> SaveResult | None:
    """生成完成：run 仍 running 时创建唯一结果版本并标 succeeded；否则（已取消/中断）返回 None。

    若输入已变（当前编辑稿与任务开始时不同），保存为历史草稿并不移动指针。
    move_pointer=False 时结果只作候选稿（例如试改），不替换当前编辑稿。
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
            move_pointer,
        )
        conn.execute(
            "UPDATE run SET status = 'succeeded', result_version_id = ?, partial_text = ?,"
            " usage_json = COALESCE(?, usage_json), finished_at = COALESCE(finished_at, ?)"
            " WHERE novel_id = ? AND id = ?",
            (result.version_id, body, usage_json, now_utc(), novel_id, run_id),
        )
        return result


def commit_run_cancel(conn: sqlite3.Connection, novel_id: str, run_id: str) -> bool:
    """取消先落库：只有 pending/running 可取消；返回是否真的改变了状态。"""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE run SET status = 'cancelled', finished_at = COALESCE(finished_at, ?)"
            " WHERE novel_id = ? AND id = ? AND status IN ('pending', 'running')",
            (now_utc(), novel_id, run_id),
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


# ---------------------------------------------------------------- 逐章复核（R02）


def _resolve_pending_impacts(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str, review_id: str
) -> int:
    cur = conn.execute(
        "UPDATE impact SET status = 'resolved', resolved_review_id = ?"
        " WHERE novel_id = ? AND chapter_id = ? AND status = 'pending'",
        (review_id, novel_id, chapter_id),
    )
    return cur.rowcount


def list_pending_impacts(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str
) -> tuple[tuple[str, str], ...]:
    rows = conn.execute(
        "SELECT id, reason FROM impact WHERE novel_id = ? AND chapter_id = ? AND status = 'pending'"
        " ORDER BY created_at, rowid",
        (novel_id, chapter_id),
    ).fetchall()
    return tuple((r["id"], r["reason"]) for r in rows)


def _impact(row: sqlite3.Row) -> ImpactRecord:
    return ImpactRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_id=row["chapter_id"],
        reason=row["reason"],
        status=ImpactStatus(row["status"]),
        old_snapshot_id=row["old_snapshot_id"],
        source_adoption_id=row["source_adoption_id"],
        source_story_item_id=row["source_story_item_id"],
        evidence_issue_id=row["evidence_issue_id"],
        resolved_review_id=row["resolved_review_id"],
    )


def list_impacts(conn: sqlite3.Connection, novel_id: str) -> tuple[ImpactRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM impact WHERE novel_id = ? ORDER BY created_at, rowid", (novel_id,)
    ).fetchall()
    return tuple(_impact(r) for r in rows)


def commit_impact_analysis(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    target_chapter_id: str,
    classification: str,
    summary: str,
    source_adoption_id: str | None = None,
    source_story_item_id: str | None = None,
    run_id: str | None = None,
    evidence: tuple[tuple[str, int | None, int | None, str], ...] = (),
    coverage_checked: tuple[str, ...] = (),
    coverage_unchecked: tuple[str, ...] = (),
) -> str:
    analysis_id = new_id()
    with transaction(conn):
        target = get_chapter(conn, novel_id, target_chapter_id)
        if source_adoption_id is not None:
            source_adoption = get_adoption(conn, novel_id, source_adoption_id)
            source_chapter_id = source_adoption.chapter_id
            baseline_adoption_id = source_adoption.id
        elif target.adopted_id is not None:
            source_chapter_id = target.id
            baseline_adoption_id = target.adopted_id
        else:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "影响分析需要已采用正文作为比较基线。",
                {"chapter_id": target_chapter_id},
            )
        candidate_adoption_id = target.adopted_id or baseline_adoption_id
        impact_class = classification
        if impact_class not in {"affected", "unrelated", "unchecked"}:
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "影响分类无效。")
        created = now_utc()
        conn.execute(
            "INSERT INTO impact_analysis (id, novel_id, created_at, source_chapter_id,"
            " baseline_adoption_id, candidate_adoption_id, target_chapter_id, impact_class,"
            " summary, run_id, coverage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                analysis_id,
                novel_id,
                created,
                source_chapter_id,
                baseline_adoption_id,
                candidate_adoption_id,
                target_chapter_id,
                impact_class,
                summary,
                run_id,
                json.dumps(
                    {"checked": list(coverage_checked), "unchecked": list(coverage_unchecked)},
                    ensure_ascii=False,
                ),
            ),
        )
        _ = source_story_item_id
        for version_id, start, end, quote in evidence:
            if version_id is not None:
                version = get_version(conn, novel_id, version_id)
                if version.chapter_id != target_chapter_id:
                    raise WorkbenchError(
                        ErrorCode.WRONG_SCOPE,
                        "影响分析证据引用了目标章节之外的正文。",
                        {"target_chapter_id": target_chapter_id, "version_id": version_id},
                    )
                if start is None or end is None:
                    raise WorkbenchError(ErrorCode.INVALID_INPUT, "影响分析证据缺少区间。")
                rules.validate_evidence_span(version.body, start, end, quote)
            conn.execute(
                "INSERT INTO impact_evidence (id, novel_id, created_at, analysis_id, version_id,"
                " start, end, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), novel_id, created, analysis_id, version_id, start, end, quote),
            )
    return analysis_id


def get_impact_analysis_for_target(
    conn: sqlite3.Connection, novel_id: str, run_id: str, target_chapter_id: str
) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT id, impact_class FROM impact_analysis"
        " WHERE novel_id = ? AND run_id = ? AND target_chapter_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (novel_id, run_id, target_chapter_id),
    ).fetchone()
    return None if row is None else (row["id"], row["impact_class"])


def commit_impact_resolution(
    conn: sqlite3.Connection,
    novel_id: str,
    impact_id: str,
    classification: str,
    author_message_id: str,
    *,
    analysis_id: str | None = None,
    note: str = "",
) -> str:
    if classification not in {"affected", "unrelated", "unchecked"}:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "影响分类无效。")
    resolution_id = new_id()
    with transaction(conn):
        impact = conn.execute(
            "SELECT * FROM impact WHERE novel_id = ? AND id = ?", (novel_id, impact_id)
        ).fetchone()
        if impact is None:
            raise WorkbenchError(ErrorCode.NOT_FOUND, "影响记录不存在。", {"impact_id": impact_id})
        if impact["status"] != "pending":
            raise WorkbenchError(ErrorCode.STALE_BASIS, "影响记录已处理，请重新查看。")
        target = get_chapter(conn, novel_id, impact["chapter_id"])
        source_adoption_id = impact["source_adoption_id"]
        if source_adoption_id is None:
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "只能确认采用变更产生的影响。")

        created = now_utc()
        if classification != "unrelated":
            conn.execute(
                "INSERT INTO impact_resolution (id, novel_id, created_at, impact_id,"
                " analysis_id, classification, author_message_id, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resolution_id,
                    novel_id,
                    created,
                    impact_id,
                    analysis_id,
                    classification,
                    author_message_id,
                    note,
                ),
            )
            return resolution_id

        existing_unrelated = conn.execute(
            "SELECT id FROM impact_resolution WHERE novel_id = ? AND impact_id = ?"
            " AND classification = 'unrelated' LIMIT 1",
            (novel_id, impact_id),
        ).fetchone()
        if existing_unrelated is not None:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "该影响已确认无关。")
        if analysis_id is None:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "确认无关需要匹配的影响分析。")
        analysis = conn.execute(
            "SELECT * FROM impact_analysis WHERE novel_id = ? AND id = ?",
            (novel_id, analysis_id),
        ).fetchone()
        if analysis is None:
            raise WorkbenchError(
                ErrorCode.NOT_FOUND, "影响分析不存在。", {"analysis_id": analysis_id}
            )
        if (
            analysis["impact_class"] != "unrelated"
            or analysis["target_chapter_id"] != target.id
            or analysis["baseline_adoption_id"] != source_adoption_id
            or analysis["candidate_adoption_id"] != target.adopted_id
        ):
            raise WorkbenchError(ErrorCode.STALE_BASIS, "影响分析与当前影响记录不匹配。")
        if target.adopted_id is None:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "目标章节尚未采用。")

        source_adoption = get_adoption(conn, novel_id, source_adoption_id)
        source_chapter = get_chapter(conn, novel_id, source_adoption.chapter_id)
        if source_chapter.current_snapshot_id is None:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "来源章节状态尚未更新完成。")
        source_snapshot = get_snapshot(conn, novel_id, source_chapter.current_snapshot_id)
        if (
            source_snapshot.status is not SnapshotStatus.READY
            or source_snapshot.adoption_id != source_adoption_id
        ):
            raise WorkbenchError(ErrorCode.STALE_BASIS, "来源章节状态已经变化。")

        previous = _previous_chapter(conn, novel_id, target)
        previous_snapshot_id = None
        if previous is not None:
            if previous.current_snapshot_id is None:
                raise WorkbenchError(ErrorCode.STALE_BASIS, "前章状态尚未更新完成。")
            previous_snapshot = get_snapshot(conn, novel_id, previous.current_snapshot_id)
            if previous_snapshot.status is not SnapshotStatus.READY:
                raise WorkbenchError(ErrorCode.STALE_BASIS, "前章状态尚未更新完成。")
            previous_snapshot_id = previous_snapshot.id

        target_adoption = get_adoption(conn, novel_id, target.adopted_id)
        old_review = conn.execute(
            "SELECT * FROM review WHERE novel_id = ? AND id = ?",
            (novel_id, target_adoption.review_id),
        ).fetchone()
        old_check = conn.execute(
            "SELECT * FROM check_result WHERE novel_id = ? AND id = ?",
            (novel_id, old_review["check_id"]),
        ).fetchone()
        novel = get_novel_by_id(conn, novel_id)
        basis_id = new_id()
        conn.execute(
            "INSERT INTO basis (id, novel_id, created_at, chapter_id, plan_id,"
            " previous_snapshot_id, design_revision, purpose, omitted_sources_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'impact_rebase', '[]')",
            (
                basis_id,
                novel_id,
                created,
                target.id,
                target.current_plan_id,
                previous_snapshot_id,
                novel.design_revision,
            ),
        )
        run_id = new_id()
        conn.execute(
            "INSERT INTO run (id, novel_id, created_at, chapter_id, task, basis_id,"
            " input_version_id, source_message_id, status, template_version, request_json,"
            " finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                novel_id,
                created,
                target.id,
                TaskKind.RECOMPUTE.value,
                basis_id,
                target_adoption.version_id,
                author_message_id,
                RunStatus.SUCCEEDED.value,
                "impact-rebase",
                json.dumps({"impact_id": impact_id, "analysis_id": analysis_id}),
                created,
            ),
        )
        check_id = new_id()
        conn.execute(
            "INSERT INTO check_result (id, novel_id, created_at, chapter_id, version_id,"
            " basis_id, run_id, coverage_json, actual_plot, actual_ending)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                check_id,
                novel_id,
                created,
                target.id,
                target_adoption.version_id,
                basis_id,
                run_id,
                old_check["coverage_json"],
                old_check["actual_plot"],
                old_check["actual_ending"],
            ),
        )

        candidate_map: dict[str, str] = {}
        revision_map: dict[str, str] = {}
        old_candidates = conn.execute(
            "SELECT * FROM candidate_fact WHERE novel_id = ? AND check_id = ? ORDER BY rowid",
            (novel_id, old_check["id"]),
        ).fetchall()
        for candidate in old_candidates:
            candidate_id = new_id()
            candidate_map[candidate["id"]] = candidate_id
            conn.execute(
                "INSERT INTO candidate_fact (id, novel_id, created_at, check_id, kind,"
                " subject, proposition, story_time, evidence_start, evidence_end, quote)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    novel_id,
                    created,
                    check_id,
                    candidate["kind"],
                    candidate["subject"],
                    candidate["proposition"],
                    candidate["story_time"],
                    candidate["evidence_start"],
                    candidate["evidence_end"],
                    candidate["quote"],
                ),
            )
        for candidate in old_candidates:
            revisions = conn.execute(
                "SELECT * FROM fact_revision WHERE novel_id = ? AND candidate_id = ?"
                " ORDER BY created_at, rowid",
                (novel_id, candidate["id"]),
            ).fetchall()
            for revision in revisions:
                revision_id = new_id()
                revision_map[revision["id"]] = revision_id
                conn.execute(
                    "INSERT INTO fact_revision (id, novel_id, created_at, candidate_id,"
                    " previous_revision_id, kind, subject, proposition, story_time,"
                    " evidence_start, evidence_end, quote, author_message_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        novel_id,
                        created,
                        candidate_map[revision["candidate_id"]],
                        None
                        if revision["previous_revision_id"] is None
                        else revision_map[revision["previous_revision_id"]],
                        revision["kind"],
                        revision["subject"],
                        revision["proposition"],
                        revision["story_time"],
                        revision["evidence_start"],
                        revision["evidence_end"],
                        revision["quote"],
                        revision["author_message_id"],
                    ),
                )
            head = conn.execute(
                "SELECT * FROM fact_head WHERE novel_id = ? AND candidate_id = ?",
                (novel_id, candidate["id"]),
            ).fetchone()
            if head is not None:
                conn.execute(
                    "INSERT INTO fact_head (id, novel_id, created_at, candidate_id,"
                    " current_revision_id) VALUES (?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        novel_id,
                        created,
                        candidate_map[head["candidate_id"]],
                        revision_map[head["current_revision_id"]],
                    ),
                )

        review_id = new_id()
        conn.execute(
            "INSERT INTO review (id, novel_id, created_at, chapter_id, version_id,"
            " check_id, basis_id, author_message_id, read_declared, accepted_plot,"
            " accepted_ending) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                review_id,
                novel_id,
                created,
                target.id,
                target_adoption.version_id,
                check_id,
                basis_id,
                author_message_id,
                old_review["accepted_plot"],
                old_review["accepted_ending"],
            ),
        )
        snapshot_id = new_id()
        conn.execute(
            "INSERT INTO snapshot (id, novel_id, created_at, chapter_id, adoption_id,"
            " check_id, review_id, basis_id, previous_snapshot_id, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')",
            (
                snapshot_id,
                novel_id,
                created,
                target.id,
                target_adoption.id,
                check_id,
                review_id,
                basis_id,
                previous_snapshot_id,
            ),
        )
        if previous_snapshot_id is not None:
            for fact in list_snapshot_facts(conn, novel_id, previous_snapshot_id):
                conn.execute(
                    "INSERT INTO snapshot_fact (id, novel_id, created_at, snapshot_id,"
                    " candidate_id, fact_revision_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        novel_id,
                        created,
                        snapshot_id,
                        fact.candidate_id,
                        fact.fact_revision_id,
                    ),
                )
        for candidate in old_candidates:
            head = conn.execute(
                "SELECT current_revision_id FROM fact_head WHERE novel_id = ? AND candidate_id = ?",
                (novel_id, candidate_map[candidate["id"]]),
            ).fetchone()
            conn.execute(
                "INSERT INTO snapshot_fact (id, novel_id, created_at, snapshot_id,"
                " candidate_id, fact_revision_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    novel_id,
                    created,
                    snapshot_id,
                    None if head is not None else candidate_map[candidate["id"]],
                    None if head is None else head["current_revision_id"],
                ),
            )
        conn.execute(
            "INSERT INTO impact_resolution (id, novel_id, created_at, impact_id, analysis_id,"
            " classification, author_message_id, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolution_id,
                novel_id,
                created,
                impact_id,
                analysis_id,
                classification,
                author_message_id,
                note,
            ),
        )
        conn.execute(
            "UPDATE impact SET status = 'resolved', resolved_review_id = ?"
            " WHERE novel_id = ? AND id = ? AND status = 'pending'",
            (review_id, novel_id, impact_id),
        )
        conn.execute(
            "UPDATE chapter SET current_snapshot_id = ?, needs_review = 0"
            " WHERE novel_id = ? AND id = ? AND adopted_id = ?",
            (snapshot_id, novel_id, target.id, target_adoption.id),
        )
    return resolution_id


def list_impact_records(
    conn: sqlite3.Connection, novel_id: str, chapter_id: str | None = None
) -> tuple[ImpactRecord, ...]:
    if chapter_id is None:
        rows = conn.execute(
            "SELECT * FROM impact WHERE novel_id = ? ORDER BY created_at, rowid", (novel_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM impact WHERE novel_id = ? AND chapter_id = ? ORDER BY created_at, rowid",
            (novel_id, chapter_id),
        ).fetchall()
    return tuple(
        ImpactRecord(
            id=r["id"],
            novel_id=r["novel_id"],
            chapter_id=r["chapter_id"],
            reason=r["reason"],
            status=ImpactStatus(r["status"]),
            old_snapshot_id=r["old_snapshot_id"],
            source_adoption_id=r["source_adoption_id"],
            source_story_item_id=r["source_story_item_id"],
            evidence_issue_id=r["evidence_issue_id"],
            resolved_review_id=r["resolved_review_id"],
        )
        for r in rows
    )


def commit_impact_findings(
    conn: sqlite3.Connection,
    novel_id: str,
    source_adoption_id: str,
    findings: tuple[tuple[str, str, tuple[EvidenceInput, ...]], ...],
) -> tuple[str, ...]:
    """保存影响分析发现；每项为 (chapter_id, reason, evidence)。重复 pending reason 跳过。"""
    ids: list[str] = []
    with transaction(conn):
        source_adoption = get_adoption(conn, novel_id, source_adoption_id)
        source_chapter = get_chapter(conn, novel_id, source_adoption.chapter_id)
        created = now_utc()
        for chapter_id, reason, evidence in findings:
            chapter = get_chapter(conn, novel_id, chapter_id)
            if chapter.number <= source_chapter.number:
                raise WorkbenchError(
                    ErrorCode.WRONG_SCOPE,
                    "影响分析只能登记来源章之后的章节。",
                    {"chapter_id": chapter_id},
                )
            existing = conn.execute(
                "SELECT id FROM impact WHERE novel_id = ? AND chapter_id = ?"
                " AND source_adoption_id = ? AND status = 'pending'"
                " ORDER BY created_at, rowid LIMIT 1",
                (novel_id, chapter_id, source_adoption_id),
            ).fetchone()
            if existing is None:
                impact_id = new_id()
                conn.execute(
                    "INSERT INTO impact (id, novel_id, created_at, chapter_id,"
                    " source_adoption_id, reason, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                    (impact_id, novel_id, created, chapter_id, source_adoption_id, reason),
                )
            else:
                impact_id = existing["id"]
            ids.append(impact_id)
            for ev in evidence:
                if ev.version_id is not None:
                    version = get_version(conn, novel_id, ev.version_id)
                    if version.chapter_id != chapter_id:
                        raise WorkbenchError(
                            ErrorCode.MODEL_FAILED,
                            "影响证据引用了不属于该章节的正文。",
                            {"chapter_id": chapter_id, "version_id": ev.version_id},
                        )
                    if ev.start is None or ev.end is None:
                        raise WorkbenchError(ErrorCode.MODEL_FAILED, "影响证据缺少区间。")
                    if not rules.validate_span(version.body, ev.start, ev.end, ev.quote):
                        raise WorkbenchError(ErrorCode.MODEL_FAILED, "影响证据与正文不符。")
                conn.execute(
                    "INSERT INTO impact_evidence (id, novel_id, created_at, impact_id,"
                    " version_id, start, end, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        novel_id,
                        created,
                        impact_id,
                        ev.version_id,
                        ev.start,
                        ev.end,
                        ev.quote,
                    ),
                )
            conn.execute(
                "UPDATE chapter SET needs_review = 1, current_snapshot_id = NULL"
                " WHERE novel_id = ? AND id = ?",
                (novel_id, chapter_id),
            )
    return tuple(ids)


def commit_foreshadow_event(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    logical_id: str | None,
    stage: ForeshadowStage,
    chapter_id: str,
    adoption_id: str,
    source_message_id: str,
    body: str,
    quote: str,
    version_id: str | None = None,
) -> ForeshadowEventRecord:
    if not body.strip():
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "伏笔记录不能为空。")
    event_id = new_id()
    logical = logical_id or new_id()
    with transaction(conn):
        get_chapter(conn, novel_id, chapter_id)
        adoption = get_adoption(conn, novel_id, adoption_id)
        if adoption.chapter_id != chapter_id:
            raise WorkbenchError(ErrorCode.WRONG_SCOPE, "伏笔事件引用的采用记录不属于本章。")
        if version_id is not None:
            version = get_version(conn, novel_id, version_id)
            if version.chapter_id != chapter_id:
                raise WorkbenchError(ErrorCode.WRONG_SCOPE, "伏笔出处版本不属于该章。")
            if quote and quote not in version.body:
                raise WorkbenchError(ErrorCode.INVALID_INPUT, "伏笔引用必须出自所选正式正文。")
        created = now_utc()
        conn.execute(
            "INSERT INTO foreshadow_event (id, novel_id, created_at, logical_id, stage,"
            " chapter_id, adoption_id, version_id, source_message_id, body, quote)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                novel_id,
                created,
                logical,
                stage.value,
                chapter_id,
                adoption_id,
                version_id,
                source_message_id,
                body.strip(),
                quote,
            ),
        )
    return get_foreshadow_event(conn, novel_id, event_id)


def get_foreshadow_event(
    conn: sqlite3.Connection, novel_id: str, event_id: str
) -> ForeshadowEventRecord:
    row = conn.execute(
        "SELECT * FROM foreshadow_event WHERE novel_id = ? AND id = ?", (novel_id, event_id)
    ).fetchone()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "伏笔记录不存在。", {"event_id": event_id})
    return ForeshadowEventRecord(
        id=row["id"],
        novel_id=row["novel_id"],
        logical_id=row["logical_id"],
        stage=ForeshadowStage(row["stage"]),
        chapter_id=row["chapter_id"],
        source_message_id=row["source_message_id"],
        body=row["body"],
        quote=row["quote"],
        adoption_id=row["adoption_id"],
        version_id=row["version_id"],
        created_at=row["created_at"],
    )


def list_foreshadow_events(
    conn: sqlite3.Connection, novel_id: str
) -> tuple[ForeshadowEventRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM foreshadow_event WHERE novel_id = ? ORDER BY created_at, rowid",
        (novel_id,),
    ).fetchall()
    return tuple(get_foreshadow_event(conn, novel_id, r["id"]) for r in rows)


def commit_revalidation(conn: sqlite3.Connection, action: AuthorAction) -> str:
    """前文变化、本章正文未变：保存新审阅与新的候选快照，解除待复核；不新增正文或采用。

    目标必须是本章当前已采用版本；新检查必须基于当前依据。返回新快照 ID。
    """
    novel_id = action.target.novel_id
    with transaction(conn):
        context = load_adoption_context(conn, action)
        current = context.current_adoption
        if current is None or current.version_id != action.target.version_id:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "复核只针对本章当前已采用的正文；正文有变化请走采用流程。",
                {"adopted_version_id": None if current is None else current.version_id},
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
                current.id,
                context.check.id,
                review_id,
                context.basis.id,
                context.basis.previous_snapshot_id,
            ),
        )
        if chapter.current_snapshot_id is not None:
            conn.execute(
                "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND id = ?"
                " AND status = 'ready'",
                (novel_id, chapter.current_snapshot_id),
            )
        conn.execute(
            "UPDATE chapter SET current_snapshot_id = NULL, needs_review = 0"
            " WHERE novel_id = ? AND id = ?",
            (novel_id, chapter.id),
        )
        _resolve_pending_impacts(conn, novel_id, chapter.id, review_id)
    return snapshot_id


# ---------------------------------------------------------------- M3：分支、发布、补救与伏笔


def switch_branch(conn: sqlite3.Connection, branch_id: str) -> NovelRecord:
    with transaction(conn):
        current = current_branch(conn)
        if any_run_in_flight(conn, current.novel_id) is not None:
            raise WorkbenchError(ErrorCode.PROJECT_BUSY, "当前分支有未结束的模型任务。")
        branch = get_branch(conn, branch_id)
        if branch.materialization_status is not BranchMaterializationStatus.READY:
            raise WorkbenchError(ErrorCode.STORAGE_FAILED, "目标分支尚未完成文件物化。")
        if any_run_in_flight(conn, branch.novel_id) is not None:
            raise WorkbenchError(ErrorCode.PROJECT_BUSY, "目标分支有未结束的模型任务。")
        conn.execute(
            "UPDATE project_state SET current_branch_id = ?, active_novel_id = ? WHERE id = 1",
            (branch.id, branch.novel_id),
        )
    return get_novel_by_id(conn, branch.novel_id)


def commit_branch_from_adoption(
    conn: sqlite3.Connection,
    novel_id: str,
    name: str,
    source_adoption_id: str,
) -> BranchRecord:
    branch_name = name.strip()
    if not branch_name:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "分支名称不能为空。")
    if any_run_in_flight(conn, novel_id) is not None:
        raise WorkbenchError(ErrorCode.PROJECT_BUSY, "有模型任务正在进行，暂不能创建分支。")
    branch_id = new_id()
    new_novel_id = new_id()
    with transaction(conn):
        source_branch = current_branch(conn)
        if source_branch.novel_id != novel_id:
            raise WorkbenchError(ErrorCode.WRONG_SCOPE, "只能从当前分支创建新分支。")
        if conn.execute(
            "SELECT id FROM impact WHERE novel_id = ? AND status = 'pending'", (novel_id,)
        ).fetchone():
            raise WorkbenchError(ErrorCode.REVIEW_REQUIRED, "仍有待复核影响，暂不能创建分支。")
        adoption = get_adoption(conn, novel_id, source_adoption_id)
        fork_chapter = get_chapter(conn, novel_id, adoption.chapter_id)
        snapshot = latest_snapshot_for_adoption(conn, novel_id, source_adoption_id)
        if snapshot is None or snapshot.status is not SnapshotStatus.READY:
            raise WorkbenchError(ErrorCode.STALE_BASIS, "分支起点需要采用后的 ready 状态。")
        novel = get_novel_by_id(conn, novel_id)
        created = now_utc()
        conn.execute(
            "INSERT INTO novel (id, created_at, title, design_revision) VALUES (?, ?, ?, ?)",
            (new_novel_id, created, novel.title, novel.design_revision),
        )
        conn.execute(
            "INSERT INTO branch (id, novel_id, name, parent_branch_id, fork_adoption_id,"
            " fork_version_id, fork_snapshot_id, materialization_status, work_subdir,"
            " created_at, root_novel_id, forked_from_novel_id, forked_from_adoption_id)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, 'staging', ?, ?, ?, ?, ?)",
            (
                branch_id,
                new_novel_id,
                branch_name,
                source_branch.id,
                source_adoption_id,
                snapshot.id,
                f"branches/{branch_id}",
                created,
                source_branch.root_novel_id,
                novel_id,
                source_adoption_id,
            ),
        )

        chapters = [c for c in list_chapters(conn, novel_id) if c.number <= fork_chapter.number]
        chapter_map: dict[str, str] = {}
        plan_map: dict[str, str] = {}
        version_map: dict[str, str] = {}
        basis_map: dict[str, str] = {}
        run_map: dict[str, str] = {}
        check_map: dict[str, str] = {}
        review_map: dict[str, str] = {}
        adoption_map: dict[str, str] = {}
        snapshot_map: dict[str, str] = {}
        candidate_map: dict[str, str] = {}
        revision_map: dict[str, str] = {}
        story_item_map: dict[str, str] = {}
        basis_source_map: dict[str, str] = {}
        issue_map: dict[str, str] = {}
        message_map: dict[str, str] = {}

        def require_mapped(mapping: dict[str, str], key: str, label: str) -> str:
            try:
                return mapping[key]
            except KeyError as exc:
                raise WorkbenchError(
                    ErrorCode.STALE_BASIS,
                    "分支复制缺少正式闭包映射。",
                    {"kind": label, "id": key},
                ) from exc

        for chapter in chapters:
            new_chapter_id = new_id()
            chapter_map[chapter.id] = new_chapter_id
            conn.execute(
                "INSERT INTO chapter (id, novel_id, created_at, number) VALUES (?, ?, ?, ?)",
                (new_chapter_id, new_novel_id, created, chapter.number),
            )

        def copy_message(
            old_message_id: str | None,
            preferred_chapter_id: str | None = None,
            *,
            fallback: str | None = None,
        ) -> str:
            if old_message_id is not None and old_message_id in message_map:
                return message_map[old_message_id]
            row = None
            if old_message_id is not None:
                row = conn.execute(
                    "SELECT role, body, chapter_id FROM message WHERE novel_id = ? AND id = ?",
                    (novel_id, old_message_id),
                ).fetchone()
            old_chapter_id = None if row is None else row["chapter_id"]
            new_chapter_id = chapter_map.get(old_chapter_id, preferred_chapter_id)
            message_id = new_id()
            conn.execute(
                "INSERT INTO message (id, novel_id, created_at, role, body, chapter_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    new_novel_id,
                    created,
                    MessageRole.AUTHOR.value if row is None else row["role"],
                    fallback if row is None else row["body"],
                    new_chapter_id,
                ),
            )
            if old_message_id is not None:
                message_map[old_message_id] = message_id
            return message_id

        for chapter in chapters:
            for plan in conn.execute(
                "SELECT * FROM plan WHERE novel_id = ? AND chapter_id = ? ORDER BY revision",
                (novel_id, chapter.id),
            ).fetchall():
                new_plan_id = new_id()
                plan_map[plan["id"]] = new_plan_id
                conn.execute(
                    "INSERT INTO plan (id, novel_id, created_at, chapter_id, revision, plot,"
                    " ending, source_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_plan_id,
                        new_novel_id,
                        plan["created_at"],
                        chapter_map[chapter.id],
                        plan["revision"],
                        plan["plot"],
                        plan["ending"],
                        copy_message(plan["source_message_id"], chapter_map[chapter.id]),
                    ),
                )
            if chapter.current_plan_id is not None:
                conn.execute(
                    "UPDATE chapter SET current_plan_id = ? WHERE novel_id = ? AND id = ?",
                    (
                        require_mapped(plan_map, chapter.current_plan_id, "plan"),
                        new_novel_id,
                        chapter_map[chapter.id],
                    ),
                )

        def copy_story_item(old_item_id: str) -> str:
            if old_item_id in story_item_map:
                return story_item_map[old_item_id]
            row = conn.execute(
                "SELECT * FROM story_item WHERE novel_id = ? AND id = ?", (novel_id, old_item_id)
            ).fetchone()
            if row is None:
                raise WorkbenchError(
                    ErrorCode.NOT_FOUND,
                    "故事条目不存在。",
                    {"item_id": old_item_id},
                )
            if row["chapter_id"] is not None and row["chapter_id"] not in chapter_map:
                raise WorkbenchError(ErrorCode.WRONG_SCOPE, "分支不能复制未来章节故事条目。")
            supersedes_id = None
            if row["supersedes_id"] is not None:
                supersedes_id = copy_story_item(row["supersedes_id"])
            item_id = new_id()
            story_item_map[old_item_id] = item_id
            conn.execute(
                "INSERT INTO story_item (id, novel_id, created_at, logical_id, revision, kind,"
                " body, authority, lifecycle, source_message_id, supersedes_id, chapter_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    new_novel_id,
                    row["created_at"],
                    row["logical_id"],
                    row["revision"],
                    row["kind"],
                    row["body"],
                    row["authority"],
                    row["lifecycle"],
                    copy_message(row["source_message_id"], chapter_map.get(row["chapter_id"])),
                    supersedes_id,
                    None if row["chapter_id"] is None else chapter_map[row["chapter_id"]],
                ),
            )
            return item_id

        for item in list_story_items_current(conn, novel_id):
            if item.chapter_id is None or item.chapter_id in chapter_map:
                copy_story_item(item.id)

        needed_adoptions = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM adoption WHERE novel_id = ? AND chapter_id IN ("
                + ",".join("?" for _ in chapter_map)
                + ")",
                (novel_id, *chapter_map.keys()),
            ).fetchall()
        }

        def copy_version(old_version_id: str) -> str:
            if old_version_id in version_map:
                return version_map[old_version_id]
            row = conn.execute(
                "SELECT * FROM version WHERE novel_id = ? AND id = ?",
                (novel_id, old_version_id),
            ).fetchone()
            if row is None:
                raise WorkbenchError(
                    ErrorCode.NOT_FOUND, "正文版本不存在。", {"version_id": old_version_id}
                )
            if row["chapter_id"] not in chapter_map:
                raise WorkbenchError(ErrorCode.WRONG_SCOPE, "分支不能复制未来章节正文。")
            version_id = new_id()
            version_map[old_version_id] = version_id
            conn.execute(
                "INSERT INTO version (id, novel_id, created_at, chapter_id, parent_id,"
                " restored_from_id, body, content_hash, origin, run_id)"
                " VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL)",
                (
                    version_id,
                    new_novel_id,
                    row["created_at"],
                    chapter_map[row["chapter_id"]],
                    row["body"],
                    row["content_hash"],
                    row["origin"],
                ),
            )
            return version_id

        def copy_basis_source_value(
            source: sqlite3.Row, new_basis_id: str, preferred_chapter_id: str | None
        ) -> None:
            version_id = None
            story_item_id = None
            snapshot_id = None
            fact_revision_id = None
            message_id = None
            if source["version_id"] is not None:
                version_id = copy_version(source["version_id"])
            if source["story_item_id"] is not None:
                story_item_id = copy_story_item(source["story_item_id"])
            if source["snapshot_id"] is not None:
                snapshot_id = require_mapped(snapshot_map, source["snapshot_id"], "snapshot")
            if source["fact_revision_id"] is not None:
                fact_revision_id = require_mapped(
                    revision_map, source["fact_revision_id"], "fact_revision"
                )
            if source["message_id"] is not None:
                message_id = copy_message(source["message_id"], preferred_chapter_id)
            basis_source_id = new_id()
            basis_source_map[source["id"]] = basis_source_id
            conn.execute(
                "INSERT INTO basis_source (id, novel_id, created_at, basis_id, version_id,"
                " story_item_id, snapshot_id, fact_revision_id, message_id, role,"
                " excerpt_start, excerpt_end, quote)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    basis_source_id,
                    new_novel_id,
                    source["created_at"],
                    new_basis_id,
                    version_id,
                    story_item_id,
                    snapshot_id,
                    fact_revision_id,
                    message_id,
                    source["role"],
                    source["excerpt_start"],
                    source["excerpt_end"],
                    source["quote"],
                ),
            )

        def copy_basis(old_basis_id: str, new_chapter_id: str) -> str:
            if old_basis_id in basis_map:
                return basis_map[old_basis_id]
            row = conn.execute(
                "SELECT * FROM basis WHERE novel_id = ? AND id = ?", (novel_id, old_basis_id)
            ).fetchone()
            plan_id = None
            if row["plan_id"] is not None:
                plan_id = require_mapped(plan_map, row["plan_id"], "plan")
            previous_snapshot_id = None
            if row["previous_snapshot_id"] is not None:
                previous_snapshot_id = require_mapped(
                    snapshot_map, row["previous_snapshot_id"], "snapshot"
                )
            basis_id = new_id()
            basis_map[old_basis_id] = basis_id
            conn.execute(
                "INSERT INTO basis (id, novel_id, created_at, chapter_id, plan_id,"
                " previous_snapshot_id, design_revision, purpose, omitted_sources_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    basis_id,
                    new_novel_id,
                    row["created_at"],
                    new_chapter_id,
                    plan_id,
                    previous_snapshot_id,
                    row["design_revision"],
                    row["purpose"],
                    row["omitted_sources_json"],
                ),
            )
            for source in conn.execute(
                "SELECT * FROM basis_source WHERE novel_id = ? AND basis_id = ? ORDER BY rowid",
                (novel_id, old_basis_id),
            ).fetchall():
                copy_basis_source_value(source, basis_id, new_chapter_id)
            return basis_id

        def copy_check_facts(old_check_id: str, new_check_id: str, new_chapter_id: str) -> None:
            old_candidates = conn.execute(
                "SELECT * FROM candidate_fact WHERE novel_id = ? AND check_id = ? ORDER BY rowid",
                (novel_id, old_check_id),
            ).fetchall()
            for candidate in old_candidates:
                candidate_id = new_id()
                candidate_map[candidate["id"]] = candidate_id
                conn.execute(
                    "INSERT INTO candidate_fact (id, novel_id, created_at, check_id, kind,"
                    " subject, proposition, story_time, evidence_start, evidence_end, quote)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        new_novel_id,
                        candidate["created_at"],
                        new_check_id,
                        candidate["kind"],
                        candidate["subject"],
                        candidate["proposition"],
                        candidate["story_time"],
                        candidate["evidence_start"],
                        candidate["evidence_end"],
                        candidate["quote"],
                    ),
                )
            for candidate in old_candidates:
                revisions = conn.execute(
                    "SELECT * FROM fact_revision WHERE novel_id = ? AND candidate_id = ?"
                    " ORDER BY created_at, rowid",
                    (novel_id, candidate["id"]),
                ).fetchall()
                for revision in revisions:
                    revision_id = new_id()
                    revision_map[revision["id"]] = revision_id
                    conn.execute(
                        "INSERT INTO fact_revision (id, novel_id, created_at, candidate_id,"
                        " previous_revision_id, kind, subject, proposition, story_time,"
                        " evidence_start, evidence_end, quote, author_message_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            revision_id,
                            new_novel_id,
                            revision["created_at"],
                            candidate_map[revision["candidate_id"]],
                            None
                            if revision["previous_revision_id"] is None
                            else revision_map[revision["previous_revision_id"]],
                            revision["kind"],
                            revision["subject"],
                            revision["proposition"],
                            revision["story_time"],
                            revision["evidence_start"],
                            revision["evidence_end"],
                            revision["quote"],
                            copy_message(revision["author_message_id"], new_chapter_id),
                        ),
                    )
                head = conn.execute(
                    "SELECT * FROM fact_head WHERE novel_id = ? AND candidate_id = ?",
                    (novel_id, candidate["id"]),
                ).fetchone()
                if head is not None:
                    conn.execute(
                        "INSERT INTO fact_head (id, novel_id, created_at, candidate_id,"
                        " current_revision_id) VALUES (?, ?, ?, ?, ?)",
                        (
                            new_id(),
                            new_novel_id,
                            head["created_at"],
                            candidate_map[head["candidate_id"]],
                            revision_map[head["current_revision_id"]],
                        ),
                    )
            old_issues = conn.execute(
                "SELECT * FROM issue WHERE novel_id = ? AND check_id = ? ORDER BY rowid",
                (novel_id, old_check_id),
            ).fetchall()
            for issue in old_issues:
                issue_id = new_id()
                issue_map[issue["id"]] = issue_id
                conn.execute(
                    "INSERT INTO issue (id, novel_id, created_at, check_id, kind,"
                    " explanation, proposed_change) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        issue_id,
                        new_novel_id,
                        issue["created_at"],
                        new_check_id,
                        issue["kind"],
                        issue["explanation"],
                        issue["proposed_change"],
                    ),
                )
                for evidence in conn.execute(
                    "SELECT * FROM issue_evidence WHERE novel_id = ? AND issue_id = ?"
                    " ORDER BY rowid",
                    (novel_id, issue["id"]),
                ).fetchall():
                    version_id = None
                    basis_source_id = None
                    if evidence["version_id"] is not None:
                        version_id = require_mapped(version_map, evidence["version_id"], "version")
                    if evidence["basis_source_id"] is not None:
                        basis_source_id = require_mapped(
                            basis_source_map, evidence["basis_source_id"], "basis_source"
                        )
                    conn.execute(
                        "INSERT INTO issue_evidence (id, novel_id, created_at, issue_id,"
                        " version_id, basis_source_id, start, end, quote)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id(),
                            new_novel_id,
                            evidence["created_at"],
                            issue_id,
                            version_id,
                            basis_source_id,
                            evidence["start"],
                            evidence["end"],
                            evidence["quote"],
                        ),
                    )

        def copy_review(old_review_id: str, new_chapter_id: str) -> str:
            if old_review_id in review_map:
                return review_map[old_review_id]
            old_review = conn.execute(
                "SELECT * FROM review WHERE novel_id = ? AND id = ?",
                (novel_id, old_review_id),
            ).fetchone()
            old_check = conn.execute(
                "SELECT * FROM check_result WHERE novel_id = ? AND id = ?",
                (novel_id, old_review["check_id"]),
            ).fetchone()
            version_id = copy_version(old_review["version_id"])
            basis_id = copy_basis(old_check["basis_id"], new_chapter_id)
            old_run = conn.execute(
                "SELECT * FROM run WHERE novel_id = ? AND id = ?",
                (novel_id, old_check["run_id"]),
            ).fetchone()
            run_id = new_id()
            run_map[old_run["id"]] = run_id
            conn.execute(
                "INSERT INTO run (id, novel_id, created_at, chapter_id, task, basis_id,"
                " input_version_id, source_message_id, retry_of_id, status, partial_text,"
                " result_version_id, provider, model, template_version, request_json, usage_json,"
                " error_code, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    new_novel_id,
                    old_run["created_at"],
                    new_chapter_id,
                    old_run["task"],
                    basis_id,
                    version_id,
                    copy_message(old_run["source_message_id"], new_chapter_id),
                    old_run["status"],
                    old_run["partial_text"],
                    copy_version(old_run["result_version_id"])
                    if old_run["result_version_id"] is not None
                    else None,
                    old_run["provider"],
                    old_run["model"],
                    old_run["template_version"],
                    old_run["request_json"],
                    old_run["usage_json"],
                    old_run["error_code"],
                    old_run["finished_at"],
                ),
            )
            check_id = new_id()
            check_map[old_check["id"]] = check_id
            conn.execute(
                "INSERT INTO check_result (id, novel_id, created_at, chapter_id, version_id,"
                " basis_id, run_id, coverage_json, actual_plot, actual_ending)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    check_id,
                    new_novel_id,
                    old_check["created_at"],
                    new_chapter_id,
                    version_id,
                    basis_id,
                    run_id,
                    old_check["coverage_json"],
                    old_check["actual_plot"],
                    old_check["actual_ending"],
                ),
            )
            copy_check_facts(old_check["id"], check_id, new_chapter_id)
            review_id = new_id()
            review_map[old_review_id] = review_id
            conn.execute(
                "INSERT INTO review (id, novel_id, created_at, chapter_id, version_id,"
                " check_id, basis_id, author_message_id, read_declared, accepted_plot,"
                " accepted_ending) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    new_novel_id,
                    old_review["created_at"],
                    new_chapter_id,
                    version_id,
                    check_id,
                    basis_id,
                    copy_message(old_review["author_message_id"], new_chapter_id),
                    old_review["read_declared"],
                    old_review["accepted_plot"],
                    old_review["accepted_ending"],
                ),
            )
            for resolution in conn.execute(
                "SELECT * FROM resolution WHERE novel_id = ? AND review_id = ? ORDER BY rowid",
                (novel_id, old_review_id),
            ).fetchall():
                decision_revision_id = None
                fact_revision_id = None
                if resolution["decision_revision_id"] is not None:
                    decision_revision_id = require_mapped(
                        story_item_map, resolution["decision_revision_id"], "story_item"
                    )
                if resolution["fact_revision_id"] is not None:
                    fact_revision_id = require_mapped(
                        revision_map, resolution["fact_revision_id"], "fact_revision"
                    )
                conn.execute(
                    "INSERT INTO resolution (id, novel_id, created_at, review_id, issue_id,"
                    " action, author_message_id, explanation, decision_revision_id,"
                    " fact_revision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        new_novel_id,
                        resolution["created_at"],
                        review_id,
                        require_mapped(issue_map, resolution["issue_id"], "issue"),
                        resolution["action"],
                        copy_message(resolution["author_message_id"], new_chapter_id),
                        resolution["explanation"],
                        decision_revision_id,
                        fact_revision_id,
                    ),
                )
            return review_id

        def copy_snapshots(old_adoption_id: str, new_chapter_id: str) -> None:
            rows = conn.execute(
                "SELECT * FROM snapshot WHERE novel_id = ? AND adoption_id = ? ORDER BY rowid",
                (novel_id, old_adoption_id),
            ).fetchall()
            for row in rows:
                copy_review(row["review_id"], new_chapter_id)
                snapshot_id = new_id()
                snapshot_map[row["id"]] = snapshot_id
                conn.execute(
                    "INSERT INTO snapshot (id, novel_id, created_at, chapter_id, adoption_id,"
                    " check_id, review_id, basis_id, previous_snapshot_id, status, error_code)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        new_novel_id,
                        row["created_at"],
                        new_chapter_id,
                        adoption_map[row["adoption_id"]],
                        require_mapped(check_map, row["check_id"], "check"),
                        require_mapped(review_map, row["review_id"], "review"),
                        require_mapped(basis_map, row["basis_id"], "basis"),
                        None
                        if row["previous_snapshot_id"] is None
                        else require_mapped(snapshot_map, row["previous_snapshot_id"], "snapshot"),
                        row["status"],
                        row["error_code"],
                    ),
                )
                facts = conn.execute(
                    "SELECT * FROM snapshot_fact WHERE novel_id = ? AND snapshot_id = ?"
                    " ORDER BY rowid",
                    (novel_id, row["id"]),
                ).fetchall()
                for fact in facts:
                    candidate_id = None
                    revision_id = None
                    if fact["candidate_id"] is not None:
                        candidate_id = require_mapped(
                            candidate_map, fact["candidate_id"], "candidate_fact"
                        )
                    if fact["fact_revision_id"] is not None:
                        revision_id = require_mapped(
                            revision_map, fact["fact_revision_id"], "fact_revision"
                        )
                    conn.execute(
                        "INSERT INTO snapshot_fact (id, novel_id, created_at, snapshot_id,"
                        " candidate_id, fact_revision_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            new_id(),
                            new_novel_id,
                            fact["created_at"],
                            snapshot_id,
                            candidate_id,
                            revision_id,
                        ),
                    )

        def copy_adoption(old_adoption_id: str) -> str:
            if old_adoption_id in adoption_map:
                return adoption_map[old_adoption_id]
            old = conn.execute(
                "SELECT * FROM adoption WHERE novel_id = ? AND id = ?", (novel_id, old_adoption_id)
            ).fetchone()
            if old["chapter_id"] not in chapter_map:
                raise WorkbenchError(ErrorCode.WRONG_SCOPE, "分支不能复制未来章节采用记录。")
            new_chapter_id = chapter_map[old["chapter_id"]]
            previous_id = None
            if old["previous_adoption_id"] in needed_adoptions:
                previous_id = copy_adoption(old["previous_adoption_id"])
            version_id = copy_version(old["version_id"])
            review_id = copy_review(old["review_id"], new_chapter_id)
            adoption_id = new_id()
            adoption_map[old["id"]] = adoption_id
            conn.execute(
                "INSERT INTO adoption (id, novel_id, created_at, chapter_id, version_id,"
                " review_id, previous_adoption_id, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    adoption_id,
                    new_novel_id,
                    old["created_at"],
                    new_chapter_id,
                    version_id,
                    review_id,
                    previous_id,
                    old["request_id"],
                ),
            )
            copy_snapshots(old["id"], new_chapter_id)
            return adoption_id

        adoption_order = conn.execute(
            "SELECT a.id FROM adoption a JOIN chapter c ON c.novel_id = a.novel_id"
            " AND c.id = a.chapter_id WHERE a.novel_id = ?"
            " ORDER BY c.number, a.created_at, a.rowid",
            (novel_id,),
        ).fetchall()
        for row in adoption_order:
            if row["id"] in needed_adoptions:
                copy_adoption(row["id"])

        for chapter in chapters:
            new_chapter_id = chapter_map[chapter.id]
            if chapter.adopted_id is None:
                continue
            copied_adoption = adoption_map[chapter.adopted_id]
            copied_source_adoption = get_adoption(conn, novel_id, chapter.adopted_id)
            copied_version = version_map[copied_source_adoption.version_id]
            copied_snapshot = (
                None
                if chapter.current_snapshot_id is None
                else snapshot_map.get(chapter.current_snapshot_id)
            )
            conn.execute(
                "UPDATE chapter SET current_draft_id = ?, adopted_id = ?, current_snapshot_id = ?"
                " WHERE novel_id = ? AND id = ?",
                (copied_version, copied_adoption, copied_snapshot, new_novel_id, new_chapter_id),
            )

        publication_map: dict[str, str] = {}
        pub_rows = conn.execute(
            "SELECT * FROM publication WHERE novel_id = ? AND chapter_id IN ("
            + ",".join("?" for _ in chapter_map)
            + ") ORDER BY published_at, rowid",
            (novel_id, *chapter_map.keys()),
        ).fetchall()
        for pub in pub_rows:
            publication_id = new_id()
            publication_map[pub["id"]] = publication_id
            conn.execute(
                "INSERT INTO publication (id, novel_id, created_at, chapter_id, version_id,"
                " adoption_id, ready_snapshot_id, author_message_id, request_id,"
                " published_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    publication_id,
                    new_novel_id,
                    pub["created_at"],
                    chapter_map[pub["chapter_id"]],
                    version_map[pub["version_id"]],
                    adoption_map[pub["adoption_id"]],
                    snapshot_map[pub["ready_snapshot_id"]],
                    copy_message(pub["author_message_id"], chapter_map[pub["chapter_id"]]),
                    pub["request_id"],
                    pub["published_at"],
                    pub["note"],
                ),
            )
        for div in conn.execute(
            "SELECT * FROM publication_divergence WHERE novel_id = ? ORDER BY created_at, rowid",
            (novel_id,),
        ).fetchall():
            if div["publication_id"] not in publication_map:
                continue
            if (
                div["old_adoption_id"] not in adoption_map
                or div["new_adoption_id"] not in adoption_map
            ):
                continue
            publication = conn.execute(
                "SELECT chapter_id FROM publication WHERE novel_id = ? AND id = ?",
                (novel_id, div["publication_id"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO publication_divergence (id, novel_id, created_at, publication_id,"
                " old_adoption_id, new_adoption_id, author_message_id, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    new_novel_id,
                    div["created_at"],
                    publication_map[div["publication_id"]],
                    adoption_map[div["old_adoption_id"]],
                    adoption_map[div["new_adoption_id"]],
                    copy_message(div["author_message_id"], chapter_map[publication["chapter_id"]]),
                    div["reason"],
                ),
            )
        for plan in conn.execute(
            "SELECT * FROM remediation_plan WHERE novel_id = ? ORDER BY created_at, rowid",
            (novel_id,),
        ).fetchall():
            if plan["publication_id"] not in publication_map:
                continue
            conn.execute(
                "INSERT INTO remediation_plan (id, novel_id, created_at, publication_id,"
                " chapter_id, source_message_id, request_id, body, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    new_novel_id,
                    plan["created_at"],
                    publication_map[plan["publication_id"]],
                    chapter_map[plan["chapter_id"]],
                    copy_message(plan["source_message_id"], chapter_map[plan["chapter_id"]]),
                    plan["request_id"],
                    plan["body"],
                    plan["status"],
                ),
            )
        for event in conn.execute(
            "SELECT * FROM foreshadow_event WHERE novel_id = ? ORDER BY created_at, rowid",
            (novel_id,),
        ).fetchall():
            if event["chapter_id"] not in chapter_map:
                continue
            if event["adoption_id"] not in adoption_map:
                continue
            conn.execute(
                "INSERT INTO foreshadow_event (id, novel_id, created_at, logical_id, stage,"
                " chapter_id, adoption_id, version_id, source_message_id, body, quote)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    new_novel_id,
                    event["created_at"],
                    event["logical_id"],
                    event["stage"],
                    chapter_map[event["chapter_id"]],
                    adoption_map[event["adoption_id"]],
                    None if event["version_id"] is None else version_map[event["version_id"]],
                    copy_message(event["source_message_id"], chapter_map[event["chapter_id"]]),
                    event["body"],
                    event["quote"],
                ),
            )
    return get_branch(conn, branch_id)


def commit_branch_ready(conn: sqlite3.Connection, branch_id: str) -> BranchRecord:
    with transaction(conn):
        branch = get_branch(conn, branch_id)
        conn.execute(
            "UPDATE branch SET materialization_status = ? WHERE id = ?",
            (BranchMaterializationStatus.READY.value, branch.id),
        )
    return get_branch(conn, branch_id)


def commit_branch_materialized(conn: sqlite3.Connection, branch_id: str) -> BranchRecord:
    return commit_branch_ready(conn, branch_id)


def _copy_message(
    conn: sqlite3.Connection,
    old_novel_id: str,
    new_novel_id: str,
    old_message_id: str,
    new_chapter_id: str,
    *,
    fallback: str | None = None,
) -> str:
    row = conn.execute(
        "SELECT role, body FROM message WHERE novel_id = ? AND id = ?",
        (old_novel_id, old_message_id),
    ).fetchone()
    message_id = new_id()
    conn.execute(
        "INSERT INTO message (id, novel_id, created_at, role, body, chapter_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            message_id,
            new_novel_id,
            now_utc(),
            MessageRole.AUTHOR.value if row is None else row["role"],
            fallback if row is None else row["body"],
            new_chapter_id,
        ),
    )
    return message_id


def list_remediation_plans(
    conn: sqlite3.Connection, novel_id: str, publication_id: str | None = None
) -> tuple[RemediationPlanRecord, ...]:
    if publication_id is None:
        rows = conn.execute(
            "SELECT * FROM remediation_plan WHERE novel_id = ? ORDER BY created_at, rowid",
            (novel_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM remediation_plan WHERE novel_id = ? AND publication_id = ?"
            " ORDER BY created_at, rowid",
            (novel_id, publication_id),
        ).fetchall()
    return tuple(
        RemediationPlanRecord(
            r["id"],
            r["novel_id"],
            r["publication_id"],
            r["chapter_id"],
            r["body"],
            r["status"],
            r["source_message_id"],
            r["created_at"],
            r["request_id"],
        )
        for r in rows
    )


def commit_remediation_plan(
    conn: sqlite3.Connection,
    novel_id: str,
    publication_id: str,
    source_message_id: str,
    body: str,
    request_id: str | None = None,
) -> RemediationPlanRecord:
    plan_id = new_id()
    request_id = request_id or new_id()
    with transaction(conn):
        publication = get_publication(conn, novel_id, publication_id)
        existing = conn.execute(
            "SELECT * FROM remediation_plan WHERE novel_id = ? AND request_id = ?",
            (novel_id, request_id),
        ).fetchone()
        if existing is not None:
            return RemediationPlanRecord(
                existing["id"],
                novel_id,
                publication_id,
                publication.chapter_id,
                existing["body"],
                existing["status"],
                existing["source_message_id"],
                existing["created_at"],
                existing["request_id"],
            )
        created = now_utc()
        conn.execute(
            "INSERT INTO remediation_plan (id, novel_id, created_at, publication_id,"
            " chapter_id, source_message_id, request_id, body, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed')",
            (
                plan_id,
                novel_id,
                created,
                publication_id,
                publication.chapter_id,
                source_message_id,
                request_id,
                body,
            ),
        )
    return RemediationPlanRecord(
        plan_id,
        novel_id,
        publication_id,
        publication.chapter_id,
        body,
        "proposed",
        source_message_id,
        created,
        request_id,
    )


# ---------------------------------------------------------------- 备份（R04）


def backup_to(conn: sqlite3.Connection, destination: Path) -> None:
    """用 SQLite 一致性备份 API 复制数据库；目标文件必须不存在。"""
    if destination.exists():
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT, "备份目标已存在。", {"path": str(destination)}
        )
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()


def verify_database_file(path: Path) -> tuple[int, bool]:
    """返回 (schema 版本, 完整性与外键是否通过)。只读打开，不迁移。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ok = ok and not conn.execute("PRAGMA foreign_key_check").fetchall()
        return version, ok
    finally:
        conn.close()


def verify_references(conn: sqlite3.Connection, novel_id: str) -> list[str]:
    """跨行引用复核：章节指针指向的记录存在且属于同章；返回问题列表。"""
    problems: list[str] = []
    for chapter in list_chapters(conn, novel_id):
        if chapter.adopted_id is not None:
            adoption = get_adoption(conn, novel_id, chapter.adopted_id)
            if adoption.chapter_id != chapter.id:
                problems.append(f"第 {chapter.number} 章的采用记录不属于本章")
        if chapter.current_snapshot_id is not None:
            snapshot = get_snapshot(conn, novel_id, chapter.current_snapshot_id)
            if snapshot.chapter_id != chapter.id or snapshot.status is not SnapshotStatus.READY:
                problems.append(f"第 {chapter.number} 章的当前状态不是有效快照")
        if chapter.current_draft_id is not None:
            version = get_version(conn, novel_id, chapter.current_draft_id)
            if version.chapter_id != chapter.id:
                problems.append(f"第 {chapter.number} 章的当前编辑稿不属于本章")
    return problems
