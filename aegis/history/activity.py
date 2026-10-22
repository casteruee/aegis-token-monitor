"""七日观测足迹（UI_NEXT_VERSION_DESIGN §7E —— 保守的可比额度变化判定）。

规则（全部满足才算 confirmed_increase）：
- 同一 Provider、**相同非空 resets_at**、相同 window_minutes；
- 两点都是成功 live observation（点从 observations 表来，本身即成功采集）；
- 有效有限数值；间隔 ≤ 2×TTL（默认 TTL 300s → 600s）；
- 同窗口 used% 严格增加。

中断条件（保守漏报优先）：负差值（=重置）、resets_at 缺失或变化、跨度变化、
间隔过大。DeepSeek 无窗口字段 → 永远不产生 confirmed_increase；余额变化
只以文字表达，不算 Token 活动。

输出：给定统计时区（默认 Asia/Shanghai）的最近 7 个本地日，每天
"confirmed_increase" / "observed"（有观测未确认增加）/ "none"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from aegis.history.store import DayMark

DEFAULT_TTL_SECONDS = 300
DEFAULT_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai（固定偏移，避免数据库依赖）


@dataclass(frozen=True)
class FootprintResult:
    provider: str
    days: List[DayMark]

    @property
    def has_confirmed(self) -> bool:
        return any(d.kind == "confirmed_increase" for d in self.days)


def _local_date(iso: str, tz) -> Optional[str]:
    from aegis.cache import _parse_utc_iso
    try:
        dt = _parse_utc_iso(iso).astimezone(tz)
    except Exception:  # noqa: BLE001
        return None
    return dt.strftime("%Y-%m-%d")


def _comparable_pairs(
    observations, ttl_seconds: float
) -> List[Tuple[object, object]]:
    """按窗口键分组后，把时间升序的相邻观测组成可比对。"""
    groups: Dict[Tuple, List[object]] = {}
    for obs in observations:
        for win_prefix in (("five",), ("weekly",)):
            used = getattr(obs, f"{win_prefix[0]}_used")
            resets = getattr(obs, f"{win_prefix[0]}_resets_at")
            wmin = getattr(obs, f"{win_prefix[0]}_window_minutes")
            if used is None or resets is None or wmin is None:
                continue
            key = (win_prefix[0], resets, wmin)
            groups.setdefault(key, []).append(obs)
    pairs: List[Tuple[object, object]] = []
    max_gap = 2.0 * ttl_seconds
    for key, obs_list in groups.items():
        obs_list.sort(key=lambda o: o.fetched_at_utc)
        for a, b in zip(obs_list, obs_list[1:]):
            from aegis.cache import _parse_utc_iso
            try:
                gap = (
                    _parse_utc_iso(b.fetched_at_utc) - _parse_utc_iso(a.fetched_at_utc)
                ).total_seconds()
            except Exception:  # noqa: BLE001
                continue
            if 0 < gap <= max_gap:
                pairs.append((a, b))
    return pairs


def confirmed_increase_days(
    observations, ttl_seconds: float = DEFAULT_TTL_SECONDS
) -> set:
    """返回存在确认增加的观测 UTC 日期集合（YYYY-MM-DD，UTC 日期归属后条）。"""
    import math
    confirmed: set = set()
    for a, b in _comparable_pairs(observations, ttl_seconds):
        for prefix in ("five", "weekly"):
            ua, ub = getattr(a, f"{prefix}_used"), getattr(b, f"{prefix}_used")
            ra, rb = getattr(a, f"{prefix}_resets_at"), getattr(b, f"{prefix}_resets_at")
            wa, wb = getattr(a, f"{prefix}_window_minutes"), getattr(b, f"{prefix}_window_minutes")
            if None in (ua, ub, ra, rb, wa, wb):
                continue
            if not (math.isfinite(ua) and math.isfinite(ub)):
                continue
            if ra != rb or wa != wb:
                continue  # 重置 / 跨度变化 → 中断
            if ub > ua:
                confirmed.add(b.fetched_at_utc[:10])
                break
    return confirmed


def footprint_7d(
    observations,
    now_utc: datetime,
    tz=DEFAULT_TZ,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> List[DayMark]:
    """最近 7 个本地日（含今天）的足迹标记，时间升序。"""
    confirmed = confirmed_increase_days(observations, ttl_seconds)
    observed_days = {
        _local_date(o.fetched_at_utc, tz)
        for o in observations
        if _local_date(o.fetched_at_utc, tz)
    }
    # confirmed 日（UTC 日期）→ 统计时区本地日
    from aegis.cache import _parse_utc_iso
    confirmed_local = set()
    for obs_obs in observations:
        utc_day = obs_obs.fetched_at_utc[:10]
        if utc_day in confirmed:
            local_day = _local_date(obs_obs.fetched_at_utc, tz)
            if local_day:
                confirmed_local.add(local_day)

    today = now_utc.astimezone(tz).date()
    marks: List[DayMark] = []
    for i in range(6, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        if day in confirmed_local:
            kind = "confirmed_increase"
        elif day in observed_days:
            kind = "observed"
        else:
            kind = "none"
        marks.append(DayMark(date=day, kind=kind))
    return marks
