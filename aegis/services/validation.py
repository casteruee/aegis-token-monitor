"""缓存 / 快照的原始结构与数值验证（UI_NEXT_VERSION_DESIGN §4 脆弱点修复）。

对应 ACCEPTANCE §三 CC 低危发现 2 的 v0.2 修复：**损坏缓存条目整体拒绝
（丢弃），而不是回退 0/0 冒充真实数值**。

原则：
- 只验证服务层读入的缓存条目（v0.1 ``cache.read_all`` 过于宽松，本模块在
  其上再加一道闸）；不改 v0.1 ``cache.py`` 的对外语义（冻结 CLI 复用路径）。
- ``isfinite`` 校验：NaN / inf 一律拒绝。
- 时间字段必须可解析为 aware UTC 或显式 None。
- provider 名必须在本项目四个之内。
"""

from __future__ import annotations

import math
from typing import Optional

from aegis.model import Balance, ErrorInfo, ExtraWindow, Snapshot, Window
from aegis.services.contracts import PROVIDER_ORDER


class EntryRejected(Exception):
    """缓存条目损坏 / 结构漂移 —— 调用方应整体丢弃该条目。"""


def _finite_float(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EntryRejected(f"{name}: not a finite number ({type(value).__name__})")
    f = float(value)
    if not math.isfinite(f):
        raise EntryRejected(f"{name}: not finite ({value!r})")
    return f


def _opt_pct(value, name: str) -> float:
    f = _finite_float(value, name)
    if not (0.0 <= f <= 100.0):
        raise EntryRejected(f"{name}: out of range [0,100]: {f}")
    return f


def _opt_utc_iso(value, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EntryRejected(f"{name}: must be non-empty str or null")
    from aegis.normalize import iso_to_utc, NormalizationError
    try:
        iso_to_utc(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except NormalizationError as e:
        raise EntryRejected(f"{name}: unparseable ISO time") from e
    return value


def validate_window(raw, name: str) -> Optional[Window]:
    """验证一个窗口对象；raw 非 dict → None（窗口可缺）。数值坏 → 拒绝。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EntryRejected(f"{name}: not an object")
    used = _opt_pct(raw.get("used_percent"), f"{name}.used_percent")
    remaining = _opt_pct(raw.get("remaining_percent"), f"{name}.remaining_percent")
    resets_at = _opt_utc_iso(raw.get("resets_at"), f"{name}.resets_at")
    wm = raw.get("window_minutes")
    window_minutes: Optional[int] = None
    if wm is not None:
        if isinstance(wm, bool) or not isinstance(wm, int):
            raise EntryRejected(f"{name}.window_minutes: not int")
        window_minutes = wm
    return Window(
        used_percent=used,
        remaining_percent=remaining,
        resets_at=resets_at,
        window_minutes=window_minutes,
    )


def validate_balance(raw) -> Optional[Balance]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EntryRejected("balance: not an object")
    value = _finite_float(raw.get("value"), "balance.value")
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency:
        raise EntryRejected("balance.currency: missing")

    # v0.3 增量分项：坏值→None（不致命），仅用于金额口径核对
    def _opt_finite(key):
        if raw.get(key) is None:
            return None
        try:
            return _finite_float(raw.get(key), f"balance.{key}")
        except EntryRejected:
            return None

    return Balance(
        value=value,
        currency=currency,
        granted_balance=_opt_finite("granted_balance"),
        topped_up_balance=_opt_finite("topped_up_balance"),
    )


def validate_snapshot(provider: str, snap_raw) -> Snapshot:
    """严格验证一个缓存里的 snapshot dict → Snapshot。

    任何结构漂移抛 :class:`EntryRejected`（调用方丢弃整条，不美化、不补 0）。
    """
    if not isinstance(snap_raw, dict):
        raise EntryRejected("snapshot: not an object")
    if provider not in PROVIDER_ORDER:
        raise EntryRejected(f"unknown provider {provider!r}")

    status = snap_raw.get("status")
    if status not in ("ok", "error"):
        raise EntryRejected(f"status invalid: {status!r}")

    # ok 条目数值窗口验证；error 条目数据字段应为空（有值也按坏值丢弃其数值）
    five = validate_window(snap_raw.get("five_hour"), "five_hour")
    weekly = validate_window(snap_raw.get("weekly"), "weekly")
    balance = validate_balance(snap_raw.get("balance"))
    if status == "ok" and five is None and weekly is None and balance is None:
        raise EntryRejected("ok snapshot without any data field")

    updated_at = _opt_utc_iso(snap_raw.get("updated_at"), "updated_at")

    source = snap_raw.get("source")
    if source is not None and not isinstance(source, str):
        raise EntryRejected("source: not a string")

    raw_status = snap_raw.get("raw_status")
    if raw_status is not None and not isinstance(raw_status, dict):
        raw_status = None  # 非法 raw_status 直接剥掉，不致命

    return Snapshot(
        provider=provider,
        status=status,
        five_hour=five,
        weekly=weekly,
        balance=balance,
        extras=[],  # extras 不进 v0.2 服务层缓存通道（Codex extras 由 UI 详情单独白名单化）
        updated_at=updated_at,
        source=source or "",
        reliability=str(snap_raw.get("reliability") or "live"),
        error=None,  # 缓存里的旧错误不回放；错误只属于最近一次尝试
        raw_status=raw_status,
    )
