"""适配器纯函数测试：状态判定、数字格式、时间格式、D-007 时间信封。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aegis.desktop.adapter import (
    ST_CACHED,
    ST_CACHED_EXPIRED,
    ST_ERROR,
    ST_LIVE,
    ST_PARTIAL,
    ST_UNAVAILABLE,
    build_provider_vm,
    build_summary,
    compute_status,
    format_balance,
    format_fetched,
    format_percent,
    format_reset,
)
from aegis.model import Balance, Snapshot, Window
from aegis.services.contracts import (
    SnapshotEnvelope,
    TimeEnvelope,
    compute_completeness,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
RESET = "2026-09-12T13:00:00Z"   # 1 小时后


def _env_ok(provider="codex", used=10.0, resets=RESET, freshness="LIVE",
            fetched="2026-09-12T11:59:00Z", completeness="FULL"):
    snap = Snapshot(
        provider=provider, status="ok",
        five_hour=Window(used, 100 - used, resets, 300) if provider != "deepseek" else None,
        weekly=None,
        balance=Balance(74.16, "CNY") if provider == "deepseek" else None,
        updated_at=None if provider in ("zai", "minimax", "deepseek") else "2026-09-12T11:00:00Z",
    )
    return SnapshotEnvelope(
        provider=provider, snapshot=snap, freshness=freshness,
        completeness=completeness, availability="OK",
        time=TimeEnvelope(
            source_updated_at=snap.updated_at,
            last_success_fetched_at=fetched,
            last_attempt_at=fetched,
        ),
    )


# ---- 百分比 / 余额格式（DESIGN §4） ----

def test_percent_one_decimal_and_endpoints():
    assert format_percent(45.0) == "45%"
    assert format_percent(45.26) == "45.3%"
    assert format_percent(99.96) == ">99.9%"      # 舍入不得变 100%
    assert format_percent(0.04) == "<0.1%"        # 舍入不得变 0%
    assert format_percent(0.0) == "0%"            # 真实端点
    assert format_percent(100.0) == "100%"
    assert format_percent(None) == "—"


def test_balance_cny_and_other_currency():
    assert format_balance(74.16, "CNY") == "¥74.16"
    assert format_balance(12.5, "USD") == "12.50 USD"
    assert format_balance(0.004, "CNY") == "<0.01 ¥"
    assert format_balance(0.0, "CNY") == "¥0.00"
    assert format_balance(None, "CNY") == "—"


# ---- Reset / 采集时间 ----

def test_reset_countdown_and_unknown():
    # v3 格式：今天 HH:mm · 约 X 小时后
    text, expired, tip = format_reset("2026-09-12T13:18:00Z", NOW)
    assert "今天" in text and "约" in text and "小时" in text and not expired
    assert "2026-09-12" in tip
    text, expired, _ = format_reset(None, NOW)
    assert text == "重置时间未提供" and not expired
    # 已到期未确认
    text, expired, _ = format_reset("2026-09-12T11:00:00Z", NOW)
    assert expired is True and "待确认" in text
    # 较远日期（日期 + 周几 + 时间）
    text, _, _ = format_reset("2026-09-18T06:14:00Z", NOW)
    assert "09-18" in text
    # 明天
    text, _, _ = format_reset("2026-09-13T09:30:00Z", NOW)
    assert text.startswith("明天")


def test_fetched_age_never_uses_render_time():
    text, _ = format_fetched("2026-09-12T11:30:00Z", NOW)
    assert text == "采集 30 分钟前"
    text, _ = format_fetched("2026-09-12T11:59:30Z", NOW)
    assert text == "采集刚刚"
    text, _ = format_fetched(None, NOW)
    assert text == "尚未成功更新"           # 绝不显示渲染时刻
    # 时钟回拨
    text, _ = format_fetched("2026-09-12T13:00:00Z", NOW)
    assert "时钟异常" in text


# ---- 状态判定（DESIGN §7） ----

def test_status_mapping():
    assert compute_status(_env_ok(freshness="LIVE"), NOW, 300) == ST_LIVE
    assert compute_status(_env_ok(freshness="CACHED",
                                  fetched="2026-09-12T11:56:00Z"), NOW, 300) == ST_CACHED
    # TTL 已过的缓存 → 待更新
    assert compute_status(_env_ok(freshness="CACHED",
                                  fetched="2026-09-12T11:00:00Z"), NOW, 300) == ST_CACHED_EXPIRED
    # error + 无旧值
    env = SnapshotEnvelope(provider="codex", snapshot=None,
                           availability="UNAVAILABLE", freshness="NONE")
    assert compute_status(env, NOW, 300) == ST_UNAVAILABLE
    # error + 有旧值（stale）
    env = _env_ok(freshness="STALE", completeness="FULL")
    env.availability = "ERROR"
    env.error_code = "network_error"
    assert compute_status(env, NOW, 300) == ST_ERROR


def test_partial_on_missing_reset_for_required_provider():
    # codex 能力矩阵要求 Reset；缺失 → PARTIAL
    env = _env_ok(resets=None)
    assert compute_completeness("codex", env.snapshot) == "PARTIAL"
    # z.ai 不要求 Reset（D-006 新窗口形态）；两窗口齐全 → FULL
    snap = Snapshot(
        provider="zai", status="ok",
        five_hour=Window(10.0, 90.0, None, 300),      # 重置缺失但 z.ai 不要求
        weekly=Window(20.0, 80.0, "2026-09-19T00:00:00Z", 10080),
    )
    assert compute_completeness("zai", snap) == "FULL"
    # z.ai 缺一个窗口 → PARTIAL（支持的窗口缺失）
    snap_half = Snapshot(provider="zai", status="ok",
                         five_hour=Window(10.0, 90.0, None, 300), weekly=None)
    assert compute_completeness("zai", snap_half) == "PARTIAL"
    # DeepSeek 只要求 balance
    env = _env_ok(provider="deepseek")
    assert compute_completeness("deepseek", env.snapshot) == "FULL"


def test_deepseek_windows_are_never_fabricated():
    vm = build_provider_vm(_env_ok(provider="deepseek"), now=NOW, ttl_seconds=1800)
    assert vm.five is None and vm.weekly is None
    assert vm.is_balance_card
    assert vm.headline_value == "¥74.16"
    assert vm.unsupported_note == "该来源仅提供余额"


def test_vm_error_text_is_safe_chinese():
    env = _env_ok(freshness="STALE")
    env.availability = "ERROR"
    env.error_code = "http_error"
    env.error_http_status = 401
    vm = build_provider_vm(env, now=NOW, ttl_seconds=300)
    assert "HTTP 401" in vm.error_text
    assert "旧数据" in vm.error_text


def test_summary_counts_only_live():
    vms = [build_provider_vm(_env_ok(provider=p, freshness=f), now=NOW, ttl_seconds=300)
           for p, f in (("codex", "LIVE"), ("zai", "CACHED"),
                        ("minimax", "LIVE"), ("deepseek", "LIVE"))]
    # cached → 待处理
    for v, p in zip(vms, ("codex", "zai", "minimax", "deepseek")):
        if p == "zai":
            v = type(v)(**{**v.__dict__})
            object.__setattr__(v, "status", ST_CACHED)
            vms[1] = v
    s = build_summary(vms)
    assert s.startswith("3 家已更新") and "1 家待处理" in s
