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


def validate_evidence_span(body: str, start: int, end: int, quote: str) -> None:
    """证据区间必须按 Python 字符索引与正文准确匹配：body[start:end] == quote。

    失败抛 INVALID_INPUT，details 只带定位信息，不带正文或引用全文。
    模型返回的载荷由 ai/tasks 转记为 MODEL_FAILED；作者纠正直接使用本错误。
    """
    length = len(body)
    if start < 0 or end < start or end > length:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "证据区间超出正文范围。",
            {"start": start, "end": end, "body_length": length, "reason": "span_out_of_range"},
        )
    if not quote:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "证据引用不能为空。",
            {"start": start, "end": end, "body_length": length, "reason": "empty_quote"},
        )
    if body[start:end] != quote:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "证据引用与正文区间不一致。",
            {
                "start": start,
                "end": end,
                "body_length": length,
                "quote_length": len(quote),
                "reason": "quote_mismatch",
            },
        )
