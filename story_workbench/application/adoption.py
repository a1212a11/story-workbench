"""审阅采用、事实纠正、复核与状态重算的用例。"""

from __future__ import annotations

from story_workbench.domain import rules
from story_workbench.domain.records import (
    AdoptionResult,
    AuthorAction,
    ErrorCode,
    ReconcileOutcome,
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
    return database.commit_adoption(project.conn, action)
