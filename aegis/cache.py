"""本地缓存层（ARCHITECTURE.md §8 + ROUTE_REVIEW M-3）。

职责：

1. **写时机**：仅在某 provider 本轮抓取**成功**时更新它的条目；失败不写、
   不清缓存。
2. **TTL 判定**：每个 provider 条目记 ``fetched_at_utc``（该 provider 最近
   一次成功抓取时刻）。``--watch`` 循环里距 ``fetched_at`` 未到 TTL 的
   provider 直接复用缓存，``reliability='cached'``；到期才重抓。
3. **失败回退三件套**（防伪装核心规则）：

   * 有缓存 → 用缓存数据，但 ``reliability='stale'``，``updated_at`` 保持
     **缓存里的原值**（绝不改成 now），``status='error'`` +
     ``error{type, message}`` 同屏标出失败原因。
   * 无缓存 → 该 provider 显示 error 态行：``status='error'``，数据列全
     ``None``。
   * 任何情况下**不允许** ``reliability='live'`` 且数据来自缓存。

4. **健壮性**：缓存文件损坏 / 不可解析 → 视同无缓存，**不**抛异常。

文件位置：``<项目根>/cache/usage_latest.json``（已在 ``.gitignore``）。
测试通过模块级常量 + :func:`monkeypatch.setattr` 重定向文件路径。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aegis.model import Snapshot


# ---------------------------------------------------------------------------
# 路径常量（测试可整体或逐项 monkeypatch）
# ---------------------------------------------------------------------------

# 缓存目录：<项目根>/cache（与 probe_raw/ 一起被 .gitignore 覆盖）
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_FILE = CACHE_DIR / "usage_latest.json"

CACHE_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# 缓存条目
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CachedEntry:
    """一个 provider 的缓存条目。

    :attr fetched_at_utc: 该 provider 最近一次**成功**抓取时刻（UTC ISO）。
    :attr snapshot: 完整 :class:`Snapshot`（含 ``reliability``，通常为
        ``live`` 或 ``cached``，**绝不**为 ``stale`` —— stale 仅在
        ``resolve_after_fetch`` 失败回退时即时构造）。
    """

    fetched_at_utc: str
    snapshot: Snapshot


# ---------------------------------------------------------------------------
# 路径 / 时间工具
# ---------------------------------------------------------------------------

def _ensure_dir(path: Path) -> None:
    """确保父目录存在；用于 :func:`write_provider`。"""
    path.parent.mkdir(parents=True, exist_ok=True)


def _now_utc_iso(now: Optional[datetime] = None) -> str:
    """返回 ``now`` 的 UTC ISO Z 字符串；``now`` 为 ``None`` 时取系统当前时刻。"""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        # 防御：禁止 naive datetime 入缓存
        raise ValueError("_now_utc_iso: now must be timezone-aware")
    out = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if now.microsecond:
        out += f".{now.microsecond // 1000:03d}"
    return out + "Z"


def _parse_utc_iso(s: str) -> datetime:
    """``YYYY-MM-DDTHH:MM:SS[.fff]Z`` → aware :class:`datetime` (UTC)。

    与 :func:`aegis.normalize.iso_to_utc` 行为一致；缓存内字段由本模块
    自产自销（``_now_utc_iso`` 输出），这里独立实现一份避免循环依赖。
    """
    if not isinstance(s, str) or not s.strip():
        raise ValueError(f"_parse_utc_iso: empty/invalid string {s!r}")
    normalized = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise ValueError(f"_parse_utc_iso: naive datetime from {s!r}")
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# I/O：read_all / write_provider
# ---------------------------------------------------------------------------

def read_all() -> dict[str, CachedEntry]:
    """读取整个缓存文件。

    :returns: ``{provider_name: CachedEntry, ...}``。文件不存在 / 损坏 /
        不可解析 → 返回空 dict，**不**抛异常。
    """
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = CACHE_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}

    version = data.get("version")
    providers_raw = data.get("providers")
    if not isinstance(providers_raw, dict):
        return {}
    if version != CACHE_FORMAT_VERSION:
        # 版本不兼容 → 视同无缓存（不抛）
        return {}

    out: dict[str, CachedEntry] = {}
    for name, entry in providers_raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        fetched = entry.get("fetched_at_utc")
        snap_raw = entry.get("snapshot")
        if not isinstance(fetched, str) or not isinstance(snap_raw, dict):
            continue
        try:
            snap = _snapshot_from_dict(snap_raw)
        except Exception:
            # 单个 provider 损坏 → 跳过该条，其它仍可用
            continue
        out[name] = CachedEntry(fetched_at_utc=fetched, snapshot=snap)
    return out


def write_provider(
    provider: str,
    snapshot: Snapshot,
    *,
    now: Optional[datetime] = None,
) -> None:
    """更新**单个** provider 的缓存条目。

    仅当 ``snapshot.status == 'ok'`` 时才实际写盘（ARCH §8）。失败路径由
    调用方在 ``resolve_after_fetch`` 后决定**不**调本函数。

    若该 provider 当前不在缓存里则新增；若已存在则替换。其它 provider 条
    目原样保留。

    :raises OSError: 写盘失败（I/O 错误）。
    """
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"write_provider: invalid provider {provider!r}")
    if snapshot.status != "ok":
        # ARCH §8 硬约束：失败不写缓存
        return

    _ensure_dir(CACHE_FILE)

    # 先读已有数据（若有），保留其它 provider 条目
    existing = read_all()
    existing[provider] = CachedEntry(
        fetched_at_utc=_now_utc_iso(now),
        snapshot=snapshot,
    )

    payload = {
        "version": CACHE_FORMAT_VERSION,
        "providers": {
            name: {
                "fetched_at_utc": entry.fetched_at_utc,
                "snapshot": entry.snapshot.to_dict(),
            }
            for name, entry in existing.items()
        },
    }
    tmp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 原子替换（Windows 上 os.replace 在同卷上也是原子的）
    os.replace(tmp, CACHE_FILE)


# ---------------------------------------------------------------------------
# TTL / freshness 判定
# ---------------------------------------------------------------------------

def is_fresh(entry: CachedEntry, now: datetime, ttl_seconds: int) -> bool:
    """检查 ``entry`` 的 ``fetched_at_utc`` 距 ``now`` 是否在 TTL 内。

    :raises ValueError: ``ttl_seconds`` 非正 / ``now`` naive / 时间格式错。
    """
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError(f"is_fresh: ttl_seconds must be positive int, got {ttl_seconds!r}")
    if now.tzinfo is None:
        raise ValueError("is_fresh: now must be timezone-aware")
    fetched = _parse_utc_iso(entry.fetched_at_utc)
    delta = (now.astimezone(timezone.utc) - fetched).total_seconds()
    return 0.0 <= delta < ttl_seconds


# ---------------------------------------------------------------------------
# --watch 模式：fresh 时直接复用缓存
# ---------------------------------------------------------------------------

def get_if_fresh(
    provider: str,
    cached: Optional[CachedEntry],
    now: datetime,
    ttl_seconds: int,
) -> Optional[Snapshot]:
    """若 ``cached`` 在 TTL 内 → 返回带 ``reliability='cached'`` 的快照。

    否则（含 ``cached is None`` / 已过期 / 格式错）→ 返回 ``None``，由调用
    方决定发起新抓取。**绝不**返回 ``reliability='live'``（违反"不允许 live
    标记缓存数据"的规则）。
    """
    if cached is None:
        return None
    try:
        fresh = is_fresh(cached, now, ttl_seconds)
    except ValueError:
        return None
    if not fresh:
        return None
    cs = cached.snapshot
    return Snapshot(
        provider=cs.provider,
        status=cs.status,
        five_hour=cs.five_hour,
        weekly=cs.weekly,
        balance=cs.balance,
        extras=cs.extras,
        updated_at=cs.updated_at,
        source=cs.source,
        reliability="cached",          # ★ 关键：缓存命中标签
        error=cs.error,
        raw_status=cs.raw_status,
    )


# ---------------------------------------------------------------------------
# 单次抓取后的失败回退三件套
# ---------------------------------------------------------------------------

def resolve_after_fetch(
    provider: str,
    fetched: Snapshot,
    cached: Optional[CachedEntry],
) -> Snapshot:
    """本轮抓取后，应用 ARCH §8 的失败回退规则：

    * ``fetched.status == 'ok'`` → 返回 ``reliability='live'`` 快照
      （调用方应再调 :func:`write_provider` 入缓存）。
    * ``fetched.status == 'error'`` 且**有** cached → 返回
      ``status='error'`` + ``reliability='stale'`` + ``updated_at`` 保持
      缓存原值 + ``error`` 用最新一次抓取的错误。
    * ``fetched.status == 'error'`` 且**无** cached → 原样返回 ``fetched``
      （``status='error'``、所有数值字段 ``None``）。

    关键不变量：返回快照的 ``reliability`` 只可能是 ``live``（本轮成功）
    或 ``stale``（失败但有缓存）。**绝不会**出现 ``live`` + 缓存数据，也
    不会从缓存里继承 ``cached`` / ``stale``（覆盖为本轮的真实状态）。
    """
    if not isinstance(fetched, Snapshot):
        raise TypeError(
            f"resolve_after_fetch: fetched must be Snapshot, got {type(fetched).__name__}"
        )

    if fetched.status == "ok":
        # 本轮成功 → live；不依赖 cached
        return Snapshot(
            provider=provider,
            status="ok",
            five_hour=fetched.five_hour,
            weekly=fetched.weekly,
            balance=fetched.balance,
            extras=fetched.extras,
            updated_at=fetched.updated_at,
            source=fetched.source,
            reliability="live",          # ★ 关键：本轮成功 → live
            raw_status=fetched.raw_status,
        )

    # ---- 失败分支 ----
    if cached is None:
        # 无缓存 → 原 error；reliability 保留（fetched 已是 error，且无数据可标 live）
        return fetched

    cs = cached.snapshot
    return Snapshot(
        provider=provider,
        status="error",                 # ★ status: error
        five_hour=cs.five_hour,         # ★ 数据来自缓存
        weekly=cs.weekly,
        balance=cs.balance,
        extras=cs.extras,
        updated_at=cs.updated_at,       # ★ updated_at 保持缓存原值
        source=cs.source,
        reliability="stale",            # ★ reliability: stale
        error=fetched.error,            # ★ error: 最新一次的错误
        raw_status=cs.raw_status,
    )


# ---------------------------------------------------------------------------
# 内部：Snapshot ↔ dict（用于读盘反序列化）
# ---------------------------------------------------------------------------

def _snapshot_from_dict(d: dict) -> Snapshot:
    """``dict`` → :class:`Snapshot`（缓存读盘用）。

    这里**复用** :func:`aegis.model.Window` / :class:`ExtraWindow` 的字段
    形状，但不引入额外的反序列化层（避免把 ``model.py`` 变成双向 JSON 绑
    定）。字段缺失时退化为 ``None``，与正向构造语义保持一致。
    """
    from aegis.model import Balance, ErrorInfo, ExtraWindow, Window

    def _opt_window(raw):
        if not isinstance(raw, dict):
            return None
        try:
            used, remaining = float(raw.get("used_percent", 0)), float(
                raw.get("remaining_percent", 0)
            )
        except (TypeError, ValueError):
            return None
        return Window(
            used_percent=used,
            remaining_percent=remaining,
            resets_at=raw.get("resets_at"),
            window_minutes=raw.get("window_minutes"),
        )

    def _opt_balance(raw):
        if not isinstance(raw, dict):
            return None
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            return None

        def _opt_field(key):
            raw_val = raw.get(key)
            if raw_val is None:
                return None
            try:
                return float(raw_val)
            except (TypeError, ValueError):
                return None

        return Balance(
            value=value,
            currency=raw.get("currency") or "",
            # v0.3 增量分项（可选；坏值→None 不致命）
            granted_balance=_opt_field("granted_balance"),
            topped_up_balance=_opt_field("topped_up_balance"),
        )

    def _opt_error(raw):
        if not isinstance(raw, dict):
            return None
        return ErrorInfo(
            type=str(raw.get("type") or ""),
            message=str(raw.get("message") or ""),
        )

    extras_raw = d.get("extras") or []
    extras = []
    if isinstance(extras_raw, list):
        for e in extras_raw:
            if not isinstance(e, dict):
                continue
            try:
                used = float(e.get("used_percent", 0))
            except (TypeError, ValueError):
                used = 0.0
            extras.append(
                ExtraWindow(
                    id=str(e.get("id") or ""),
                    title=str(e.get("title") or ""),
                    used_percent=used,
                    resets_at=e.get("resets_at"),
                    reset_description=e.get("reset_description"),
                    is_informational=bool(e.get("is_informational")),
                    window_minutes=e.get("window_minutes"),
                )
            )

    return Snapshot(
        provider=str(d.get("provider") or ""),
        status=str(d.get("status") or "error"),
        five_hour=_opt_window(d.get("five_hour")),
        weekly=_opt_window(d.get("weekly")),
        balance=_opt_balance(d.get("balance")),
        extras=extras,
        updated_at=d.get("updated_at"),
        source=str(d.get("source") or ""),
        reliability=str(d.get("reliability") or "live"),
        error=_opt_error(d.get("error")),
        raw_status=d.get("raw_status"),
    )


__all__ = [
    "CACHE_DIR",
    "CACHE_FILE",
    "CACHE_FORMAT_VERSION",
    "CachedEntry",
    "get_if_fresh",
    "is_fresh",
    "read_all",
    "resolve_after_fetch",
    "write_provider",
]