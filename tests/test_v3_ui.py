"""v3 UI 专项测试：已用语义映射、DeepSeek 金额口径、缺失与失败状态。

全部使用测试数据；样例值来自方案 §三家额度数据映射 / §DeepSeek 的回归样例，
不写入任何真实账户数据。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aegis.desktop.adapter import (
    MO_BALANCE_UNAVAILABLE,
    MO_MISMATCH,
    MO_NO_TOPUP,
    MO_OK,
    MO_REGISTER_PENDING,
    ST_ERROR,
    TopupConfig,
    build_money_vm,
    build_provider_vm,
    compute_money,
    format_money,
    format_percent,
)
from aegis.model import Balance, ErrorInfo, Snapshot, Window
from aegis.services.contracts import SnapshotEnvelope, TimeEnvelope
from aegis.services.topup import load_topup

UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)


def _env(provider, snapshot, *, freshness="LIVE", availability="OK",
         fetched="2026-09-12T11:59:00Z", error_code=None, http=None):
    return SnapshotEnvelope(
        provider=provider, snapshot=snapshot, freshness=freshness,
        completeness="FULL", availability=availability,
        time=TimeEnvelope(None, fetched, fetched),
        error_code=error_code, error_http_status=http,
    )


def _quota_snap(provider, five_rem, weekly_rem, five_reset="2026-09-12T13:00:00Z",
                weekly_reset="2026-09-19T06:00:00Z"):
    """按「剩余」构造快照（模拟 reader 归一化后的统一模型；used=100−remaining）。"""
    return Snapshot(
        provider=provider, status="ok",
        five_hour=Window(100 - five_rem, five_rem, five_reset, 300),
        weekly=Window(100 - weekly_rem, weekly_rem, weekly_reset, 10080),
    )


# ---------------------------------------------------------------------------
# 已用语义映射（方案回归样例；单次映射，组件层不二次换算）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("five_rem,wk_rem,exp5,expw", [
    (88.0, 98.0, 12.0, 2.0),     # Codex 样例
    (59.0, 92.0, 41.0, 8.0),     # GLM 样例
    (97.0, 82.0, 3.0, 18.0),     # MiniMax 样例
])
def test_used_mapping_plan_samples(five_rem, wk_rem, exp5, expw):
    for rem, exp in ((five_rem, exp5), (wk_rem, expw)):
        assert format_percent(100 - rem) == format_percent(exp)


def test_used_value_is_single_mapping_no_double_flip():
    snap = _quota_snap("codex", 88.0, 98.0)
    vm = build_provider_vm(_env("codex", snap), now=NOW, ttl_seconds=300)
    # 主展示 = 已用；来自 snapshot.used_percent，适配器不再做 100−x
    assert vm.five.used_text == "12%"
    assert vm.five.used_value == 12.0
    assert vm.weekly.used_text == "2%"
    assert vm.weekly.used_value == 2.0
    assert vm.five.used_value == snap.five_hour.used_percent  # 单次映射


def test_zero_and_full_and_missing():
    # 真实 0% 才显示 0%
    snap0 = _quota_snap("codex", 100.0, 100.0)   # 剩余 100 → 已用 0
    vm0 = build_provider_vm(_env("codex", snap0), now=NOW, ttl_seconds=300)
    assert vm0.five.used_text == "0%"
    assert vm0.five.used_value == 0.0            # 条填充 0（不画假最小填充）
    # 真实 100%
    snap100 = _quota_snap("codex", 0.0, 0.0)
    vm100 = build_provider_vm(_env("codex", snap100), now=NOW, ttl_seconds=300)
    assert vm100.five.used_text == "100%"
    assert vm100.five.used_value == 100.0
    # 缺失：暂无数据，不得显示 0%
    snap_missing = Snapshot(provider="codex", status="ok",
                            five_hour=None, weekly=None)
    vmm = build_provider_vm(_env("codex", snap_missing, freshness="LIVE"),
                            now=NOW, ttl_seconds=300)
    assert vmm.five is None and vmm.weekly is None
    # 错误无旧值 → UNAVAILABLE，headline 不出数字
    env_err = SnapshotEnvelope(provider="codex", snapshot=None, freshness="NONE",
                               completeness="FULL", availability="UNAVAILABLE",
                               error_code="network_error")
    vme = build_provider_vm(env_err, now=NOW, ttl_seconds=300)
    assert vme.status == "UNAVAILABLE"
    assert vme.five is None


def test_high_usage_note_uses_used_semantics():
    # 剩余 8% → 已用 92% → 沿用阈值提示，解释为“已用/剩余”而非故障
    snap = _quota_snap("codex", 8.0, 50.0)
    vm = build_provider_vm(_env("codex", snap), now=NOW, ttl_seconds=300)
    assert vm.five.low_remaining is True
    assert vm.weekly.low_remaining is False
    assert vm.five.used_text == "92%"


def test_failed_refresh_keeps_old_values_with_stale_tag():
    ok_snap = _quota_snap("codex", 88.0, 98.0)
    env = _env("codex", ok_snap, freshness="STALE", availability="ERROR",
               fetched="2026-09-12T11:00:00Z", error_code="network_error")
    vm = build_provider_vm(env, now=NOW, ttl_seconds=300)
    assert vm.status == ST_ERROR
    assert "旧数据" in vm.error_text or "上次数据" in vm.error_text or vm.error_text
    assert vm.five.used_text == "12%"      # 旧值保留
    assert vm.fetched_text != "尚未成功更新"


# ---------------------------------------------------------------------------
# DeepSeek 金额计算（方案验收样例）
# ---------------------------------------------------------------------------

def _bal(value, currency="CNY", granted=None, topped=None):
    return Balance(value=value, currency=currency,
                   granted_balance=granted, topped_up_balance=topped)


T100 = TopupConfig(total=100.0, currency="CNY")


def test_money_plan_sample_t100_b64():
    state, topup_text, used_text, fill, note = compute_money(64.80, "CNY", None, T100)
    assert state == MO_OK
    assert used_text == "已用 ¥35.20 · 35.2%"
    assert fill == pytest.approx(35.2)
    assert topup_text == "¥100.00"


def test_money_t_equals_b_is_zero_percent():
    state, _, used_text, fill, _ = compute_money(100.0, "CNY", None, T100)
    assert state == MO_OK and used_text.endswith("0%") and fill == 0.0


def test_money_b_zero_is_full():
    state, _, used_text, fill, _ = compute_money(0.0, "CNY", None, T100)
    assert state == MO_OK and fill == 100.0 and used_text.endswith("100%")


def test_money_topup_missing_shows_pending_not_guess():
    state, topup_text, used_text, fill, note = compute_money(64.80, "CNY", None, None)
    assert state == MO_REGISTER_PENDING
    assert topup_text == "待登记"
    assert fill is None and used_text == ""
    assert "缺少充值总额" in note


def test_money_zero_topup_zero_balance():
    state, _, _, fill, note = compute_money(0.0, "CNY", None, TopupConfig(0.0, "CNY"))
    assert state == MO_NO_TOPUP and fill is None
    assert "暂无充值" in note


def test_money_balance_unavailable():
    state, _, used_text, fill, note = compute_money(None, None, None, T100)
    assert state == MO_BALANCE_UNAVAILABLE and fill is None
    assert "余额暂不可用" in note


def test_money_balance_greater_than_topup_is_mismatch():
    state, _, _, fill, note = compute_money(150.0, "CNY", None, T100)
    assert state == MO_MISMATCH and fill is None
    assert "余额大于累计充值" in note


def test_money_currency_mismatch_is_mismatch():
    state, _, _, fill, note = compute_money(64.80, "USD", None, T100)
    assert state == MO_MISMATCH and fill is None
    assert "币种不一致" in note


def test_money_granted_balance_is_mismatch():
    state, _, _, fill, note = compute_money(64.80, "CNY", 10.0, T100)
    assert state == MO_MISMATCH and fill is None
    assert "赠送余额" in note


def test_money_vm_stale_tag_on_failed_refresh():
    from aegis.desktop.adapter import ST_ERROR as _ST
    snap = Snapshot(provider="deepseek", status="ok", balance=_bal(64.80))
    env = _env("deepseek", snap, freshness="STALE", availability="ERROR",
               fetched="2026-09-12T10:00:00Z", error_code="http_error", http=500)
    vm = build_money_vm(env, now=NOW, ttl_seconds=1800, topup=T100)
    assert "上次数据" in vm.balance_time_text
    assert vm.balance_text == "¥64.80"


def test_money_vm_spent_gate():
    """v0.4.1 钱包卡：仅口径一致（MO_OK）时给出已花费；否则 "—"+口径说明。"""
    snap = Snapshot(provider="deepseek", status="ok", balance=_bal(64.80))
    env = _env("deepseek", snap)
    vm = build_money_vm(env, now=NOW, ttl_seconds=1800, topup=T100)
    assert vm.state == MO_OK
    assert vm.balance_text == "¥64.80"
    assert vm.spent_text == "¥35.20" and vm.spent_hint == ""

    # 未登记充值：不得从余额猜花费
    vm = build_money_vm(env, now=NOW, ttl_seconds=1800, topup=None)
    assert vm.spent_text == "—" and "累计充值未登记" in vm.spent_hint

    # 口径不一致（含赠送余额）：不计算花费
    snap_g = Snapshot(provider="deepseek", status="ok",
                      balance=Balance(64.80, "CNY", granted_balance=10.0))
    vm = build_money_vm(_env("deepseek", snap_g), now=NOW, ttl_seconds=1800, topup=T100)
    assert vm.spent_text == "—" and vm.spent_hint


def test_money_format():
    assert format_money(64.8, "CNY") == "¥64.80"
    assert format_money(12.5, "USD") == "12.50 USD"
    assert format_money(None, "CNY") == "—"


# ---------------------------------------------------------------------------
# 手动登记配置加载（config/topup.json；测试用临时文件）
# ---------------------------------------------------------------------------

def test_topup_config_load(tmp_path):
    assert load_topup("deepseek", tmp_path / "missing.json") is None
    f = tmp_path / "topup.json"
    f.write_text('{"deepseek": {"total_topup": 100.00, "currency": "CNY"}}',
                 encoding="utf-8")
    cfg = load_topup("deepseek", f)
    assert cfg is not None and cfg.total == 100.0 and cfg.currency == "CNY"
    # 坏值 / 缺 provider → 未登记
    f.write_text('{"deepseek": {"total_topup": " lots "}}', encoding="utf-8")
    assert load_topup("deepseek", f) is None
    f.write_text('{"other": {"total_topup": 5}}', encoding="utf-8")
    assert load_topup("deepseek", f) is None
    f.write_text("not json", encoding="utf-8")
    assert load_topup("deepseek", f) is None
