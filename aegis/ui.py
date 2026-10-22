"""文本面板渲染（ARCHITECTURE.md §10）。

约束（P4 + ARCH §10 + P5 D-2 硬约束）：

- **零依赖**：仅用 :py:mod:`dataclass` / :py:mod:`datetime` / :py:mod:`io` /
  字符串方法。不引入 Rich / Textual / 任何颜色库 / 前端库。
- **定宽文本列**（``str.format`` 即可）：列宽固定，不依赖终端宽度。
- **本地时区显示**：内部 ``resets_at`` / ``updated_at`` 一律 UTC ISO；UI
  转 ``astimezone()`` 后显示，头尾标注 ``(+HH:MM)``。
- **禁止凭据泄漏**：面板、底部小字、行尾注释中**绝对不**出现
  ``sk-...`` / ``Bearer ...`` / 任何 API key 子串（NO_SECRET_LEAK）。
- **失败语义**（ARCH §8 + §10）：
    * ``reliability='stale'`` → 行尾追加 ``*STALE(抓取失败: <error.type>)``。
    * ``status='error'`` → 数据列显示 ``—``。
- **extras 底注**：Codex 的 ``extras``（Credits/Reset）以一行小字带过。
- **P5 D-2：UPDATED 列对未提供数据时刻的 provider**（MiniMax / DeepSeek）：
    内部 ``updated_at`` 仍为 ``None``（**源真相不变**，不伪造）。UI 渲染
    时显示**本地抓取时刻**（取自当前 ``now`` 参数，即本轮抓取完成时刻），
    加 ``~`` 前缀以示区分（例 ``~15:58``），并自动输出 ``~ = 本地抓取
    时间（源未提供数据时刻）`` 底注。
    其它 provider（Codex / z.ai 上游返回 ``updated_at``）仍显示源时刻（无
    ``~``）。本调整**绝不**修改 snapshot / 缓存条目里的 ``updated_at`` 字
    段（反伪装硬约束 ARCH §8）。

ANSI 清屏（``--watch`` 模式）：``\\x1b[2J\\x1b[H``（Windows Terminal 原生支持）。
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from aegis.model import ExtraWindow, Snapshot, Window


# 列宽（参考 ARCH §10 布局，按"4 行 + 头尾"最小可用宽度）
_COL_PROVIDER = 10
_COL_PCT = 17
_COL_RESET = 17
_COL_BALANCE = 12
_COL_STATUS = 20   # 容纳常见 error.type（missing_credential / network_error / SERVICE_UNAVAILABLE）
_COL_SOURCE = 16
_COL_UPDATED = 10


# ---------------------------------------------------------------------------
# ANSI 控制序列（--watch 模式原地重画）
# ---------------------------------------------------------------------------

_ANSI_CLEAR = "\x1b[2J\x1b[H"  # 清屏 + 光标回原点


# ---------------------------------------------------------------------------
# 凭据泄漏防御（即使 snapshot 错误地含 sk- 片段，UI 也必须脱敏）
# ---------------------------------------------------------------------------

_REDACT_SK = re.compile(r"sk-[A-Za-z0-9_\-]+")
_REDACT_BEARER = re.compile(r"(?i)\bBearer\b[ \t]+\S+")


def _scrub(text: str) -> str:
    """把字符串里偶然出现的 ``sk-...`` / ``Bearer xxx`` 替换为占位。"""
    if not text:
        return text
    text = _REDACT_SK.sub("<redacted>", text)
    text = _REDACT_BEARER.sub("Bearer <redacted>", text)
    return text


# ---------------------------------------------------------------------------
# 时间 / 百分比格式化
# ---------------------------------------------------------------------------

def _to_local(dt: datetime) -> datetime:
    """aware datetime → 本地时区。"""
    return dt.astimezone()


def _local_tz_label(dt: datetime) -> str:
    """``+08:00`` 形式的时区标签。"""
    offset = dt.utcoffset()
    if offset is None:
        return "+00:00"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh = total // 3600
    mm = (total % 3600) // 60
    return f"{sign}{hh:02d}:{mm:02d}"


def _format_now_header(now_utc: datetime) -> str:
    """面板头部时间戳，例如 ``2026-09-11 14:30:05 (+08:00)``。"""
    local = _to_local(now_utc)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} ({_local_tz_label(local)})"


def _format_local_short(iso: Optional[str]) -> str:
    """UTC ISO 字符串 → 本地 ``MM-DD HH:MM`` 形式；无值返回 ``—``。"""
    if not iso:
        return "—"
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        return "—"
    local = dt.astimezone()
    return local.strftime("%m-%d %H:%M")


def _format_local_clock(iso: Optional[str]) -> str:
    """UTC ISO 字符串 → 本地 ``HH:MM:SS`` 形式；无值返回 ``—``。"""
    if not iso:
        return "—"
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        return "—"
    return dt.astimezone().strftime("%H:%M:%S")


def _format_updated(
    source_iso: Optional[str],
    *,
    now_utc: datetime,
) -> str:
    """UPDATED 列渲染（P5 D-2）。

    - ``source_iso`` 非 ``None`` → 显示**源时刻** ``HH:MM:SS``，无 ``~``
      前缀（Codex / z.ai 等上游带 ``updated_at`` 字段的 provider）。
    - ``source_iso`` 为 ``None`` → 显示**本地抓取时刻** ``~HH:MM``（取自
      ``now_utc``，即本轮抓取完成时刻），加 ``~`` 前缀以示区分（MiniMax /
      DeepSeek 等上游未返回数据时刻的 provider）。

    本函数**绝不**修改 snapshot / 缓存的 ``updated_at`` 字段（反伪装硬约束
    ARCH §8）；``source_iso`` 仅作为只读输入。
    """
    if source_iso is not None:
        return _format_local_clock(source_iso)
    # ``now_utc`` 来自 ``render_panel`` 的 ``now`` 参数，即抓取完成时刻
    local_now = _to_local(now_utc)
    return "~" + local_now.strftime("%H:%M")


def _format_pct(used: Optional[float], remaining: Optional[float]) -> str:
    """``55.0% / 45.0%`` 形式。"""
    if used is None or remaining is None:
        return "—     / —"
    return f"{used:.1f}% / {remaining:.1f}%"


def _format_balance(balance) -> str:
    """``74.16 CNY`` 或 ``—``。"""
    if balance is None:
        return "—"
    return f"{balance.value:.2f} {balance.currency}"


def _format_status(snap: Snapshot) -> str:
    """STATUS 列：``ok`` / ``error`` / 错误类型缩写（截到列宽）。"""
    if snap.status == "ok":
        return "ok"
    if snap.error is not None:
        return _scrub(snap.error.type)[:_COL_STATUS]
    return "error"[:_COL_STATUS]


# ---------------------------------------------------------------------------
# 单行渲染
# ---------------------------------------------------------------------------

def _render_row(
    snap: Snapshot,
    *,
    now_utc: datetime,
    stale_tail: str = "",
) -> str:
    """单 provider 一行（按 ARCH §10 列宽）。

    :param now_utc: 当前轮抓取完成时刻，用于 UPDATED 列在 ``updated_at`` 为
        ``None`` 时显示本地抓取时间（P5 D-2）。不影响其它列。
    """
    provider = snap.provider[:_COL_PROVIDER]

    # 5h 窗口
    if snap.five_hour is not None:
        five_pct = _format_pct(snap.five_hour.used_percent,
                               snap.five_hour.remaining_percent)
        five_reset = _format_local_short(snap.five_hour.resets_at)
    else:
        five_pct = "—     / —"
        five_reset = "—"

    # 周窗口
    if snap.weekly is not None:
        wk_pct = _format_pct(snap.weekly.used_percent,
                             snap.weekly.remaining_percent)
        wk_reset = _format_local_short(snap.weekly.resets_at)
    else:
        wk_pct = "—     / —"
        wk_reset = "—"

    balance_str = _format_balance(snap.balance)
    status_str = _format_status(snap)
    source_str = (snap.source or "")[:_COL_SOURCE]
    updated_str = _format_updated(snap.updated_at, now_utc=now_utc)

    row = (
        f"{provider:<{_COL_PROVIDER}}"
        f"{five_pct:<{_COL_PCT}}"
        f"{five_reset:<{_COL_RESET}}"
        f"{wk_pct:<{_COL_PCT}}"
        f"{wk_reset:<{_COL_RESET}}"
        f"{balance_str:<{_COL_BALANCE}}"
        f"{status_str:<{_COL_STATUS}}"
        f"{source_str:<{_COL_SOURCE}}"
        f"{updated_str:<{_COL_UPDATED}}"
    )
    if stale_tail:
        row += " " + _scrub(stale_tail)
    return row


# ---------------------------------------------------------------------------
# 头 / 底注
# ---------------------------------------------------------------------------

_TOTAL_WIDTH = (
    _COL_PROVIDER + _COL_PCT + _COL_RESET + _COL_PCT + _COL_RESET
    + _COL_BALANCE + _COL_STATUS + _COL_SOURCE + _COL_UPDATED
)


def _header_lines(snapshots: List[Snapshot], now_utc: datetime) -> List[str]:
    """标题 + 表头两行。"""
    title = "Aegis Token Monitor"
    time_str = _format_now_header(now_utc)
    # 左标题 + 右时间戳，时间戳右对齐到总宽度
    pad = max(1, _TOTAL_WIDTH - len(title) - len(time_str))
    head = f"{title}{' ' * pad}{time_str}"
    # 表头：5h 用/剩 | 5h RESET | WK 用/剩 | WK RESET | BALANCE | STATUS | SOURCE | UPDATED
    header_cols = (
        f"{'PROVIDER':<{_COL_PROVIDER}}"
        f"{'5h used/rem':<{_COL_PCT}}"
        f"{'5h RESET':<{_COL_RESET}}"
        f"{'WK used/rem':<{_COL_PCT}}"
        f"{'WK RESET':<{_COL_RESET}}"
        f"{'BALANCE':<{_COL_BALANCE}}"
        f"{'STATUS':<{_COL_STATUS}}"
        f"{'SOURCE':<{_COL_SOURCE}}"
        f"{'UPDATED':<{_COL_UPDATED}}"
    )
    return [head, header_cols]


def _extras_footer(snapshots: Iterable[Snapshot]) -> List[str]:
    """Codex extras 底注（一行小字带过，如 ``reset-credits: 1 available``）。

    仅对 ``id`` ∈ {``reset-credits``, ``codex-gpt-reserve``}（ARCH §1 中确认的
    两个常见条目）做一行展示。其它 extras 静默忽略，避免面板噪声。
    """
    lines: List[str] = []
    for snap in snapshots:
        if snap.provider != "codex":
            continue
        if not snap.extras:
            continue
        for ex in snap.extras:
            if not isinstance(ex, ExtraWindow):
                continue
            if ex.id == "reset-credits":
                # 形如 "reset-credits: 1 available"
                desc = ex.reset_description or "available"
                lines.append(f"[codex] {ex.id}: {desc}")
            elif ex.id == "codex-gpt-reserve":
                # 形如 "codex-gpt-reserve: 6d 23h"
                desc = ex.reset_description or "—"
                lines.append(f"[codex] {ex.id}: {desc}")
    return lines


def _local_time_legend(snapshots: Iterable[Snapshot]) -> List[str]:
    """P5 D-2 底注：``~`` 前缀说明。

    仅当存在 ``updated_at is None`` 的 provider（MiniMax / DeepSeek 等上
    游未返回数据时刻）时才输出脚注，避免在没有任何 ``~`` 的面板上加入无
    意义说明。脚注内容固定为 ``~ = 本地抓取时间（源未提供数据时刻）``。
    """
    needs_legend = any(s.updated_at is None for s in snapshots)
    if not needs_legend:
        return []
    return ["~ = 本地抓取时间（源未提供数据时刻）"]


def _stale_tail(snap: Snapshot) -> str:
    """失败时的行尾注释，例：``*STALE(抓取失败: network_error)``。"""
    if snap.reliability != "stale":
        return ""
    if snap.error is None:
        return "*STALE(抓取失败: unknown)"
    return f"*STALE(抓取失败: {snap.error.type})"


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def render_panel(
    snapshots: List[Snapshot],
    *,
    now: Optional[datetime] = None,
) -> str:
    """渲染文本面板，返回完整字符串（不含 ANSI 清屏序列）。

    :param snapshots: provider 列表（顺序自由；UI 不强制）。
    :param now: 当前 UTC 时刻；``None`` 时取 :py:func:`datetime.now`。
        同时作为"本轮抓取完成时刻"，用于 UPDATED 列 ``~HH:MM`` 显示
        （P5 D-2：上游 ``updated_at`` 为 ``None`` 时显示本地抓取时间）。
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    lines: List[str] = []
    lines.extend(_header_lines(snapshots, now))

    if not snapshots:
        lines.append("(no providers configured)")
    else:
        for snap in snapshots:
            lines.append(
                _render_row(snap, now_utc=now, stale_tail=_stale_tail(snap))
            )

    # Codex extras 底注（Credits/Reset 一行小字）
    for line in _extras_footer(snapshots):
        lines.append(line)

    # P5 D-2 底注：``~`` 前缀说明（仅在有需要时出现）
    for line in _local_time_legend(snapshots):
        lines.append(line)

    return "\n".join(lines)


def render_panel_watch(
    snapshots: List[Snapshot],
    *,
    now: Optional[datetime] = None,
) -> str:
    """``--watch`` 模式：ANSI 清屏 + 完整面板（原地重画）。"""
    panel = render_panel(snapshots, now=now)
    return _ANSI_CLEAR + panel


def render_to_buffer(
    snapshots: List[Snapshot],
    *,
    now: Optional[datetime] = None,
    watch: bool = False,
    out: Optional[io.IOBase] = None,
) -> str:
    """渲染并可选地写入缓冲；返回渲染内容字符串（便于测试断言）。"""
    if watch:
        rendered = render_panel_watch(snapshots, now=now)
    else:
        rendered = render_panel(snapshots, now=now)
    if out is not None:
        out.write(rendered)
        if not rendered.endswith("\n"):
            out.write("\n")
    return rendered


__all__ = [
    "render_panel",
    "render_panel_watch",
    "render_to_buffer",
]
