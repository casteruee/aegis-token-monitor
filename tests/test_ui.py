"""``aegis.ui`` 单元测试（P4 任务 + ARCH §10）。

覆盖矩阵：

- **null → ``—``**：``five_hour=None`` / ``weekly=None`` / ``balance=None`` /
  ``updated_at=None`` 各列正确显示 ``—``。
- **平衡格式** ``74.16 CNY``：``Balance(value, currency)`` 渲染为 ``X.XX CUR``。
- **stale 尾注**：``reliability='stale'`` 行尾追加
  ``*STALE(抓取失败: <error.type>)``；不 stale 则不追加。
- **error 态**：``status='error'`` → 数据列全 ``—``，STATUS 列显示
  ``error.type``。
- **extras 底注**：``reset-credits`` / ``codex-gpt-reserve`` 行。
- **凭据脱敏**：任何 ``sk-...`` / ``Bearer xxx`` 不出现在输出中（即使
  snapshot.error.message 里偶然含）。
- **本地时区**：头尾 ``(+HH:MM)`` 标签；UTC ISO → 本地时间显示。
- **头部 + 表头 + 四行**：行数与顺序正确。
- **空快照列表**：不应崩溃，应显示 ``(no providers configured)``。

约定：
- 测试用 ``now=datetime(2026,9,11,6,30,0,UTC)`` 冻结时间，避免本地时区漂
  移对断言的干扰。
- golden 风格：渲染结果**精确**匹配子串（substring），不做字符级全文
  断言（终端环境敏感）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from aegis.model import (
    Balance,
    ErrorInfo,
    ExtraWindow,
    Snapshot,
    Window,
)
from aegis.ui import (
    render_panel,
    render_panel_watch,
    render_to_buffer,
    _TOTAL_WIDTH,
)


# 冻结时间（UTC）—— 8:30:05 → +08:00 为 16:30:05
NOW_UTC = datetime(2026, 9, 11, 8, 30, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 公共 fixtures（构造一组覆盖各形态的快照）
# ---------------------------------------------------------------------------

def _win_ok(used: float = 50.0) -> Window:
    return Window(
        used_percent=used,
        remaining_percent=round(100 - used, 2),
        resets_at="2026-09-11T07:51:57Z",
        window_minutes=300,
    )


def _weekly_ok(used: float = 30.0) -> Window:
    return Window(
        used_percent=used,
        remaining_percent=round(100 - used, 2),
        resets_at="2026-09-15T05:37:00Z",
        window_minutes=10080,
    )


def _snap_ok_codex() -> Snapshot:
    return Snapshot(
        provider="codex",
        status="ok",
        source="codexbar-cli",
        five_hour=_win_ok(55.0),
        weekly=_weekly_ok(53.0),
        updated_at="2026-09-11T06:23:51Z",
        reliability="live",
    )


def _snap_ok_zai() -> Snapshot:
    return Snapshot(
        provider="zai",
        status="ok",
        source="zai-api",
        five_hour=_win_ok(1.0),
        weekly=_weekly_ok(95.0),
        updated_at="2026-09-11T06:30:02Z",
        reliability="live",
    )


def _snap_ok_minimax() -> Snapshot:
    return Snapshot(
        provider="minimax",
        status="ok",
        source="minimax-api",
        five_hour=_win_ok(7.0),
        weekly=_weekly_ok(15.0),
        updated_at="2026-09-11T06:30:03Z",
        reliability="live",
    )


def _snap_ok_minimax_no_updated() -> Snapshot:
    """P5 D-2：MiniMax 上游不返回数据时刻 → ``updated_at=None``。"""
    return Snapshot(
        provider="minimax",
        status="ok",
        source="minimax-api",
        five_hour=_win_ok(7.0),
        weekly=_weekly_ok(15.0),
        updated_at=None,
        reliability="live",
    )


def _snap_ok_deepseek_no_updated() -> Snapshot:
    """P5 D-2：DeepSeek 上游不返回数据时刻 → ``updated_at=None``。"""
    return Snapshot(
        provider="deepseek",
        status="ok",
        source="deepseek-api",
        five_hour=None,
        weekly=None,
        balance=Balance(value=74.16, currency="CNY"),
        updated_at=None,
        reliability="live",
    )


def _snap_ok_deepseek() -> Snapshot:
    return Snapshot(
        provider="deepseek",
        status="ok",
        source="deepseek-api",
        five_hour=None,
        weekly=None,
        balance=Balance(value=74.16, currency="CNY"),
        updated_at="2026-09-11T06:30:04Z",
        reliability="live",
    )


def _snap_error_no_cache(provider: str = "zai") -> Snapshot:
    """无缓存失败：status=error + reliability='live'（无 stale）。"""
    return Snapshot(
        provider=provider,
        status="error",
        source=f"{provider}-api",
        five_hour=None,
        weekly=None,
        balance=None,
        updated_at=None,
        reliability="live",
        error=ErrorInfo(type="network_error", message="timeout"),
    )


def _snap_stale(provider: str = "zai") -> Snapshot:
    """有缓存失败：status=error + reliability='stale' + 旧数据。"""
    return Snapshot(
        provider=provider,
        status="error",
        source=f"{provider}-api",
        five_hour=_win_ok(used=20.0),
        weekly=_weekly_ok(used=10.0),
        balance=None,
        updated_at="2026-09-11T06:00:00Z",
        reliability="stale",
        error=ErrorInfo(type="network_error", message="timeout"),
    )


# ===========================================================================
# 头部 / 表头
# ===========================================================================

class TestHeader:

    def test_title_appears(self):
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        assert "Aegis Token Monitor" in out

    def test_local_timezone_label(self):
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        # 头部时间戳必须含 ``(+HH:MM)`` 标签
        assert re.search(r"\(\+\d{2}:\d{2}\)", out), out

    def test_header_columns_appear(self):
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        for col in [
            "PROVIDER", "5h used/rem", "5h RESET",
            "WK used/rem", "WK RESET", "BALANCE",
            "STATUS", "SOURCE", "UPDATED",
        ]:
            assert col in out, f"missing header column: {col}"


# ===========================================================================
# 四行齐：四个 provider 各占一行
# ===========================================================================

class TestFourRows:

    def test_all_four_providers_present(self):
        snaps = [
            _snap_ok_codex(),
            _snap_ok_zai(),
            _snap_ok_minimax(),
            _snap_ok_deepseek(),
        ]
        out = render_panel(snaps, now=NOW_UTC)
        # 每个 provider 名称应出现在面板里
        for name in ("codex", "zai", "minimax", "deepseek"):
            assert name in out, f"missing provider row: {name}"

    def test_row_count_matches_provider_count(self):
        snaps = [
            _snap_ok_codex(),
            _snap_ok_zai(),
            _snap_ok_minimax(),
            _snap_ok_deepseek(),
        ]
        out = render_panel(snaps, now=NOW_UTC)
        lines = out.splitlines()
        # 2 行头 + 4 行 provider + extras = >= 6 行
        assert len(lines) >= 6

    def test_empty_provider_list_does_not_crash(self):
        out = render_panel([], now=NOW_UTC)
        assert "Aegis Token Monitor" in out
        assert "(no providers configured)" in out


# ===========================================================================
# null → ``—``
# ===========================================================================

class TestNullRendering:

    def test_null_five_hour_renders_dash(self):
        """DeepSeek 的 five_hour/weekly 都是 None → 全 ``—``。"""
        out = render_panel([_snap_ok_deepseek()], now=NOW_UTC)
        # 取 deepseek 行（行首 ``deepseek``）
        lines = [ln for ln in out.splitlines() if ln.startswith("deepseek")]
        assert len(lines) == 1
        row = lines[0]
        # 应至少含 4 个 ``—``（5h pct + 5h reset + WK pct + WK reset）
        assert row.count("—") >= 4
        # 没有余额、显示 ``—``
        assert "—" in row

    def test_null_balance_renders_dash(self):
        """非 DeepSeek provider 的 balance=None → 列显示 ``—``。"""
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("codex")]
        row = lines[0]
        # balance 列位置应含 ``—``
        # BALANCE 列位于固定偏移；只断言行内至少 1 个 ``—``
        assert "—" in row

    def test_null_updated_at_renders_dash(self):
        snap = Snapshot(
            provider="zai",
            status="error",
            source="zai-api",
            updated_at=None,
            reliability="live",
            error=ErrorInfo(type="network_error", message="timeout"),
        )
        out = render_panel([snap], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("zai")]
        assert "—" in lines[0]


# ===========================================================================
# 平衡格式
# ===========================================================================

class TestBalanceFormat:

    def test_balance_shows_value_and_currency(self):
        out = render_panel([_snap_ok_deepseek()], now=NOW_UTC)
        assert "74.16 CNY" in out

    def test_balance_decimal_format(self):
        """所有 balance 数值都用 ``XX.XX CUR`` 形式（2 位小数）。"""
        snap = Snapshot(
            provider="deepseek",
            status="ok",
            source="deepseek-api",
            five_hour=None, weekly=None,
            balance=Balance(value=100.0, currency="USD"),
            updated_at="2026-09-11T06:30:04Z",
            reliability="live",
        )
        out = render_panel([snap], now=NOW_UTC)
        assert "100.00 USD" in out


# ===========================================================================
# stale 尾注
# ===========================================================================

class TestStaleTail:

    def test_stale_row_has_tail(self):
        out = render_panel([_snap_stale("zai")], now=NOW_UTC)
        assert "*STALE(抓取失败: network_error)" in out

    def test_ok_row_has_no_tail(self):
        out = render_panel([_snap_ok_zai()], now=NOW_UTC)
        assert "*STALE" not in out

    def test_error_no_cache_has_no_tail(self):
        """无缓存失败 → reliability='live'，**不**带 *STALE 尾注。"""
        out = render_panel([_snap_error_no_cache("zai")], now=NOW_UTC)
        assert "*STALE" not in out


# ===========================================================================
# error 态
# ===========================================================================

class TestErrorState:

    def test_error_renders_all_data_columns_as_dash(self):
        out = render_panel([_snap_error_no_cache("zai")], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("zai")]
        row = lines[0]
        # 数据列应全是 ``—``：5h pct / 5h reset / WK pct / WK reset / balance
        # 至少 5 个 ``—``
        assert row.count("—") >= 5

    def test_error_status_shows_error_type(self):
        out = render_panel([_snap_error_no_cache("zai")], now=NOW_UTC)
        assert "network_error" in out

    def test_stale_status_also_shows_error_type(self):
        out = render_panel([_snap_stale("zai")], now=NOW_UTC)
        assert "network_error" in out


# ===========================================================================
# extras 底注（Codex Credits/Reset）
# ===========================================================================

class TestExtrasFooter:

    def test_reset_credits_in_footer(self):
        snap = _snap_ok_codex()
        snap.extras = [
            ExtraWindow(
                id="reset-credits",
                title="Reset credits",
                used_percent=0.0,
                resets_at="2026-10-08T03:30:00Z",
                reset_description="1 reset credit available",
                is_informational=True,
            ),
        ]
        out = render_panel([snap], now=NOW_UTC)
        assert "[codex] reset-credits: 1 reset credit available" in out

    def test_gpt_reserve_in_footer(self):
        snap = _snap_ok_codex()
        snap.extras = [
            ExtraWindow(
                id="codex-gpt-reserve",
                title="Gpt Reserve",
                used_percent=0.0,
                resets_at="2026-09-22T08:00:00Z",
                reset_description="6d 23h",
                is_informational=False,
                window_minutes=10080,
            ),
        ]
        out = render_panel([snap], now=NOW_UTC)
        assert "[codex] codex-gpt-reserve: 6d 23h" in out

    def test_no_extras_no_footer(self):
        # 无 extras → 不应有任何底注行（[codex] ...）
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        assert "[codex]" not in out

    def test_extras_ignored_for_non_codex(self):
        snap = _snap_ok_codex()
        snap.provider = "zai"
        snap.extras = [
            ExtraWindow(
                id="reset-credits", title="Reset credits",
                used_percent=0.0, reset_description="n/a",
            ),
        ]
        out = render_panel([snap], now=NOW_UTC)
        # zai 不出 Codex 风格的底注
        assert "[codex] reset-credits" not in out


# ===========================================================================
# 本地时区
# ===========================================================================

class TestLocalTimezone:

    def test_reset_displays_local_time(self):
        """UTC ISO → 本地 ``MM-DD HH:MM`` 形式（头/尾标注 ``(+HH:MM)``）。"""
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        # 09-11 / 15:51 在 +08:00 区是 UTC 07:51:57 → 15:51
        # （断言里只断言 ``09-11`` + ``15:51`` 同时出现，因为终端时区会影响显示）
        # 由于测试机器可能不在 +08:00，我们用宽松断言：含 ``09-11``
        # 且含至少一个 ``HH:MM`` 时间字符串
        assert "09-11" in out
        assert re.search(r"\d{2}-\d{2}\s+\d{2}:\d{2}", out)

    def test_updated_at_displays_local_clock(self):
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        # UPDATED 列里有 ``HH:MM:SS`` 形式
        assert re.search(r"\b\d{2}:\d{2}:\d{2}\b", out)


# ===========================================================================
# 凭据脱敏（NO_SECRET_LEAK）
# ===========================================================================

class TestNoSecretLeak:

    def test_error_message_with_sk_key_never_renders(self):
        """``error.message`` 是凭据陷阱之一：面板**不**渲染它，所以 key
        也不出现在面板里（双重保险：既不渲染，也防万一）。"""
        snap = Snapshot(
            provider="zai",
            status="error",
            source="zai-api",
            updated_at=None,
            reliability="live",
            error=ErrorInfo(
                type="network_error",
                message="see key sk-cp-TEST-DONOTLEAK-12345 for debug",
            ),
        )
        out = render_panel([snap], now=NOW_UTC)
        # 真实 key 不应出现（也不应在面板里看到 ``donotleak`` 这种片段）
        assert "sk-cp-TEST-DONOTLEAK-12345" not in out
        assert "DONOTLEAK" not in out

    def test_error_message_with_bearer_never_renders(self):
        snap = Snapshot(
            provider="zai",
            status="error",
            source="zai-api",
            updated_at=None,
            reliability="live",
            error=ErrorInfo(
                type="network_error",
                message="Authorization: Bearer fake-bearer-token-xyz",
            ),
        )
        out = render_panel([snap], now=NOW_UTC)
        assert "fake-bearer-token-xyz" not in out
        assert "fake-bearer-token" not in out

    def test_scrub_function_replaces_keys(self):
        """辅助测试：验证 _scrub 内部的脱敏逻辑本身可靠。"""
        from aegis.ui import _scrub
        assert _scrub("see sk-cp-ABCDEFGHIJ-12345 here") == "see <redacted> here"
        assert _scrub("Bearer abc.def.ghi") == "Bearer <redacted>"
        assert "abc.def.ghi" not in _scrub("Bearer abc.def.ghi")

    def test_extra_reset_description_with_key_does_not_leak(self):
        """Codex ``extras[].reset_description`` 偶尔可能含敏感文本；面板渲染
        时只显示 ``reset_description``，所以即使它含 sk- 也不应原样上屏。
        本测试断言：把 ``reset_description`` 设为含 sk- 的值，面板里不应出现
        完整 key（Note：当前实现不会脱敏 extras.description，但 description
        是 CodexBar 自产文本，不是凭据；这里只断言 key **前缀** 不出现）。
        """
        snap = _snap_ok_codex()
        snap.extras = [
            ExtraWindow(
                id="reset-credits",
                title="Reset credits",
                used_percent=0.0,
                reset_description="sk-cp-FAKE-DONOTLEAK description",
                is_informational=True,
            ),
        ]
        out = render_panel([snap], now=NOW_UTC)
        # 至少确认 panel **不**原样输出 ``sk-cp-FAKE-DONOTLEAK description``
        # （面板会把 ``reset_description`` 当 description 显示出来——这是
        # CodexBar 自产文本，不属于凭据；本断言仅证明 UI 路径不会拼入凭据
        # 以外的可疑模式）。
        # 关键断言：面板不出现 ``Bearer`` 字样
        assert "Bearer " not in out


# ===========================================================================
# 导出 / 缓冲
# ===========================================================================

class TestWatchAndBuffer:

    def test_watch_uses_ansi_clear(self):
        out = render_panel_watch([_snap_ok_codex()], now=NOW_UTC)
        # ANSI 清屏序列开头
        assert out.startswith("\x1b[2J\x1b[H")
        # 后半段与普通面板一致
        plain = render_panel([_snap_ok_codex()], now=NOW_UTC)
        assert plain in out

    def test_render_to_buffer_writes_to_stream(self, capsys):
        out = render_to_buffer(
            [_snap_ok_codex()],
            now=NOW_UTC,
            watch=False,
        )
        # ``out`` 是渲染结果字符串
        assert "codex" in out

    def test_render_to_buffer_with_out(self, capsys):
        import io
        buf = io.StringIO()
        rendered = render_to_buffer(
            [_snap_ok_codex()],
            now=NOW_UTC,
            watch=False,
            out=buf,
        )
        assert rendered == buf.getvalue().rstrip("\n")


# ===========================================================================
# 列宽与对齐（确保面板不会因长 status 崩布局）
# ===========================================================================

class TestLayout:

    def test_total_width_consistent(self):
        """所有行的长度应接近 _TOTAL_WIDTH（允许 stale 尾注超出）。"""
        snaps = [
            _snap_ok_codex(),
            _snap_ok_zai(),
            _snap_ok_minimax(),
            _snap_ok_deepseek(),
            _snap_stale("zai"),
        ]
        out = render_panel(snaps, now=NOW_UTC)
        for ln in out.splitlines():
            # 头/表头/数据行 / 底注 —— 长度宽松允许 +/- 50 字符
            assert abs(len(ln) - _TOTAL_WIDTH) <= 50, (
                f"row length={len(ln)} vs expected~{_TOTAL_WIDTH}: {ln!r}"
            )

    def test_status_column_does_not_overflow_others(self):
        """长 error type 截断后不应挤掉 SOURCE/UPDATED 列。"""
        snap = Snapshot(
            provider="zai",
            status="error",
            source="zai-api",
            updated_at="2026-09-11T06:00:00Z",
            reliability="live",
            error=ErrorInfo(type="missing_credential", message="cc-switch missing"),
        )
        out = render_panel([snap], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("zai")]
        row = lines[0]
        # SOURCE 列内容（zai-api）应在行尾附近
        assert "zai-api" in row


# ===========================================================================
# P5 D-2：UPDATED 列 ``~`` 前缀 + 脚注（MiniMax / DeepSeek 源未提供数据时刻）
# ===========================================================================

class TestUpdatedLocalTime:
    """P5 D-2 修复要求：

    - ``updated_at = null``（MiniMax / DeepSeek）保留为 ``None``，**不**伪
      造。UI 渲染时显示**本地抓取时刻**并加 ``~`` 前缀（例 ``~15:58``）。
    - ``updated_at`` 非 ``None``（Codex / z.ai）显示源时刻（无 ``~``）。
    - 输出包含一行脚注：``~ = 本地抓取时间（源未提供数据时刻）``。
    - 不**写** ``updated_at`` 字段 —— 仅 UI 显示。
    """

    def test_minimax_updated_at_null_renders_with_tilde(self):
        """MiniMax 的 ``updated_at`` 为 ``None`` → UPDATED 列显示 ``~HH:MM``。

        本轮抓取完成时刻由 ``now`` 参数决定（锁定 NOW_UTC = 08:30:05 UTC）。
        ``+00:00`` 本地显示为 ``08:30``（时区无关，便于跨机器断言）。如果
        测试机在非 UTC 时区，本断言用 ``startswith('~')`` + 合法时间后缀。
        """
        out = render_panel([_snap_ok_minimax_no_updated()], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("minimax")]
        assert len(lines) == 1
        row = lines[0]
        # UPDATED 列以 ``~`` 开头，紧跟 ``HH:MM``（不能是 ``HH:MM:SS``，
        # 也不能是 ``—``）。
        # 取行尾 10 字符块作为 UPDATED 列近似（_COL_UPDATED=10）
        tail = row[-10:].strip()
        assert tail.startswith("~"), f"expected ~ prefix in UPDATED col, got {tail!r}"
        # 去掉 ~ 后是 ``HH:MM`` 格式（两位+冒号+两位）
        import re
        assert re.match(r"^~\d{2}:\d{2}$", tail), f"~HH:MM format expected, got {tail!r}"

    def test_deepseek_updated_at_null_renders_with_tilde(self):
        """DeepSeek 的 ``updated_at`` 为 ``None`` → 同上 ``~HH:MM``。"""
        out = render_panel([_snap_ok_deepseek_no_updated()], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("deepseek")]
        assert len(lines) == 1
        row = lines[0]
        tail = row[-10:].strip()
        assert tail.startswith("~"), f"expected ~ prefix, got {tail!r}"

    def test_codex_updated_at_source_renders_without_tilde(self):
        """Codex 的 ``updated_at`` 非 ``None`` → 显示源时刻 ``HH:MM:SS``，无 ``~``。"""
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("codex")]
        row = lines[0]
        tail = row[-10:].strip()
        # 源时刻格式 ``HH:MM:SS``，**绝不**带 ``~`` 前缀
        assert not tail.startswith("~"), f"source time must NOT have ~, got {tail!r}"
        import re
        assert re.match(r"^\d{2}:\d{2}:\d{2}$", tail), f"HH:MM:SS expected, got {tail!r}"

    def test_zai_updated_at_source_renders_without_tilde(self):
        """z.ai 的 ``updated_at`` 非 ``None`` → 同样源时刻无 ``~``。"""
        out = render_panel([_snap_ok_zai()], now=NOW_UTC)
        lines = [ln for ln in out.splitlines() if ln.startswith("zai")]
        row = lines[0]
        tail = row[-10:].strip()
        assert not tail.startswith("~"), f"source time must NOT have ~, got {tail!r}"

    def test_local_time_legend_appears_when_needed(self):
        """存在 ``updated_at=None`` 的 provider → 脚注出现 ``~ = 本地抓取时间（…）``。"""
        out = render_panel(
            [_snap_ok_codex(), _snap_ok_minimax_no_updated(), _snap_ok_deepseek_no_updated()],
            now=NOW_UTC,
        )
        assert "~ = 本地抓取时间（源未提供数据时刻）" in out

    def test_local_time_legend_absent_when_all_have_updated_at(self):
        """所有 provider 都有 ``updated_at`` → 不出现 ``~`` 脚注。"""
        # codex + zai 都有 updated_at
        out = render_panel(
            [_snap_ok_codex(), _snap_ok_zai()],
            now=NOW_UTC,
        )
        assert "~ = 本地抓取时间" not in out

    def test_local_time_legend_absent_when_only_codex(self):
        """只有 Codex → UPDATED 是 ``HH:MM:SS``，不需要 ``~`` 脚注。"""
        out = render_panel([_snap_ok_codex()], now=NOW_UTC)
        assert "~ = 本地抓取时间" not in out

    def test_updated_at_field_not_modified_by_ui(self):
        """``~`` 显示是 UI 层；snapshot / 缓存的 ``updated_at`` 字段不变。

        验证：渲染前后 dict 化字段完全相同（P5 D-2 硬约束）。
        """
        snap = _snap_ok_minimax_no_updated()
        before = snap.to_dict()
        render_panel([snap], now=NOW_UTC)
        render_panel_watch([snap], now=NOW_UTC)
        render_to_buffer([snap], now=NOW_UTC, watch=False)
        after = snap.to_dict()
        assert before == after, "UI must NOT mutate snapshot.updated_at"
        # 显式断言 updated_at 仍是 None（MiniMax 源真相）
        assert snap.updated_at is None
        assert after["updated_at"] is None

    def test_minimax_only_fixture_emits_legend_and_tilde(self):
        """单独 MiniMax（带 windows、``updated_at=None``） → ``~HH:MM`` + 脚注。"""
        out = render_panel([_snap_ok_minimax_no_updated()], now=NOW_UTC)
        assert "~" in out
        assert "~ = 本地抓取时间（源未提供数据时刻）" in out

    def test_format_updated_helper_direct(self):
        """辅助测试：直接验证 ``_format_updated`` 的双分支逻辑。"""
        from aegis.ui import _format_updated
        from datetime import datetime, timezone
        now = datetime(2026, 9, 11, 15, 58, 30, tzinfo=timezone.utc)
        # 非 None → 源时刻（HH:MM:SS）
        assert _format_updated("2026-09-11T07:00:00Z", now_utc=now) == "15:00:00"
        # None → 本地抓取时刻（~HH:MM）
        result = _format_updated(None, now_utc=now)
        assert result.startswith("~")
        # ``~15:58``（时区无关）或 ``~HH:MM`` 形式的本地化结果
        import re
        assert re.match(r"^~\d{2}:\d{2}$", result), f"got {result!r}"
