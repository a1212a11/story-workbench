"""纯校验与计算；不读文件、数据库、网络、时钟。首次使用时增加。"""

from __future__ import annotations

import hashlib

from story_workbench.domain.records import (
    AdoptionContext,
    AuthorAction,
    ErrorCode,
    IssueKind,
    ResolutionAction,
    SnapshotStatus,
    WorkbenchError,
    WriteMode,
)


def normalize_newlines(text: str) -> str:
    """只在明确的保存入口调用；检查必须引用保存后的确切正文。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_plan_inputs(mode: WriteMode, plot: str | None, ending: str | None) -> tuple[str, ...]:
    """返回缺失项。正式写作要求两项都有；试写允许缺失但要记录缺口。"""
    missing: list[str] = []
    if not (plot and plot.strip()):
        missing.append("plot")
    if not (ending and ending.strip()):
        missing.append("ending")
    if mode is WriteMode.FORMAL and missing:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "正式写作需要本章剧情和结尾状态。",
            {"missing": missing},
        )
    return tuple(missing)


def require_same_scope(expected_novel_id: str, actual_novel_id: str, what: str) -> None:
    if expected_novel_id != actual_novel_id:
        raise WorkbenchError(
            ErrorCode.WRONG_SCOPE,
            f"{what}不属于当前作品。",
            {"expected_novel_id": expected_novel_id, "actual_novel_id": actual_novel_id},
        )


# ---------------------------------------------------------------- 采用条件


def validate_span(body: str, start: int, end: int, quote: str) -> bool:
    """证据区间按 Python 字符索引 [start, end)，必须与原文逐字一致。"""
    return 0 <= start < end <= len(body) and body[start:end] == quote


_AUTHORIZATION_REQUIRED_KINDS = {
    IssueKind.NEW_PERSISTENT_SETTING: {ResolutionAction.AUTHORIZE_SETTING},
    IssueKind.DECISION_CONFLICT: {ResolutionAction.AUTHORIZE_DECISION_CHANGE},
}


def validate_adoption(action: AuthorAction, current: AdoptionContext) -> None:
    """采用条件。预检与事务内使用同一实现；通过则返回，否则抛领域错误。

    不把模型置信度当授权；普通已读不替代具体问题授权；依据过时一律拒绝。
    """
    target = action.target
    require_same_scope(current.novel.id, target.novel_id, "采用目标")
    check = current.check
    if check.chapter_id != target.chapter_id or check.version_id != target.version_id:
        raise WorkbenchError(
            ErrorCode.REVIEW_REQUIRED,
            "这份检查不属于要采用的稿件，请对当前稿件重新检查。",
            {"check_version_id": check.version_id, "target_version_id": target.version_id},
        )
    if not action.read_declared:
        raise WorkbenchError(ErrorCode.REVIEW_REQUIRED, "采用前需要明确声明已阅读全文。")

    chapter = current.chapter
    stale: dict[str, object] = {}
    if action.expected.adopted_id != chapter.adopted_id:
        stale["adopted_id"] = chapter.adopted_id
    if action.expected.plan_id != chapter.current_plan_id:
        stale["plan_id"] = chapter.current_plan_id
    basis = current.basis
    if basis.plan_id != chapter.current_plan_id:
        stale["basis_plan_id"] = basis.plan_id
    if basis.design_revision != current.novel.design_revision:
        stale["design_revision"] = current.novel.design_revision
    previous = current.previous_chapter_snapshot
    if current.previous_chapter_exists:
        if previous is None or previous.status is not SnapshotStatus.READY:
            raise WorkbenchError(
                ErrorCode.STALE_BASIS,
                "上一章的故事状态尚未更新完成，暂不能采用本章。",
                {"previous_snapshot_id": None if previous is None else previous.id},
            )
        if basis.previous_snapshot_id != previous.id:
            stale["previous_snapshot_id"] = previous.id
    elif basis.previous_snapshot_id is not None:
        stale["previous_snapshot_id"] = None
    if action.expected.previous_snapshot_id != (None if previous is None else previous.id):
        stale["expected_previous_snapshot_id"] = None if previous is None else previous.id
    if stale:
        raise WorkbenchError(
            ErrorCode.STALE_BASIS,
            "相关依据已经变化，请重新检查并审阅后再采用。",
            stale,
        )

    resolutions = {r.issue_id: r for r in action.issue_resolutions}
    known = {i.id: i for i in check.issues}
    unknown = set(resolutions) - set(known)
    if unknown:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT, "处理了不属于这份检查的问题。", {"issue_ids": sorted(unknown)}
        )
    for issue in check.issues:
        resolution = resolutions.get(issue.id)
        required = _AUTHORIZATION_REQUIRED_KINDS.get(issue.kind)
        if required is not None:
            if resolution is None or resolution.action not in required:
                raise WorkbenchError(
                    ErrorCode.AUTHORIZATION_REQUIRED,
                    "有新增持久设定或与既有决定冲突的内容，需要逐项明确授权或修改稿件。",
                    {"issue_id": issue.id, "kind": issue.kind.value},
                )
        elif issue.kind is IssueKind.CANON_CONFLICT:
            if (
                resolution is None
                or resolution.action is ResolutionAction.KEEP_AS_NARRATIVE
                or not resolution.explanation.strip()
            ):
                raise WorkbenchError(
                    ErrorCode.AUTHORIZATION_REQUIRED,
                    "与既有正文直接冲突的内容不能忽略，需要修改稿件或给出已成文的修正依据。",
                    {"issue_id": issue.id},
                )
        elif issue.kind is IssueKind.PLAN_DEVIATION:
            accepted = (
                resolution is not None and resolution.action is ResolutionAction.ACCEPT_DEVIATION
            )
            if not accepted or not (action.accepted_plot and action.accepted_ending):
                raise WorkbenchError(
                    ErrorCode.REVIEW_REQUIRED,
                    "正文与计划不一致，请明确接受实际剧情与结尾，或修改稿件。",
                    {"issue_id": issue.id},
                )
    if current.basis.purpose == "exploratory" and not (
        action.accepted_plot and action.accepted_ending
    ):
        raise WorkbenchError(
            ErrorCode.REVIEW_REQUIRED, "试写稿采用时需要明确接受探索出的剧情与结尾。"
        )
