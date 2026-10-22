"""纯函数适配器：SnapshotEnvelope + 能力矩阵 → ViewModel（设计 §13 adapter.py）。

不依赖 Qt；时间格式化所需 ``now`` 显式传入（禁止渲染时刻冒充采集时刻 ——
D-007 / 设计 §4 对 ``ui._format_updated`` 的修复）。数值格式遵循 DESIGN.md §4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from aegis.services.contracts import (
    AVAIL_ERROR,
    AVAIL_OK,
    AVAIL_UNAVAILABLE,
    CAPABILITIES,
    COMPLETE_PARTIAL,
    FRESH_CACHED,
    FRESH_LIVE,
    FRESH_NONE,
    FRESH_STALE,
    SnapshotEnvelope,
)

from aegis.desktop import theme
from aegis.desktop.theme import SPENT_HINT, STALE_DATA_TAG

# 状态枚举（chip 渲染键；与 DESIGN.md §7 表一一对应）
ST_LIVE = "LIVE"
ST_CACHED = "CACHED"
ST_CACHED_EXPIRED = "CACHED_EXPIRED"
ST_PARTIAL = "PARTIAL"
ST_UNAVAILABLE = "UNAVAILABLE"
ST_ERROR = "ERROR"


@dataclass(frozen=True)
class WindowVM:
    """单个窗口的展示值。

    v3 语义：**used_value / used_text 是唯一主展示（“X% 已用”）**，取自
    snapshot.used_percent（归一化层已保证 0–100 且语义=已用）——适配层只
    做一次映射，组件层不做第二次换算。remaining_* 保留供详情页展示。
    """

    used_text: str
    remaining_text: str
    remaining_value: Optional[float]
    used_value: Optional[float]     # 已用百分比真实值（进度条填充比例）
    reset_text: str                 # "今天 21:29 · 约 3 小时后" / "09-19 周五 16:28" / "重置时间未提供" / "待确认"
    reset_expired: bool
    reset_tooltip: str              # 完整日期 + 时区；未知为空
    low_remaining: bool             # 剩余 ≤10%（已用 ≥90%）：沿用 v0.2 阈值，以“已用”解释


@dataclass(frozen=True)
class ExtraVM:
    """Codex extras 的白名单化展示（只保留 id/title/used 数值）。"""

    id: str
    title: str
    used_percent_text: str


@dataclass(frozen=True)
class ProviderVM:
    """一张卡片的全部展示输入。"""

    provider: str
    display_name: str
    status: str                     # ST_* 常量
    status_text: str
    loading: bool
    is_balance_card: bool
    headline_label: str             # "5h 剩余" / "余额"
    headline_value: str             # "45.5%" / "¥74.16"；无数据 "—"
    headline_unit: str              # "" / 币种提示
    five: Optional[WindowVM]
    weekly: Optional[WindowVM]
    balance_text: str               # 仅 BalanceCard
    unsupported_note: str           # DeepSeek: "该来源仅提供余额"
    fetched_text: str               # "采集 3 分钟前" / "采集于 14:20"
    fetched_tooltip: str
    error_text: str                 # 固定中文文案（可能为空）
    partial_tooltip: str            # 逐项缺项说明
    extras: List[ExtraVM] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 数字格式（DESIGN.md §4）
# ---------------------------------------------------------------------------

def format_percent(value: Optional[float]) -> str:
    """一位小数、去尾零；端点保护：显示舍入不得把非端点变成 0%/100%。"""
    if value is None:
        return "—"
    if value > 0 and round(value, 1) == 0.0:
        return "<0.1%"
    if value < 100 and round(value, 1) == 100.0:
        return ">99.9%"
    s = f"{value:.1f}".rstrip("0").rstrip(".")
    return (s + "%") if s else "0%"


def format_balance(value: Optional[float], currency: Optional[str]) -> str:
    """两位小数 + 币种；仅 CNY 用 ¥；<0.01 且 >0 显示 <0.01。"""
    if value is None:
        return "—"
    cur = currency or ""
    if 0 < abs(value) < 0.01:
        return f"<0.01 {cur}" if cur != "CNY" else "<0.01 ¥"
    if cur == "CNY":
        return f"¥{value:.2f}"
    return f"{value:.2f} {cur}".strip()


# ---------------------------------------------------------------------------
# 时间格式（DESIGN.md §6；now 显式传入）
# ---------------------------------------------------------------------------

_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        from aegis.cache import _parse_utc_iso
        return _parse_utc_iso(s)
    except Exception:  # noqa: BLE001
        return None


def format_reset(resets_at: Optional[str], now: datetime) -> Tuple[str, bool, str]:
    """→ (显示文本, 是否已到期未确认, tooltip)。v3 格式（方案：五小时优先
    “今天 HH:mm · 约 X 小时后”；本周显示日期、时间）。"""
    dt = _parse_iso(resets_at)
    if dt is None:
        return ("重置时间未提供", False, "")
    local = dt.astimezone()
    delta = dt - now
    full = local.strftime("%Y-%m-%d %H:%M") + f" (UTC{local.strftime('%z')[:3]}:{local.strftime('%z')[3:]})"
    if delta.total_seconds() <= 0:
        return ("重置时间已到，待确认", True, full)
    now_local = now.astimezone()
    hours = delta.total_seconds() / 3600.0
    if hours >= 1:
        approx = f"约 {int(hours)} 小时后" if hours < 48 else f"约 {int(hours // 24)} 天后"
    else:
        approx = f"约 {max(1, int(delta.total_seconds() // 60))} 分钟后"
    if local.date() == now_local.date():
        text = f"今天 {local.strftime('%H:%M')} · {approx}"
    elif (local.date() - now_local.date()).days == 1:
        text = f"明天 {local.strftime('%H:%M')} · {approx}"
    else:
        text = f"{local.strftime('%m-%d')} {_WEEKDAY[local.weekday()]} {local.strftime('%H:%M')}"
    return (text, False, full)


def format_fetched(last_success_iso: Optional[str], now: datetime) -> Tuple[str, str]:
    """→ (显示文本, tooltip)。空值 = "尚未成功更新"（绝不填渲染时刻）。"""
    dt = _parse_iso(last_success_iso)
    if dt is None:
        return ("尚未成功更新", "")
    delta = (now - dt).total_seconds()
    local = dt.astimezone()
    if delta < 0:
        # 时钟回拨：显示绝对时间并提示
        text = f"采集于 {local.strftime('%m-%d %H:%M')}"
        return (text + "（时钟异常？）", text)
    if delta < 60:
        text = "采集刚刚"
    elif delta < 3600:
        text = f"采集 {int(delta // 60)} 分钟前"
    elif now.astimezone().date() == local.date():
        text = f"采集于 {local.strftime('%H:%M')}"
    else:
        text = f"采集于 {local.strftime('%m-%d %H:%M')}"
    tooltip = local.strftime("%Y-%m-%d %H:%M:%S") + f" (UTC{local.strftime('%z')[:3]}:{local.strftime('%z')[3:]})"
    return (text, tooltip)


# ---------------------------------------------------------------------------
# 状态判定（DESIGN.md §7 状态文案合同；纯函数）
# ---------------------------------------------------------------------------

def compute_status(env: SnapshotEnvelope, now: datetime, ttl_seconds: int) -> str:
    """按三维 + TTL 判定卡片状态。错误优先；unsupported 属字段层不进此处。"""
    if env.availability == AVAIL_UNAVAILABLE:
        return ST_UNAVAILABLE
    if env.availability == AVAIL_ERROR:
        return ST_ERROR
    # OK
    if env.completeness == COMPLETE_PARTIAL:
        return ST_PARTIAL
    if env.freshness == FRESH_LIVE:
        return ST_LIVE
    if env.freshness == FRESH_CACHED:
        # TTL 已过但仍显示缓存 → 待更新
        fetched = _parse_iso(env.time.last_success_fetched_at)
        if fetched is not None and (now - fetched).total_seconds() >= ttl_seconds:
            return ST_CACHED_EXPIRED
        return ST_CACHED
    if env.freshness == FRESH_STALE:
        return ST_ERROR
    return ST_UNAVAILABLE


_STATUS_TO_TEXT = {
    ST_LIVE: "已更新",
    ST_CACHED: "已保存",
    ST_CACHED_EXPIRED: "待更新",
    ST_PARTIAL: "部分数据",
    ST_UNAVAILABLE: "暂不可用",
    ST_ERROR: "更新失败",
}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _window_vm(win, now: datetime) -> WindowVM:
    reset_text, reset_expired, reset_tip = format_reset(win.resets_at, now)
    remaining = win.remaining_percent
    used = win.used_percent
    low = remaining is not None and remaining <= 10.0
    return WindowVM(
        # v3 单次映射：used_percent 已是“已用”语义（归一化层保证 0–100），
        # 此处不再做 100−x 换算；剩余字段仅供详情页展示。
        used_text=format_percent(used),
        remaining_text=format_percent(remaining),
        remaining_value=remaining,
        used_value=used,
        reset_text=reset_text,
        reset_expired=reset_expired,
        reset_tooltip=reset_tip,
        low_remaining=low,
    )


def build_provider_vm(
    env: SnapshotEnvelope,
    *,
    now: datetime,
    ttl_seconds: int,
) -> ProviderVM:
    """信封 + 能力矩阵 → 卡片 ViewModel（不访问网络/文件/凭据）。"""
    cap = CAPABILITIES[env.provider]
    status = compute_status(env, now, ttl_seconds)
    snap = env.snapshot

    five = weekly = None
    balance_text = "—"
    extras: List[ExtraVM] = []
    headline_label = "5h 剩余"
    headline_value = "—"
    is_balance = False
    unsupported_note = ""

    if snap is not None and snap.status == "ok":
        if cap.supports_five_hour and snap.five_hour is not None:
            five = _window_vm(snap.five_hour, now)
            headline_value = five.remaining_text
        if cap.supports_weekly and snap.weekly is not None:
            weekly = _window_vm(snap.weekly, now)
        if cap.supports_balance:
            is_balance = True
            headline_label = "余额"
            if snap.balance is not None:
                balance_text = format_balance(snap.balance.value, snap.balance.currency)
                headline_value = balance_text
            unsupported_note = "该来源仅提供余额"
        for ex in getattr(snap, "extras", None) or []:
            extras.append(
                ExtraVM(
                    id=str(getattr(ex, "id", "") or ""),
                    title=str(getattr(ex, "title", "") or ""),
                    used_percent_text=format_percent(getattr(ex, "used_percent", None)),
                )
            )

    # 窗口不支持的字段层说明（DeepSeek 5h/Weekly）
    partial_notes: List[str] = []
    if env.completeness == COMPLETE_PARTIAL:
        if snap is not None and snap.status == "ok":
            if cap.supports_five_hour and (snap.five_hour is None or (
                    cap.reset_required and snap.five_hour.resets_at is None)):
                partial_notes.append("5h 窗口数据或重置时间缺失")
            if cap.supports_weekly and (snap.weekly is None or (
                    cap.reset_required and snap.weekly.resets_at is None)):
                partial_notes.append("Weekly 窗口数据或重置时间缺失")

    fetched_text, fetched_tip = format_fetched(env.time.last_success_fetched_at, now)

    error_text = ""
    if status == ST_ERROR:
        error_text = env.error_display_text() or "更新失败"
        if env.freshness == FRESH_STALE:
            error_text = f"旧数据 · {error_text}"

    return ProviderVM(
        provider=env.provider,
        display_name=cap.display_name,
        status=status,
        status_text=_STATUS_TO_TEXT.get(status, status),
        loading=env.loading,
        is_balance_card=is_balance,
        headline_label=headline_label,
        headline_value=headline_value,
        headline_unit="" if not is_balance else "",
        five=five,
        weekly=weekly,
        balance_text=balance_text,
        unsupported_note=unsupported_note,
        fetched_text=fetched_text,
        fetched_tooltip=fetched_tip,
        error_text=error_text,
        partial_tooltip="；".join(partial_notes),
        extras=extras,
    )


def build_summary(vms) -> str:
    """顶部汇总（v3 措辞）：已更新 / 失败 / 部分数据 三类分开计数。"""
    live = sum(1 for v in vms if v.status == ST_LIVE)
    failed = sum(1 for v in vms if v.status in (ST_ERROR, ST_UNAVAILABLE))
    partial = sum(1 for v in vms if v.status == ST_PARTIAL)
    other = len(vms) - live - failed - partial
    parts = [f"{live} 家已更新"]
    if failed:
        parts.append(f"{failed} 家失败")
    if partial:
        parts.append(f"{partial} 家部分数据")
    if other:
        parts.append(f"{other} 家待处理")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# DeepSeek 金额卡（v3；方案 §DeepSeek 独立金额卡）
# ---------------------------------------------------------------------------

# 金额卡状态（口径闸门；v0.4.1 起仅"已花费"计算使用，首页不再展示充值说明）
MO_OK = "OK"                            # 口径一致且数据齐全 → 已花费 = 累计充值 − 余额
MO_REGISTER_PENDING = "REGISTER_PENDING"  # 累计充值未登记
MO_NO_TOPUP = "NO_TOPUP"                # T=0 且 B=0 → 暂无充值
MO_BALANCE_UNAVAILABLE = "BALANCE_UNAVAILABLE"  # 余额缺失 / 读取失败
MO_MISMATCH = "MISMATCH"                # B>T / 币种不一致 / 含未拆分赠送余额 → 口径待核对


# 手动登记的累计充值：定义在服务层（aegis.services.topup），此处仅导入使用
from aegis.services.topup import TopupConfig  # noqa: E402


@dataclass(frozen=True)
class MoneyVM:
    """DeepSeek 钱包卡展示值（v0.4.1：简洁两金额）。

    口径（全部真实，不编造）：
    - 钱包剩余 = 官方 balance 接口当前余额（value）。
    - 已花费：上游没有消费累计字段；仅当手动登记的累计充值与余额口径一致
      （compute_money 的 MO_OK 闸门）时按 累计充值 − 当前余额 计算；
      否则 spent_text = "—"，spent_hint 集中说明缺什么。
    """

    state: str
    balance_text: str               # "¥64.80" / "暂无数据"
    spent_text: str                 # "¥35.20" / "—"
    spent_hint: str                 # spent_text 为 "—" 时的口径说明（悬停展示）；已知为空
    balance_time_text: str          # 上次余额时间（失败/旧数据时标注）


def format_money(value: Optional[float], currency: Optional[str]) -> str:
    """金额两位小数；仅 CNY 用 ¥，其他币种保留 ISO 代码。"""
    if value is None:
        return "—"
    cur = currency or ""
    if cur == "CNY":
        return f"¥{value:.2f}"
    return f"{value:.2f} {cur}".strip()


def compute_money(
    balance_value: Optional[float],
    currency: Optional[str],
    granted_balance: Optional[float],
    topup: Optional[TopupConfig],
) -> Tuple[str, str, str, Optional[float], str]:
    """DeepSeek 已用计算（方案口径闸门）。

    仅当 T>0、0≤B≤T、币种一致、且无未拆分赠送余额时启用：
    U = T − B，P = U / T × 100。

    :returns: (state, topup_text, used_text, fill_percent, note)
    """
    has_topup_cfg = topup is not None and topup.total is not None
    T = float(topup.total) if has_topup_cfg else None

    topup_text = format_money(T, topup.currency if topup else "CNY") if has_topup_cfg else "待登记"

    # 余额缺失 / 读取失败
    if balance_value is None:
        return (MO_BALANCE_UNAVAILABLE, topup_text, "", None,
                "余额暂不可用，不计算新的用量")

    if not has_topup_cfg:
        # 累计充值未登记：继续显示余额，中性说明，不从余额猜
        return (MO_REGISTER_PENDING, topup_text, "", None,
                "缺少充值总额，暂不计算已用比例")

    cur_ok = (topup.currency or "CNY") == (currency or "CNY")
    granted = granted_balance if granted_balance is not None else 0.0

    if T == 0 and balance_value == 0:
        return (MO_NO_TOPUP, topup_text, "", None, "暂无充值记录")

    problems: List[str] = []
    if not cur_ok:
        problems.append(f"币种不一致（充值 {topup.currency} / 余额 {currency}）")
    if granted > 0:
        problems.append(f"余额含未拆分赠送余额 {format_money(granted, currency)}")
    if balance_value < 0 or balance_value > T:
        problems.append("余额大于累计充值（可能存在未登记充值或退款）")

    if problems:
        return (MO_MISMATCH, topup_text, "", None,
                "；".join(problems) + "。显示原始金额，暂不生成已用比例")

    U = T - balance_value
    P = U / T * 100.0
    return (MO_OK, topup_text,
            f"已用 {format_money(U, currency)} · {format_percent(P)}",
            P, "已用 = 累计充值 − 当前余额（手动登记口径，非官方累计消费）")


def build_money_vm(
    env: SnapshotEnvelope,
    *,
    now: datetime,
    ttl_seconds: int,
    topup: Optional[TopupConfig],
) -> MoneyVM:
    """DeepSeek 钱包卡 ViewModel（v0.4.1：钱包剩余 + 已花费）。"""
    status = compute_status(env, now, ttl_seconds)
    snap = env.snapshot
    balance = getattr(snap, "balance", None) if snap is not None else None
    currency = getattr(balance, "currency", None) if balance is not None else None
    granted = getattr(balance, "granted_balance", None) if balance is not None else None

    state, _topup_text, _used_text, fill, note = compute_money(
        balance.value if balance is not None else None,
        currency,
        granted,
        topup,
    )

    # 钱包剩余 = 真实余额；快照本身无余额时"暂无数据"，绝不填零
    balance_text = format_money(balance.value, currency) if balance is not None else "—"

    # 已花费：仅 MO_OK（登记充值与余额口径一致）时按 T − B 计算；
    # 其余状态一律 "—"，用 spent_hint 集中说明缺少什么，不从余额猜花费。
    if state == MO_OK:
        spent_text = format_money((topup.total or 0.0) - (balance.value or 0.0), currency)
        spent_hint = ""
    else:
        spent_text = "—"
        spent_hint = theme.SPENT_HINT.get(state, note)

    # 失败 / 旧数据：标注上次余额时间（状态区悬停可见）
    balance_time_text = ""
    if status == ST_ERROR and balance is not None:
        fetched_text, _ = format_fetched(env.time.last_success_fetched_at, now)
        balance_time_text = f"{STALE_DATA_TAG} · {fetched_text}"

    return MoneyVM(
        state=state,
        balance_text=balance_text,
        spent_text=spent_text,
        spent_hint=spent_hint,
        balance_time_text=balance_time_text,
    )
