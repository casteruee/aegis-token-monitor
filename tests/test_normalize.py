"""ARCHITECTURE.md §7.2 / §7.3 的纯函数回归测试。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from aegis.normalize import (
    MINIMAX_INTERVAL_MS,
    MINIMAX_WEEKLY_MS,
    NormalizationError,
    UnexpectedShape,
    assert_minimax_interval_span,
    assert_minimax_weekly_span,
    assert_window_span,
    epoch_ms_to_utc_iso,
    from_remaining,
    from_used,
    iso_to_utc,
)


# ---------------------------------------------------------------------------
# from_used
# ---------------------------------------------------------------------------

class TestFromUsed:
    """源是「已用%」（Codex / z.ai）。"""

    def test_basic_95(self):
        assert from_used(95) == (95.0, 5.0)

    def test_basic_zero(self):
        assert from_used(0) == (0.0, 100.0)

    def test_basic_full(self):
        assert from_used(100) == (100.0, 0.0)

    def test_basic_fractional(self):
        used, remaining = from_used(33.5)
        assert used == 33.5
        assert remaining == 66.5

    def test_int_input_accepted(self):
        used, remaining = from_used(50)
        assert (used, remaining) == (50.0, 50.0)

    @pytest.mark.parametrize("bad", [-0.01, -1, 100.01, 101, 999])
    def test_out_of_range_raises(self, bad):
        with pytest.raises(NormalizationError):
            from_used(bad)

    def test_non_numeric_raises(self):
        with pytest.raises(NormalizationError):
            from_used("nope")

    def test_nan_rejected(self):
        with pytest.raises(NormalizationError):
            from_used(float("nan"))

    def test_inf_rejected(self):
        with pytest.raises(NormalizationError):
            from_used(float("inf"))


# ---------------------------------------------------------------------------
# from_remaining
# ---------------------------------------------------------------------------

class TestFromRemaining:
    """源是「剩余%」（MiniMax）。"""

    def test_basic_93(self):
        # ARCHITECTURE §7.2：MiniMax interval remaining=93 → used≈7
        assert from_remaining(93) == (7.0, 93.0)

    def test_basic_zero(self):
        assert from_remaining(0) == (100.0, 0.0)

    def test_basic_full(self):
        assert from_remaining(100) == (0.0, 100.0)

    def test_basic_fractional(self):
        used, remaining = from_remaining(72.5)
        assert remaining == 72.5
        assert used == 27.5

    @pytest.mark.parametrize("bad", [-0.01, -1, 100.01, 101])
    def test_out_of_range_raises(self, bad):
        with pytest.raises(NormalizationError):
            from_remaining(bad)

    def test_non_numeric_raises(self):
        with pytest.raises(NormalizationError):
            from_remaining(None)


# ---------------------------------------------------------------------------
# 时间归一化 —— epoch 毫秒 → UTC ISO（ARCHITECTURE §7.3 四个向量）
# ---------------------------------------------------------------------------

# (epoch_ms, (year, month, day, hour, minute, second, microsecond))
_TIME_VECTORS = [
    (1789112389135, (2026, 9, 11, 7, 39, 49, 135000)),   # z.ai 5h
    (1789193655997, (2026, 9, 12, 6, 14, 15, 997000)),   # z.ai weekly (with ms)
    (1789120800000, (2026, 9, 11, 10, 0, 0, 0)),         # MiniMax end_time
    (1789344000000, (2026, 9, 14, 0, 0, 0, 0)),         # MiniMax weekly_end
]


class TestEpochMsToUtcIso:
    @pytest.mark.parametrize("ms,expected", _TIME_VECTORS)
    def test_vector_roundtrip(self, ms, expected):
        iso = epoch_ms_to_utc_iso(ms)
        dt = iso_to_utc(iso)
        actual = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)
        assert actual == expected

    @pytest.mark.parametrize("ms,expected", _TIME_VECTORS)
    def test_vector_iso_format(self, ms, expected):
        iso = epoch_ms_to_utc_iso(ms)
        # 必须以 Z 结尾
        assert iso.endswith("Z")
        # ISO 格式：YYYY-MM-DDTHH:MM:SS[.fff]Z
        import re
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z", iso), iso

    def test_zero_microsecond_no_dot(self):
        # 整秒没有小数点
        assert "." not in epoch_ms_to_utc_iso(1789120800000)

    def test_with_millisecond_has_three_digits(self):
        iso = epoch_ms_to_utc_iso(1789112389135)
        # 毫秒部分必须三位精度
        assert ".135Z" in iso

    @pytest.mark.parametrize("bad", ["1700000000", 1.5, None, [1700000000000]])
    def test_invalid_input_raises(self, bad):
        with pytest.raises(NormalizationError):
            epoch_ms_to_utc_iso(bad)


# ---------------------------------------------------------------------------
# 时间归一化 —— ISO 解析（M-4：Z 替换 + naive 拒绝）
# ---------------------------------------------------------------------------

class TestIsoToUtc:
    def test_with_z_suffix(self):
        dt = iso_to_utc("2026-09-11T07:51:57Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (
            2026, 9, 11, 7, 51, 57,
        )

    def test_without_z_but_with_offset(self):
        dt = iso_to_utc("2026-09-11T07:51:57+00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_with_positive_offset_keeps_offset(self):
        dt = iso_to_utc("2026-09-11T15:51:57+08:00")
        assert dt.tzinfo is not None
        # 内部统一存 UTC；但解析后保留原 offset，便于上层转换
        assert dt.utcoffset() == timedelta(hours=8)

    def test_with_fractional_seconds_and_z(self):
        dt = iso_to_utc("2026-09-12T06:14:15.997Z")
        assert dt.microsecond == 997000

    def test_naive_string_rejected(self):
        with pytest.raises(NormalizationError) as excinfo:
            iso_to_utc("2026-09-11T07:51:57")
        # 错误消息要指路（无凭据内容）
        assert "naive" in str(excinfo.value)

    def test_empty_string_rejected(self):
        with pytest.raises(NormalizationError):
            iso_to_utc("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(NormalizationError):
            iso_to_utc("   ")

    def test_garbage_rejected(self):
        with pytest.raises(NormalizationError):
            iso_to_utc("not-a-date")

    def test_non_string_rejected(self):
        with pytest.raises(NormalizationError):
            iso_to_utc(12345)

    def test_z_replacement_3_9_compat(self):
        """M-4：即使在不支持 Z 后缀的版本上也应该能解析。

        这里只验证 ``replace('Z','+00:00')`` 的语义被实际采用：传 Z 字符串与
        传 +00:00 字符串解析出的 dt 在 UTC 投影下完全相等。
        """
        z_dt = iso_to_utc("2026-09-11T07:51:57Z")
        off_dt = iso_to_utc("2026-09-11T07:51:57+00:00")
        # 同一时刻
        assert z_dt.astimezone(timezone.utc) == off_dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# MiniMax 窗口跨度断言（ARCHITECTURE §7.3）
# ---------------------------------------------------------------------------

class TestMiniMaxSpan:
    def test_interval_ok(self):
        assert assert_minimax_interval_span(1789102800000, 1789120800000) is None

    def test_weekly_ok(self):
        assert assert_minimax_weekly_span(1788739200000, 1789344000000) is None

    def test_interval_off_by_one(self):
        with pytest.raises(UnexpectedShape) as excinfo:
            assert_minimax_interval_span(1789102800000, 1789120800001)
        assert str(MINIMAX_INTERVAL_MS) in str(excinfo.value)

    def test_weekly_off_by_one(self):
        with pytest.raises(UnexpectedShape):
            assert_minimax_weekly_span(1788739200000, 1789343999999)

    def test_interval_reversed_rejected(self):
        with pytest.raises(UnexpectedShape):
            assert_minimax_interval_span(1789120800000, 1789102800000)

    def test_weekly_reversed_rejected(self):
        with pytest.raises(UnexpectedShape):
            assert_minimax_weekly_span(1789344000000, 1788739200000)

    def test_float_whole_number_accepted(self):
        assert assert_minimax_interval_span(1789102800000.0, 1789120800000.0) is None

    def test_float_fractional_rejected(self):
        with pytest.raises(UnexpectedShape):
            assert_minimax_interval_span(1789102800000.5, 1789120800000)


class TestAssertWindowSpanGeneric:
    def test_custom_name(self):
        with pytest.raises(UnexpectedShape) as excinfo:
            assert_window_span(0, 1, 100, "my_window")
        assert "my_window" in str(excinfo.value)

    def test_bool_rejected(self):
        with pytest.raises(UnexpectedShape):
            assert_window_span(True, 100, 100, "x")

    def test_str_rejected(self):
        with pytest.raises(UnexpectedShape):
            assert_window_span("0", "100", 100, "x")
