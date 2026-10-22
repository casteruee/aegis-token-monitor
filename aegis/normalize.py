"""纯函数归一化层（ARCHITECTURE.md §7.2 / §7.3）。

本模块是项目里**唯一**允许出现 ``100 - x`` 翻转逻辑的地方（reader 层
被禁止）。所有函数无副作用、不联网、不读写磁盘，方便离线测试。

时间格式（ARCHITECTURE §7.3 + ROUTE_REVIEW M-4）：
- epoch 毫秒 → UTC ISO 8601，秒级无小数点、非零毫秒带三位小数。
- ISO 字符串 → aware datetime；naive 必须报错。
- 解析前显式 ``s.replace("Z", "+00:00")``，兼容 Python 3.9/3.10/3.11/3.12。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Tuple


class NormalizationError(Exception):
    """字段值越界 / 类型错误 / naive 时间等归一化语义错误。"""


class UnexpectedShape(Exception):
    """上游数据结构漂移（例如 MiniMax 窗口跨度不对、缺关键字段）。"""


# ---------------------------------------------------------------------------
# 百分比语义转换
# ---------------------------------------------------------------------------

def from_used(used_pct) -> Tuple[float, float]:
    """源字段是「已用百分比」→ ``(used, remaining)``。

    适用：Codex ``used_percent``、z.ai ``percentage``。

    >>> from_used(95)
    (95.0, 5.0)
    """
    try:
        used = float(used_pct)
    except (TypeError, ValueError):
        raise NormalizationError(
            f"used_percent cannot be coerced to float: {used_pct!r}"
        )
    if not (0.0 <= used <= 100.0):
        raise NormalizationError(
            f"used_percent out of range [0,100]: {used}"
        )
    return used, round(100.0 - used, 2)


def from_remaining(remaining_pct) -> Tuple[float, float]:
    """源字段是「剩余百分比」→ ``(used, remaining)``。

    适用：MiniMax ``current_*_remaining_percent``。源语义必须翻转。

    >>> from_remaining(93)
    (7.0, 93.0)
    """
    try:
        remaining = float(remaining_pct)
    except (TypeError, ValueError):
        raise NormalizationError(
            f"remaining_percent cannot be coerced to float: {remaining_pct!r}"
        )
    if not (0.0 <= remaining <= 100.0):
        raise NormalizationError(
            f"remaining_percent out of range [0,100]: {remaining}"
        )
    return round(100.0 - remaining, 2), remaining


# ---------------------------------------------------------------------------
# 时间归一化
# ---------------------------------------------------------------------------

def epoch_ms_to_utc_iso(ms: int) -> str:
    """epoch 毫秒 → UTC ISO 8601 字符串。

    无小数时输出形如 ``2026-09-11T10:00:00Z``；
    含毫秒时输出 ``...T07:39:49.135Z``（三位精度）。
    """
    # bool 是 int 的子类，需排除
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise NormalizationError(
            f"epoch_ms must be int, got {type(ms).__name__}"
        )
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if dt.microsecond:
        ms_part = dt.microsecond // 1000  # 三位毫秒
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms_part:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_utc(s: str) -> datetime:
    """ISO 8601 字符串 → aware datetime（UTC 偏移可任意，但必须带 tz）。

    实现细节（ROUTE_REVIEW.md M-4）：先把 ``Z`` 替换为 ``+00:00`` 再调用
    :py:meth:`datetime.fromisoformat`，使 Python 3.9+ 均能解析；naive 字
    符串（无 tz）必须报错，禁止默认本地时区。
    """
    if not isinstance(s, str):
        raise NormalizationError(
            f"iso_to_utc expects str, got {type(s).__name__}"
        )
    if not s.strip():
        raise NormalizationError("iso_to_utc: empty string")
    normalized = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise NormalizationError(
            f"failed to parse ISO datetime {s!r}: {e}"
        )
    if dt.tzinfo is None:
        raise NormalizationError(
            f"naive ISO datetime not allowed (no tz info): {s!r}"
        )
    return dt


# ---------------------------------------------------------------------------
# MiniMax 窗口跨度断言（ARCHITECTURE.md §3 / §7.3）
# ---------------------------------------------------------------------------

MINIMAX_INTERVAL_MS = 18_000_000    # 5h
MINIMAX_WEEKLY_MS = 604_800_000     # 7d


def _coerce_ms(value, name: str) -> int:
    """把可能是 int 或整型 float 的毫秒统一为 int，非整数报错。"""
    if isinstance(value, bool):
        raise UnexpectedShape(f"{name}: bool is not a valid ms value")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise UnexpectedShape(
                f"{name}: ms value must be integer, got {value!r}"
            )
        return int(value)
    raise UnexpectedShape(
        f"{name}: ms value must be int or float, got {type(value).__name__}"
    )


def assert_window_span(start_ms, end_ms, expected_ms: int, name: str) -> None:
    """断言 ``end - start == expected`` 毫秒，否则抛 :class:`UnexpectedShape`。"""
    s = _coerce_ms(start_ms, f"{name}.start")
    e = _coerce_ms(end_ms, f"{name}.end")
    delta = e - s
    if delta != expected_ms:
        raise UnexpectedShape(
            f"{name} window span mismatch: expected {expected_ms}ms, got {delta}ms"
        )


def assert_minimax_interval_span(start_ms, end_ms) -> None:
    """MiniMax 5h 窗口断言：end - start == 18,000,000 ms。"""
    assert_window_span(start_ms, end_ms, MINIMAX_INTERVAL_MS, "minimax_interval")


def assert_minimax_weekly_span(start_ms, end_ms) -> None:
    """MiniMax 周窗口断言：weekly_end - weekly_start == 604,800,000 ms。"""
    assert_window_span(start_ms, end_ms, MINIMAX_WEEKLY_MS, "minimax_weekly")
