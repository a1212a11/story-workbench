"""跨层共享的故事记录、动作与错误。

只放 domain、application、ai、infrastructure 都需要的记录；供应商 DTO、数据库行、
用例专有参数不放这里。字段只允许追加，不改语义（见架构 V0.2 第 4 节）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------- 错误


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    WRONG_SCOPE = "WRONG_SCOPE"
    STALE_BASIS = "STALE_BASIS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    MODEL_FAILED = "MODEL_FAILED"
    STORAGE_FAILED = "STORAGE_FAILED"
    WORKCOPY_CONFLICT = "WORKCOPY_CONFLICT"
    PROJECT_BUSY = "PROJECT_BUSY"


class WorkbenchError(Exception):
    """领域与用例错误。details 只带安全的定位与恢复信息，不带密钥或正文全文。"""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"WorkbenchError({self.code}, {self.message!r}, {self.details!r})"


# ---------------------------------------------------------------- 枚举


class MessageRole(StrEnum):
    AUTHOR = "author"
    MODEL = "model"
    SYSTEM = "system"


class Authority(StrEnum):
    """信息权威：来源决定 AI 的修改权限（需求第 4 节）。"""

    AI_SUGGESTION = "ai_suggestion"
    AUTHOR_IDEA = "author_idea"
    AUTHOR_DECISION = "author_decision"


class Lifecycle(StrEnum):
    """想法生命周期，与权威分开存储。"""

    DRAFT = "draft"
    CANDIDATE = "candidate"
    DECIDED = "decided"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class VersionOrigin(StrEnum):
    AUTHOR = "author"
    GENERATED = "generated"
    EXTERNAL = "external"
    RESTORE = "restore"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TaskKind(StrEnum):
    DISCUSS = "discuss"
    GENERATE = "generate"
    INSPECT = "inspect"
    DIAGNOSE = "diagnose"
    RECOMPUTE = "recompute"


class WriteMode(StrEnum):
    FORMAL = "formal"
    EXPLORATORY = "exploratory"


class FactKind(StrEnum):
    WORLD = "world"
    CHARACTER_BELIEF = "character_belief"
    READER_INFORMATION = "reader_information"
    NARRATION = "narration"
    UNCERTAIN = "uncertain"


class IssueKind(StrEnum):
    NEW_PERSISTENT_SETTING = "new_persistent_setting"
    DECISION_CONFLICT = "decision_conflict"
    CANON_CONFLICT = "canon_conflict"
    PLAN_DEVIATION = "plan_deviation"
    RUMOR_OR_BELIEF = "rumor_or_belief"
    SUSPECTED_ERROR = "suspected_error"


class ResolutionAction(StrEnum):
    AUTHORIZE_SETTING = "authorize_setting"
    AUTHORIZE_DECISION_CHANGE = "authorize_decision_change"
    KEEP_AS_NARRATIVE = "keep_as_narrative"
    CORRECT_EXTRACTION = "correct_extraction"
    ACCEPT_DEVIATION = "accept_deviation"
    KEEP_OPEN = "keep_open"


class SnapshotStatus(StrEnum):
    CANDIDATE = "candidate"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"


class SyncStatus(StrEnum):
    IN_SYNC = "in_sync"
    IMPORTED = "imported"
    CONFLICT = "conflict"
    PENDING = "pending"


# ---------------------------------------------------------------- 共享记录


@dataclass(frozen=True, slots=True)
class Target:
    """准确定位一个正文版本。"""

    novel_id: str
    chapter_id: str
    version_id: str


@dataclass(frozen=True, slots=True)
class Expected:
    """调用前观察到的基准；NULL 也是需要比较的值。

    current_draft_id 用于编辑与恢复的基准比较；其余用于采用与重算。
    """

    adopted_id: str | None = None
    plan_id: str | None = None
    previous_snapshot_id: str | None = None
    workcopy_hash: str | None = None
    current_draft_id: str | None = None


@dataclass(frozen=True, slots=True)
class IssueResolution:
    issue_id: str
    action: ResolutionAction
    explanation: str = ""
    fact_correction: FactCorrection | None = None


@dataclass(frozen=True, slots=True)
class FactCorrection:
    """作者对一条候选提取的纠正；证据仍指向原正文版本。"""

    kind: FactKind
    proposition: str
    evidence_start: int
    evidence_end: int
    quote: str
    subject: str | None = None
    story_time: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorAction:
    """只有明确的 UI 操作能构造正式动作。"""

    request_id: str
    source_message_id: str
    target: Target
    expected: Expected
    check_id: str | None = None
    read_declared: bool = False
    issue_resolutions: tuple[IssueResolution, ...] = ()
    accepted_plot: str | None = None
    accepted_ending: str | None = None


@dataclass(frozen=True, slots=True)
class DelegationScope:
    """有范围的决策委托，本期只允许本次请求本章。"""

    chapter_id: str
    request_message_id: str
    may_choose_plot: bool = False


@dataclass(frozen=True, slots=True)
class TaskRequest:
    task: TaskKind
    basis_id: str
    source_message_id: str
    template_version: str
    input_version_id: str | None = None
    delegation_scope: DelegationScope | None = None
    missing_inputs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    output_limit: int = 0


@dataclass(frozen=True, slots=True)
class DraftResult:
    version_id: str
    run_id: str
    based_on_old_inputs: bool
    check_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    adoption_id: str
    version_id: str
    state_status: SnapshotStatus
    already_adopted: bool


@dataclass(frozen=True, slots=True)
class SaveResult:
    """保存正文的结果：新版本一定存在；是否成为当前编辑稿取决于基准比较。"""

    version_id: str
    became_current: bool
    conflicting_current_id: str | None = None


@dataclass(frozen=True, slots=True)
class VersionRecord:
    id: str
    novel_id: str
    chapter_id: str
    body: str
    content_hash: str
    origin: VersionOrigin
    created_at: str
    parent_id: str | None = None
    restored_from_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanRecord:
    id: str
    novel_id: str
    chapter_id: str
    revision: int
    plot: str | None
    ending: str | None
    source_message_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ChapterRecord:
    id: str
    novel_id: str
    number: int
    current_plan_id: str | None = None
    current_draft_id: str | None = None
    adopted_id: str | None = None
    current_snapshot_id: str | None = None
    needs_review: bool = False


@dataclass(frozen=True, slots=True)
class NovelRecord:
    id: str
    title: str
    design_revision: int


@dataclass(frozen=True, slots=True)
class WorkcopyRecord:
    id: str
    novel_id: str
    chapter_id: str
    relative_path: str
    base_version_id: str | None
    base_hash: str | None
    observed_version_id: str | None
    sync_status: SyncStatus


class ReconcileOutcome(StrEnum):
    NO_WORKCOPY = "no_workcopy"
    IN_SYNC = "in_sync"
    IMPORTED = "imported"
    CONFLICT = "conflict"
    FILE_MISSING = "file_missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """一次文件对账的结果；imported_version_id 只在导入了新稿时有值。"""

    outcome: ReconcileOutcome
    relative_path: str | None = None
    imported_version_id: str | None = None
    conflicting_current_id: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryNotice:
    """打开项目时向作者报告的恢复提示。"""

    interrupted_run_ids: tuple[str, ...] = ()
    pending_workcopies: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------ 检查材料输入（ai 解析后交 database 保存）


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    """来源二选一：本次检查正文（version_id）或同一 basis 的来源（basis_source_id）。"""

    quote: str
    version_id: str | None = None
    basis_source_id: str | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True, slots=True)
class IssueInput:
    kind: IssueKind
    explanation: str
    evidence: tuple[EvidenceInput, ...]
    proposed_change: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateFactInput:
    kind: FactKind
    proposition: str
    evidence_start: int
    evidence_end: int
    quote: str
    subject: str | None = None
    story_time: str | None = None


@dataclass(frozen=True, slots=True)
class CheckInput:
    """一次通过结构校验的完整检查结果；空列表也必须显式给出。"""

    coverage_json: str
    actual_plot: str
    actual_ending: str
    issues: tuple[IssueInput, ...]
    candidate_facts: tuple[CandidateFactInput, ...]


@dataclass(frozen=True, slots=True)
class IssueRecord:
    id: str
    check_id: str
    kind: IssueKind
    explanation: str
    proposed_change: str | None


@dataclass(frozen=True, slots=True)
class CheckRecord:
    id: str
    novel_id: str
    chapter_id: str
    version_id: str
    basis_id: str
    run_id: str
    actual_plot: str
    actual_ending: str
    issues: tuple[IssueRecord, ...]


@dataclass(frozen=True, slots=True)
class BasisRecord:
    id: str
    novel_id: str
    chapter_id: str
    plan_id: str | None
    previous_snapshot_id: str | None
    design_revision: int
    purpose: str


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    id: str
    novel_id: str
    chapter_id: str
    adoption_id: str
    status: SnapshotStatus
    previous_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdoptionRecord:
    id: str
    novel_id: str
    chapter_id: str
    version_id: str
    review_id: str
    request_id: str
    previous_adoption_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdoptionContext:
    """采用校验所需的当前记录，由 database 在事务内读取后交给 rules。"""

    novel: NovelRecord
    chapter: ChapterRecord
    check: CheckRecord
    basis: BasisRecord
    previous_chapter_snapshot: SnapshotRecord | None
    previous_chapter_exists: bool
    existing_adoption_for_request: AdoptionRecord | None
    current_adoption: AdoptionRecord | None


# ------------------------------------------------ 快照计算（R01）


@dataclass(frozen=True, slots=True)
class CandidateFactRecord:
    id: str
    check_id: str
    kind: FactKind
    proposition: str
    evidence_start: int
    evidence_end: int
    quote: str
    subject: str | None = None
    story_time: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotFactRef:
    """二选一：原始候选或作者纠正后的有效修订。"""

    candidate_id: str | None = None
    fact_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotChain:
    """提交快照前必须逐项比较的来源链；任一变化即 stale。"""

    adoption_id: str
    previous_snapshot_id: str | None
    head_revisions: tuple[tuple[str, str | None], ...]  # (candidate_id, current_revision_id)


@dataclass(frozen=True, slots=True)
class SnapshotDraft:
    facts: tuple[SnapshotFactRef, ...]
    open_questions: tuple[str, ...]
