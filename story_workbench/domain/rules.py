"""纯校验与计算；不读文件、数据库、网络、时钟。首次使用时增加。"""

from __future__ import annotations

import hashlib

from story_workbench.domain.records import ErrorCode, WorkbenchError, WriteMode


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
