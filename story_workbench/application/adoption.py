"""审阅采用、事实纠正、复核与状态重算的用例。"""

from __future__ import annotations

from story_workbench.domain import rules
from story_workbench.domain.records import (
    AdoptionResult,
    AuthorAction,
    ErrorCode,
    FactCorrection,
    ForeshadowEventRecord,
    ForeshadowStage,
    ImpactRecord,
    PublicationRecord,
    ReconcileOutcome,
    RemediationPlanRecord,
    SnapshotChain,
    SnapshotStatus,
    WorkbenchError,
)
from story_workbench.infrastructure import database

from . import projects
from .projects import Project


def adopt(project: Project, action: AuthorAction) -> AdoptionResult:
    """作者已读及具体授权 → 采用。一次动作内保存 review/resolution/adoption；短事务。

    采用前再对账文件：发现未展示的外部稿则停止并报告，不采用。
    重算失败不撤销采用（重算属于 R01，此处只返回状态待更新）。
    """
    rules.require_same_scope(project.novel_id, action.target.novel_id, "采用目标")
    reconcile = projects.reconcile_workcopy(project, action.target.chapter_id)
    if reconcile.outcome in (ReconcileOutcome.IMPORTED, ReconcileOutcome.CONFLICT):
        raise WorkbenchError(
            ErrorCode.WORKCOPY_CONFLICT,
            "发现尚未展示的外部修改稿，已保存为新稿；请查看后决定采用哪一稿。",
            {
                "imported_version_id": reconcile.imported_version_id,
                "target_version_id": action.target.version_id,
            },
        )
    # 预检：用同一套规则先给作者明确原因；事务内会再次复核。
    rules.validate_adoption(action, database.load_adoption_context(project.conn, action))
    result = database.commit_adoption(project.conn, action)
    if result.already_adopted:
        return result
    try:
        status = recompute_state(project, action.target.chapter_id)
    except WorkbenchError:
        status = SnapshotStatus.FAILED
    return AdoptionResult(
        adoption_id=result.adoption_id,
        version_id=result.version_id,
        state_status=status,
        already_adopted=False,
    )


def recompute_state(project: Project, chapter_id: str) -> SnapshotStatus:
    """读取当前采用的已审阅候选与纠正，纯计算后请求提交匹配依据的快照。

    不调用模型、不新增采用；失败保留“状态待更新”；依据改变只存历史结果。
    """
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    if chapter.adopted_id is None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "本章尚未采用，没有可更新的状态。")
    snapshot = database.latest_snapshot_for_adoption(
        project.conn, project.novel_id, chapter.adopted_id
    )
    if snapshot is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "找不到本次采用的状态记录。")
    if snapshot.status is SnapshotStatus.READY and chapter.current_snapshot_id == snapshot.id:
        return SnapshotStatus.READY
    if snapshot.status in (SnapshotStatus.FAILED, SnapshotStatus.STALE):
        snapshot_id = database.commit_snapshot_retry(project.conn, project.novel_id, snapshot.id)
        snapshot = database.get_snapshot(project.conn, project.novel_id, snapshot_id)
    adoption_rec = database.get_adoption(project.conn, project.novel_id, chapter.adopted_id)
    check = database.get_check(
        project.conn,
        project.novel_id,
        database.get_snapshot_check_id(project.conn, project.novel_id, snapshot.id),
    )
    if check.version_id != adoption_rec.version_id:
        raise WorkbenchError(ErrorCode.STALE_BASIS, "状态记录与采用版本不一致。")
    candidates = database.list_candidate_facts(project.conn, project.novel_id, check.id)
    heads = database.get_fact_heads(project.conn, project.novel_id, tuple(c.id for c in candidates))
    previous_facts: tuple = ()
    if snapshot.previous_snapshot_id is not None:
        previous_facts = database.list_snapshot_facts(
            project.conn, project.novel_id, snapshot.previous_snapshot_id
        )
    try:
        draft = rules.compute_snapshot(candidates, heads, previous_facts)
    except Exception as exc:  # 计算失败：保留采用，状态待更新
        database.commit_snapshot_failed(
            project.conn, project.novel_id, snapshot.id, "COMPUTE_FAILED"
        )
        raise WorkbenchError(
            ErrorCode.STORAGE_FAILED,
            "故事状态更新失败，正文已采用，可稍后重试。",
            {"error": repr(exc)},
        ) from exc
    chain = SnapshotChain(
        adoption_id=adoption_rec.id,
        previous_snapshot_id=snapshot.previous_snapshot_id,
        head_revisions=tuple((c.id, heads.get(c.id)) for c in candidates),
    )
    try:
        return database.commit_snapshot(project.conn, project.novel_id, snapshot.id, draft, chain)
    except WorkbenchError as exc:
        if exc.code is ErrorCode.STORAGE_FAILED:
            database.commit_snapshot_failed(
                project.conn, project.novel_id, snapshot.id, exc.code.value
            )
        raise


def correct_fact(
    project: Project,
    candidate_id: str,
    correction: FactCorrection,
    author_message_id: str,
    expected_head_revision_id: str | None,
) -> str:
    """仅修正解释；正文变化应走 save_draft。依赖该提取的状态过时，需重算。"""
    return database.commit_fact_correction(
        project.conn,
        project.novel_id,
        candidate_id,
        correction,
        author_message_id,
        expected_head_revision_id,
    )


def revalidate_chapter(project: Project, action: AuthorAction) -> SnapshotStatus:
    """前章变化而本章正文未变：用已完成的新检查与作者动作保存新审阅并重算状态。

    不发起检查、不新建正文、不重复采用。新检查由 generation.check_draft 先完成。
    """
    rules.require_same_scope(project.novel_id, action.target.novel_id, "复核目标")
    reconcile = projects.reconcile_workcopy(project, action.target.chapter_id)
    if reconcile.outcome in (ReconcileOutcome.IMPORTED, ReconcileOutcome.CONFLICT):
        raise WorkbenchError(
            ErrorCode.WORKCOPY_CONFLICT,
            "发现尚未展示的外部修改稿，已保存为新稿；复核前请先查看。",
            {"imported_version_id": reconcile.imported_version_id},
        )
    database.commit_revalidation(project.conn, action)
    try:
        return recompute_state(project, action.target.chapter_id)
    except WorkbenchError:
        return SnapshotStatus.FAILED


def pending_impacts(project: Project, chapter_id: str) -> tuple[tuple[str, str], ...]:
    return database.list_pending_impacts(project.conn, project.novel_id, chapter_id)


def list_impacts(project: Project, chapter_id: str | None = None) -> tuple[ImpactRecord, ...]:
    return database.list_impact_records(project.conn, project.novel_id, chapter_id)


def mark_published(
    project: Project,
    action: AuthorAction,
    note: str = "",
) -> PublicationRecord:
    """作者把准确的当前已采用版本标为已发布；本地后续采用不移动发布记录。"""
    rules.require_same_scope(project.novel_id, action.target.novel_id, "发布目标")
    if not action.read_declared:
        raise WorkbenchError(ErrorCode.REVIEW_REQUIRED, "发布前需要确认这是读者实际看到的版本。")
    chapter = database.get_chapter(project.conn, project.novel_id, action.target.chapter_id)
    if chapter.adopted_id is None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "本章尚未采用，不能标记发布。")
    return database.commit_publication(
        project.conn,
        project.novel_id,
        action.target.chapter_id,
        action.target.version_id,
        action.source_message_id,
        note,
        request_id=action.request_id,
    )


def list_publications(
    project: Project, chapter_id: str | None = None
) -> tuple[PublicationRecord, ...]:
    return database.list_publications(project.conn, project.novel_id, chapter_id)


def propose_remediation(
    project: Project, publication_id: str, source_message_id: str, body: str
) -> RemediationPlanRecord:
    return database.commit_remediation_plan(
        project.conn, project.novel_id, publication_id, source_message_id, body
    )


def resolve_impact(
    project: Project,
    impact_id: str,
    classification: str,
    author_message_id: str,
    *,
    analysis_id: str | None = None,
    note: str = "",
) -> str:
    return database.commit_impact_resolution(
        project.conn,
        project.novel_id,
        impact_id,
        classification,
        author_message_id,
        analysis_id=analysis_id,
        note=note,
    )


def record_foreshadow(
    project: Project,
    *,
    stage: ForeshadowStage,
    chapter_id: str,
    body: str,
    quote: str,
    adoption_id: str,
    source_message_id: str,
    logical_id: str | None = None,
    version_id: str | None = None,
) -> ForeshadowEventRecord:
    return database.commit_foreshadow_event(
        project.conn,
        project.novel_id,
        logical_id=logical_id or database.new_id(),
        stage=stage,
        chapter_id=chapter_id,
        adoption_id=adoption_id,
        version_id=version_id,
        source_message_id=source_message_id,
        body=body,
        quote=quote,
    )


def list_foreshadow(project: Project) -> tuple[ForeshadowEventRecord, ...]:
    return database.list_foreshadow_events(project.conn, project.novel_id)
