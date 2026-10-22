"""Provider readers 离线测试（ARCHITECTURE §13 / P2 §6）。

约束：
- 不连真机：所有 ``aegis.net.http_get_json`` 调用由 :func:`monkeypatch` 替代；
  所有凭据由 :func:`fake_creds` fixture 固定为 ``sk-test-aaaaaaaaaaaaaaaaa``。
- 离线 = 无网络也必须全绿。
- DATA_READ 类真机测试由 P3 / Hermes 验收脚本负责（这里不写）。

覆盖矩阵（P2 §6 测试要求逐项列出）：
- NORMALIZATION：z.ai percentage 语义（95 → used 95.0）；
                  MiniMax remaining 翻转（93 → used 7.0）。
- RESET_TIME   ：epoch ms 向量（z.ai ×2、minimax ×2 = §7.3 全部）。
- MiniMax 过滤 general（fixture 里有 video 条目）。
- 畸形 JSON → ParseError（3 家各自覆盖）。
- HttpError(401) → status='error'，**未**切换备用域（zai）；NetworkError → 切换一次。
- M-3：429 / 5xx 不重试 / 不退避。
- NO_SECRET_LEAK：错误快照 + to_dict() 全链路，fake key 不得出现。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from aegis import credentials
from aegis import net
from aegis.providers.deepseek import read as ds_read
from aegis.providers.minimax import read as mm_read
from aegis.providers.zai import (
    BACKUP_URL,
    PRIMARY_URL,
    read as zai_read,
)
from aegis.providers import READERS


# ---------------- 常量 ----------------

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_KEY = "sk-test-aaaaaaaaaaaaaaaaa"
LEAK_NEVER = [FAKE_KEY]

# 完整的 Reader 列表（统一参数化测试用）
_ALL_READERS = {
    "zai": (zai_read, PRIMARY_URL),
    "minimax": (mm_read, None),     # minimax / deepseek 无备用域
    "deepseek": (ds_read, None),
}


# ---------------- autouse：固定凭据 ----------------

@pytest.fixture(autouse=True)
def fake_creds(monkeypatch):
    """把 ``aegis.credentials.get_key`` 全部替换为 ``FAKE_KEY``（不影响真机）。"""
    monkeypatch.setattr(credentials, "get_key", lambda provider: FAKE_KEY)


# ===========================================================================
# z.ai
# ===========================================================================

class TestZaiHappy:
    def test_file_fixture_happy_path(self, monkeypatch):
        fixture = json.loads((FIXTURES / "zai_quota.json").read_text(encoding="utf-8"))
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: fixture)
            s = zai_read()
        assert s.provider == "zai"
        assert s.status == "ok"
        assert s.source == "zai-api"
        # 5h: percentage=12 → used 12.0、remaining 88.0
        assert s.five_hour.used_percent == 12.0
        assert s.five_hour.remaining_percent == 88.0
        assert s.five_hour.window_minutes == 300
        # weekly: percentage=67 → used 67.0、remaining 33.0
        assert s.weekly.used_percent == 67.0
        assert s.weekly.remaining_percent == 33.0
        assert s.weekly.window_minutes == 10080

    def test_percentage_95_means_used_95_not_remaining(self, monkeypatch):
        """P2 §6 硬要求：percentage 是已用% → 95 → used 95.0"""
        body = {
            "code": 200,
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
                     "percentage": 95, "nextResetTime": 1789112389135},
                    {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
                     "percentage": 95, "nextResetTime": 1789193655997},
                ],
            },
        }
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        # 语义关键断言
        assert s.five_hour.used_percent == 95.0
        assert s.five_hour.remaining_percent == 5.0
        assert s.weekly.used_percent == 95.0
        assert s.weekly.remaining_percent == 5.0

    def test_percentage_zero(self, monkeypatch):
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 0,
             "nextResetTime": 1789112389135},
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 0,
             "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.five_hour.used_percent == 0.0
        assert s.five_hour.remaining_percent == 100.0


class TestZaiEpochVectors:
    """ARCH §7.3：已知 epoch ms 向量 → 预期 UTC ISO。"""

    @pytest.mark.parametrize("ms,expected", [
        (1789112389135, "2026-09-11T07:39:49.135Z"),    # z.ai 5h 含毫秒
        (1789193655997, "2026-09-12T06:14:15.997Z"),    # z.ai weekly 含毫秒
    ])
    def test_epoch_ms_to_iso(self, ms, expected, monkeypatch):
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 1,
             "nextResetTime": ms},
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 1,
             "nextResetTime": ms},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.five_hour.resets_at == expected
        assert s.weekly.resets_at == expected


class TestZaiMissingOrExtra:
    def test_missing_unit_3_returns_ok_with_five_hour_none(self, monkeypatch):
        """P5 D-1（收紧到真正的结构性错误）：

        单个 unit 缺失（例如只有 unit=6）→ 该窗口为 ``None``，另一窗口
        照常解析，整体仍 ``status='ok'``。``error`` 字段必须保持 None
        （绝不允许"为了绕过问题而放宽到缺字段就静默返回空对象"）。
        """
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 50,
             "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        assert s.error is None
        # 五小时窗口缺失 → 必须 None（与"放宽为缺字段就静默返回空对象"红线对齐）
        assert s.five_hour is None
        # 周窗口照常解析
        assert s.weekly is not None
        assert s.weekly.used_percent == 50.0
        assert s.weekly.remaining_percent == 50.0
        assert s.weekly.resets_at == "2026-09-12T06:14:15.997Z"
        assert s.weekly.window_minutes == 10080

    def test_missing_unit_6_returns_ok_with_weekly_none(self, monkeypatch):
        """P5 D-1：缺失 unit=6（只有 unit=3）→ 整体仍 ok，``weekly=None``。"""
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 50,
             "nextResetTime": 1789112389135},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        assert s.error is None
        assert s.weekly is None
        assert s.five_hour is not None
        assert s.five_hour.used_percent == 50.0
        assert s.five_hour.remaining_percent == 50.0
        assert s.five_hour.window_minutes == 300

    def test_missing_both_units_raises_unexpected_shape(self, monkeypatch):
        """P5 D-1：unit=3 与 unit=6 **同时**缺失 → 真正的结构性错误，保留
        ``unexpected_shape`` 路径（"不静默补 null"硬约束仍然有效）。
        """
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "OTHER_LIMIT", "unit": 3, "percentage": 50,
             "nextResetTime": 1789112389135},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "error"
        assert s.error is not None
        assert s.error.type == "unexpected_shape"
        # 错误消息要指明两个 unit 都缺失（不允许"缺字段就静默返回空对象"）
        msg = s.error.message
        assert "unit=" in msg

    def test_filters_non_credit_limit(self, monkeypatch):
        """只取 type == CREDIT_LIMIT；其他类型必须被忽略（不能干扰 unit=3/6）。"""
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "OTHER_LIMIT", "unit": 3, "percentage": 99,
             "nextResetTime": 1789112389135},  # 应被忽略
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 30,
             "nextResetTime": 1789112389135},
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 40,
             "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        assert s.five_hour.used_percent == 30.0   # 不是 99.0
        assert s.weekly.used_percent == 40.0

    def test_top_level_code_401_raises(self, monkeypatch):
        body = {"code": 401, "data": {"limits": []}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "error"
        assert s.error.type == "unexpected_shape"

    def test_data_missing_raises(self, monkeypatch):
        body = {"code": 200}  # 缺 data
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "error"


class TestZaiFallback:
    """z.ai 备用域规则：仅 NetworkError 切换；HttpError / ParseError 不切。"""

    def test_http_401_does_not_try_backup(self, monkeypatch):
        """P2 §3 硬要求：HttpError 不能触发备用域。"""
        calls = []
        def fake_http(url, key, timeout=10):
            calls.append(url)
            raise net.HttpError(status=401, message="unauthorized")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = zai_read()
        assert s.status == "error"
        assert s.error.type == "http_error"
        # 仅调主域一次
        assert calls == [PRIMARY_URL]

    def test_http_403_does_not_try_backup(self, monkeypatch):
        calls = []
        def fake_http(url, key, timeout=10):
            calls.append(url)
            raise net.HttpError(status=403, message="forbidden")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = zai_read()
        assert calls == [PRIMARY_URL]
        assert s.error.type == "http_error"

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_m3_no_retry_no_backoff(self, status, monkeypatch):
        """M-3：429 / 5xx 直接抛错，不重试也不切备用域。"""
        calls = []
        def fake_http(url, key, timeout=10):
            calls.append(url)
            raise net.HttpError(status=status, message=f"HTTP {status}")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = zai_read()
        assert calls == [PRIMARY_URL]
        assert s.status == "error"
        assert s.error.type == "http_error"

    def test_network_error_triggers_backup_once(self, monkeypatch):
        """NetworkError 必须切备用域一次。"""
        calls = []
        ok_body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 12,
             "nextResetTime": 1789112389135},
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 67,
             "nextResetTime": 1789193655997},
        ]}}
        def fake_http(url, key, timeout=10):
            calls.append(url)
            if url == PRIMARY_URL:
                raise net.NetworkError("primary timeout")
            return ok_body
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = zai_read()
        assert s.status == "ok"
        assert calls == [PRIMARY_URL, BACKUP_URL]

    def test_network_error_on_both_returns_error(self, monkeypatch):
        calls = []
        def fake_http(url, key, timeout=10):
            calls.append(url)
            raise net.NetworkError("conn failed")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = zai_read()
        assert s.status == "error"
        assert s.error.type == "network_error"
        # 主域 + 备用域各一次（**不**无限重试）
        assert calls == [PRIMARY_URL, BACKUP_URL]

    def test_parse_error_does_not_try_backup(self, monkeypatch):
        calls = []
        def fake_http(url, key, timeout=10):
            calls.append(url)
            raise net.ParseError("not json")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = zai_read()
        # ParseError 也不触发备用域（仅 NetworkError 切）
        assert calls == [PRIMARY_URL]
        assert s.error.type == "parse_error"


class TestZaiLevel:
    def test_level_in_raw_status(self, monkeypatch):
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 1,
             "nextResetTime": 1789112389135},
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 1,
             "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.raw_status is not None
        assert s.raw_status.get("level") == "lite"


# ===========================================================================
# z.ai P5 D-1：新鲜窗口 / 缺字段容错（Hermes 真机验收发现 2026-09-11 15:56）
# ===========================================================================

class TestZaiFreshWindowD1:
    """P5 D-1：5h 窗口刚重置时上游整体省略 ``percentage`` 与 ``nextResetTime``。

    真机实测响应（见 ``tests/fixtures/zai_quota_fresh_window.json``）：

        {"code":200,"data":{"limits":[
          {"type":"CREDIT_LIMIT","unit":3,"number":5,
           "usage":2000,"currentValue":0,"remaining":2000},       ← 无 percentage / nextResetTime
          {"type":"CREDIT_LIMIT","unit":6,"number":1,
           "usage":10000,"currentValue":9649,"remaining":350,
           "percentage":96,"nextResetTime":1789193655997}
        ],"level":"lite"}}

    旧实现会把这种情况当作 ``unexpected_shape`` → 整行 zai 变 error（误判）。
    新行为（§1~§4 修复要求）：
    - ``percentage`` 缺失 → ``currentValue / usage * 100``（usage=0 → 0.0）；
      计算后仍走 ``normalize.from_used``。
    - ``nextResetTime`` 缺失 → ``resets_at = None``（UI 显示 ``—``）。
    - 整体 ``status='ok'``，另一窗口（unit=6）照常解析。
    """

    def test_fresh_window_fixture_parses_as_ok(self, monkeypatch):
        """真机响应 fixture → 整体 ok，5h 窗口 resets_at=None。"""
        fixture = json.loads(
            (FIXTURES / "zai_quota_fresh_window.json").read_text(encoding="utf-8")
        )
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: fixture)
            s = zai_read()
        # 关键断言：整体是 ok，绝不再是 unexpected_shape 误判
        assert s.status == "ok"
        assert s.error is None
        assert s.provider == "zai"
        assert s.source == "zai-api"

        # 5h 窗口：percentage 缺失 → computed = currentValue(0) / usage(2000) = 0.0
        assert s.five_hour is not None
        assert s.five_hour.used_percent == 0.0
        assert s.five_hour.remaining_percent == 100.0
        assert s.five_hour.window_minutes == 300
        # nextResetTime 缺失 → resets_at 必须为 None（不伪造）
        assert s.five_hour.resets_at is None

        # weekly 窗口：percentage=96 来自上游，原样走 from_used
        assert s.weekly is not None
        assert s.weekly.used_percent == 96.0
        assert s.weekly.remaining_percent == 4.0
        assert s.weekly.window_minutes == 10080
        # epoch ms 1789193655997 → UTC ISO（ARCH §7.3 向量）
        assert s.weekly.resets_at == "2026-09-12T06:14:15.997Z"

    def test_percentage_missing_with_current_value_5_percent(self, monkeypatch):
        """``percentage`` 缺失 + ``currentValue=100, usage=2000`` → ``used_percent=5.0``。

        覆盖计算分支（**不**走 falsy/or 兜底）。
        """
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "usage": 2000, "currentValue": 100, "remaining": 1900,
             # 没有 percentage / nextResetTime
             },
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "usage": 10000, "currentValue": 5000, "remaining": 5000,
             "percentage": 50, "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        # 100 / 2000 * 100 = 5.0
        assert s.five_hour.used_percent == 5.0
        assert s.five_hour.remaining_percent == 95.0
        assert s.five_hour.resets_at is None

    def test_usage_zero_falls_back_to_zero(self, monkeypatch):
        """``usage=0`` 时不能除 → ``used_percent=0.0``（窗口刚重置取 0）。"""
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "usage": 0, "currentValue": 0, "remaining": 0},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "usage": 10000, "currentValue": 100, "remaining": 9900,
             "percentage": 1, "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        assert s.five_hour.used_percent == 0.0
        assert s.five_hour.remaining_percent == 100.0

    def test_only_unit_6_returns_ok_with_five_hour_none(self, monkeypatch):
        """只有 unit=6 的 fixture → ``five_hour=None``、``weekly`` 正常、整体 ok。

        这是把"单 unit 缺失"分支从 error 收紧到 ok 的核心用例。
        """
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "percentage": 50, "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        assert s.error is None
        assert s.five_hour is None
        assert s.weekly is not None
        assert s.weekly.used_percent == 50.0

    def test_empty_limits_list_raises_unexpected_shape(self, monkeypatch):
        """``limits=[]`` → 真正的结构性错误（无任何窗口）→ ``unexpected_shape``。

        这是 D-1 §3 三种"真正的结构性错误"之一。
        """
        body = {"code": 200, "data": {"level": "lite", "limits": []}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "error"
        assert s.error is not None
        assert s.error.type == "unexpected_shape"

    def test_no_credit_limit_entries_raises_unexpected_shape(self, monkeypatch):
        """``limits`` 全是非 CREDIT_LIMIT 类型 → 无任何窗口 → ``unexpected_shape``。"""
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "OTHER_LIMIT", "unit": 3, "percentage": 10,
             "nextResetTime": 1789112389135},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "error"
        assert s.error.type == "unexpected_shape"

    def test_resets_at_none_is_not_fabricated_to_local_time(self, monkeypatch):
        """``nextResetTime`` 缺失 → ``resets_at`` 必须是 None，绝不是本地时间 / 缓存值。"""
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "usage": 2000, "currentValue": 0, "remaining": 2000},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "usage": 10000, "currentValue": 0, "remaining": 10000,
             "percentage": 0},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        # 两个窗口的 resets_at 都必须是 None（不能是任何 ISO 字符串）
        assert s.five_hour.resets_at is None
        assert s.weekly.resets_at is None
        # 也不允许 to_dict() 输出里出现伪造的 ISO 字符串
        d = s.to_dict()
        assert d["five_hour"]["resets_at"] is None
        assert d["weekly"]["resets_at"] is None

    def test_current_value_zero_is_legitimate_not_falsy(self, monkeypatch):
        """``currentValue=0`` 是合法值（P5 D-1 数值健壮性）→ ``used_percent=0.0``。

        防止未来实现误用 ``or currentValue`` 兜底变成 1/usage 的值。
        """
        body = {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "usage": 2000, "currentValue": 0, "remaining": 2000},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "usage": 10000, "currentValue": 9649, "remaining": 350,
             "percentage": 96, "nextResetTime": 1789193655997},
        ]}}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", lambda url, key, timeout=10: body)
            s = zai_read()
        assert s.status == "ok"
        # currentValue=0（合法）→ 0.0（不是 0.05 这种误算）
        assert s.five_hour.used_percent == 0.0
        assert s.five_hour.remaining_percent == 100.0


# ===========================================================================
# MiniMax
# ===========================================================================

class TestMinimaxHappy:
    def test_file_fixture_happy_path(self, monkeypatch):
        fixture = json.loads((FIXTURES / "minimax_remains.json").read_text(encoding="utf-8"))
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: fixture)
            s = mm_read()
        assert s.provider == "minimax"
        assert s.status == "ok"
        assert s.source == "minimax-api"
        # file fixture: interval remaining=80 → used 20.0; weekly remaining=72 → used 28.0
        assert s.five_hour.used_percent == 20.0
        assert s.five_hour.remaining_percent == 80.0
        assert s.five_hour.window_minutes == 300
        assert s.weekly.used_percent == 28.0
        assert s.weekly.remaining_percent == 72.0
        assert s.weekly.window_minutes == 10080

    def test_flip_93_remaining_means_used_7(self, monkeypatch):
        """P2 §6 硬要求：MiniMax remaining=93 → used=7.0（必须翻转）。"""
        body = {
            "base_resp": {"status_code": 0, "status_msg": "ok"},
            "model_remains": [{
                "model_name": "general",
                "start_time": 1789102800000,
                "end_time": 1789120800000,
                "current_interval_remaining_percent": 93,
                "weekly_start_time": 1788739200000,
                "weekly_end_time": 1789344000000,
                "current_weekly_remaining_percent": 85,
                "current_interval_status": 1,
                "current_weekly_status": 1,
            }],
        }
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        # 关键语义断言
        assert s.five_hour.used_percent == 7.0
        assert s.five_hour.remaining_percent == 93.0
        assert s.weekly.used_percent == 15.0
        assert s.weekly.remaining_percent == 85.0


class TestMinimaxGeneralFiltering:
    """fixture 里同时有 general 和 video 条目，必须取 general。"""

    def test_picks_general_not_video(self, monkeypatch):
        fixture = json.loads((FIXTURES / "minimax_remains.json").read_text(encoding="utf-8"))
        # sanity：fixture 真包含 video 行（防止 future 误删 fixture）
        assert any(
            isinstance(it, dict) and it.get("model_name") == "video"
            for it in fixture["model_remains"]
        )
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: fixture)
            s = mm_read()
        assert s.status == "ok"
        # general 的 remaining=80 → used 20.0（不是 video 的 100 → 0.0）
        assert s.five_hour.used_percent == 20.0
        assert s.weekly.used_percent == 28.0

    def test_no_general_returns_error(self, monkeypatch):
        body = {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "video",
                    "current_interval_remaining_percent": 100,
                    "current_weekly_remaining_percent": 100,
                    "start_time": 1789102800000, "end_time": 1789120800000,
                    "weekly_start_time": 1788739200000, "weekly_end_time": 1789344000000,
                }]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"
        assert "general" in s.error.message


class TestMinimaxEpochVectors:
    """ARCH §7.3：end_time / weekly_end_time 两个向量。"""

    @pytest.mark.parametrize("end_ms,weekly_end_ms,expected_fh,expected_wk", [
        (1789120800000, 1789344000000,
         "2026-09-11T10:00:00Z", "2026-09-14T00:00:00Z"),
    ])
    def test_vectors(self, end_ms, weekly_end_ms, expected_fh, expected_wk, monkeypatch):
        body = {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "start_time": 1789102800000,
                    "end_time": end_ms,
                    "weekly_start_time": 1788739200000,
                    "weekly_end_time": weekly_end_ms,
                    "current_interval_remaining_percent": 100,
                    "current_weekly_remaining_percent": 100,
                }]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.five_hour.resets_at == expected_fh
        assert s.weekly.resets_at == expected_wk


class TestMinimaxSpanAssertion:
    """窗口跨度不对 → UnexpectedShape（不静默补 null）。"""

    def test_interval_span_mismatch(self, monkeypatch):
        body = {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "start_time": 1789102800000,
                    "end_time": 1789102800000 + 10_000_000,  # 10M 而非 18M
                    "weekly_start_time": 1788739200000,
                    "weekly_end_time": 1789344000000,
                    "current_interval_remaining_percent": 90,
                    "current_weekly_remaining_percent": 80,
                }]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"
        assert "span" in s.error.message.lower() or "window" in s.error.message.lower()

    def test_weekly_span_mismatch(self, monkeypatch):
        body = {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "start_time": 1789102800000,
                    "end_time": 1789120800000,
                    "weekly_start_time": 1788739200000,
                    "weekly_end_time": 1788739200000 + 100_000_000,  # 太短
                    "current_interval_remaining_percent": 90,
                    "current_weekly_remaining_percent": 80,
                }]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"


class TestMinimaxBaseResp:
    def test_non_zero_status_code(self, monkeypatch):
        body = {"base_resp": {"status_code": 1004, "status_msg": "auth failed"},
                "model_remains": []}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"
        # 必须包含 status_msg
        assert "auth failed" in s.error.message
        assert "1004" in s.error.message

    def test_base_resp_missing(self, monkeypatch):
        body = {"model_remains": []}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"

    def test_model_remains_empty(self, monkeypatch):
        body = {"base_resp": {"status_code": 0}, "model_remains": []}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"

    def test_required_field_missing(self, monkeypatch):
        body = {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "start_time": 1789102800000,
                    "end_time": 1789120800000,
                    # weekly_start_time / weekly_end_time / current_weekly_remaining_percent 缺失
                    "current_interval_remaining_percent": 50,
                }]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.status == "error"


class TestMinimaxRawStatus:
    def test_raw_status_kept(self, monkeypatch):
        body = {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "start_time": 1789102800000,
                    "end_time": 1789120800000,
                    "weekly_start_time": 1788739200000,
                    "weekly_end_time": 1789344000000,
                    "current_interval_remaining_percent": 80,
                    "current_weekly_remaining_percent": 70,
                    "current_interval_status": 1,
                    "current_weekly_status": 2,
                }]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = mm_read()
        assert s.raw_status is not None
        assert s.raw_status["current_interval_status"] == 1
        assert s.raw_status["current_weekly_status"] == 2


# ===========================================================================
# DeepSeek
# ===========================================================================

class TestDeepSeekHappy:
    def test_file_fixture(self, monkeypatch):
        fixture = json.loads((FIXTURES / "deepseek_balance.json").read_text(encoding="utf-8"))
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: fixture)
            s = ds_read()
        assert s.provider == "deepseek"
        assert s.status == "ok"
        assert s.source == "deepseek-api"
        # 硬约束：DeepSeek 没有 5h / Weekly 概念 → 必须是 None
        assert s.five_hour is None
        assert s.weekly is None
        # balance
        assert s.balance is not None
        assert s.balance.value == 12.34
        assert s.balance.currency == "CNY"

    def test_no_window_fields_in_source(self, monkeypatch):
        """上游若偶然带 5h/weekly 字段也不读取。"""
        fixture = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "1.50",
                               "granted_balance": "0", "topped_up_balance": "1.50"}],
        }
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: fixture)
            s = ds_read()
        assert s.balance.value == 1.5
        assert s.balance.currency == "USD"
        assert s.five_hour is None
        assert s.weekly is None


class TestDeepSeekUnavailable:
    def test_is_available_false(self, monkeypatch):
        body = {"is_available": False, "balance_infos": []}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = ds_read()
        assert s.status == "error"
        assert s.error.type == "SERVICE_UNAVAILABLE"
        # 必须是 None（不允许 balance / window 有任何值）
        assert s.balance is None
        assert s.five_hour is None
        assert s.weekly is None


class TestDeepSeekMissing:
    def test_balance_infos_missing(self, monkeypatch):
        body = {"is_available": True}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = ds_read()
        assert s.status == "error"
        assert s.error.type == "unexpected_shape"

    def test_balance_infos_empty(self, monkeypatch):
        body = {"is_available": True, "balance_infos": []}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = ds_read()
        assert s.status == "error"

    def test_total_balance_invalid(self, monkeypatch):
        body = {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "not-a-number"},
        ]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = ds_read()
        assert s.status == "error"

    def test_currency_missing(self, monkeypatch):
        body = {"is_available": True, "balance_infos": [
            {"total_balance": "10.00"},
        ]}
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json",
                      lambda url, key, timeout=10: body)
            s = ds_read()
        assert s.status == "error"


# ===========================================================================
# 跨 reader：畸形 JSON / 网络错误 → error snapshot
# ===========================================================================

class TestAllReadersParseError:
    @pytest.mark.parametrize("name,read_fn", [
        ("zai", zai_read),
        ("minimax", mm_read),
        ("deepseek", ds_read),
    ])
    def test_parse_error_returns_error_snapshot(self, name, read_fn, monkeypatch):
        """畸形 JSON → ParseError → status=error，error.type=parse_error。"""
        def fake_http(url, key, timeout=10):
            raise net.ParseError("not json")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = read_fn()
        assert s.provider == name
        assert s.status == "error"
        assert s.error.type == "parse_error"


class TestAllReadersNetworkError:
    @pytest.mark.parametrize("name,read_fn", [
        ("zai", zai_read),
        ("minimax", mm_read),
        ("deepseek", ds_read),
    ])
    def test_network_error_returns_error_snapshot(self, name, read_fn, monkeypatch):
        def fake_http(url, key, timeout=10):
            raise net.NetworkError("conn failed")
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = read_fn()
        assert s.provider == name
        assert s.status == "error"
        assert s.error.type == "network_error"


class TestAllReadersMissingCreds:
    """MissingCredential 路径：error.type == missing_credential，message 含指路信息。"""

    @pytest.mark.parametrize("name,read_fn", [
        ("zai", zai_read),
        ("minimax", mm_read),
        ("deepseek", ds_read),
    ])
    def test_missing_creds(self, name, read_fn, monkeypatch):
        def boom(p):
            raise credentials.MissingCredential("creds not found at /tmp/x.db")
        with monkeypatch.context() as m:
            m.setattr(credentials, "get_key", boom)
            s = read_fn()
        assert s.status == "error"
        assert s.error.type == "missing_credential"
        assert "creds not found" in s.error.message


# ===========================================================================
# NO_SECRET_LEAK：全链路 fake key 隔离
# ===========================================================================

class TestNoSecretLeakEndToEnd:
    """在所有失败路径中，fake key 不得出现于 :func:`Snapshot.to_dict` 或错误消息。"""

    @pytest.mark.parametrize("name,read_fn", [
        ("zai", zai_read),
        ("minimax", mm_read),
        ("deepseek", ds_read),
    ])
    def test_http_error_no_leak(self, name, read_fn, monkeypatch):
        def fake_http(url, key, timeout=10):
            raise net.HttpError(
                status=401,
                message=f"unauthorized sk-test-aaaaaaaaaaaaaaaaa boom",
            )
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = read_fn()
        blob = json.dumps(s.to_dict(), default=str)
        for p in LEAK_NEVER:
            assert p not in blob, f"leak ({name}): {p!r} in {blob!r}"
        for p in LEAK_NEVER:
            assert p not in s.error.message

    @pytest.mark.parametrize("name,read_fn", [
        ("zai", zai_read),
        ("minimax", mm_read),
        ("deepseek", ds_read),
    ])
    def test_parse_error_no_leak(self, name, read_fn, monkeypatch):
        def fake_http(url, key, timeout=10):
            raise net.ParseError(
                f"body contains sk-test-aaaaaaaaaaaaaaaaa token"
            )
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = read_fn()
        blob = json.dumps(s.to_dict(), default=str)
        for p in LEAK_NEVER:
            assert p not in blob
            assert p not in s.error.message

    @pytest.mark.parametrize("name,read_fn", [
        ("zai", zai_read),
        ("minimax", mm_read),
        ("deepseek", ds_read),
    ])
    def test_network_error_no_leak(self, name, read_fn, monkeypatch):
        def fake_http(url, key, timeout=10):
            raise net.NetworkError(
                f"could not resolve sk-test-aaaaaaaaaaaaaaaaa timeout"
            )
        with monkeypatch.context() as m:
            m.setattr(net, "http_get_json", fake_http)
            s = read_fn()
        blob = json.dumps(s.to_dict(), default=str)
        for p in LEAK_NEVER:
            assert p not in blob
            assert p not in s.error.message


# ===========================================================================
# READERS 注册表完整性
# ===========================================================================

class TestReadersRegistry:
    def test_readers_registry_keys(self):
        # P3: codex 也加入 READERS（P3 任务书 §"codex.py 要求"）
        assert set(READERS.keys()) == {"codex", "zai", "minimax", "deepseek"}

    def test_each_reader_returns_snapshot(self, monkeypatch):
        """每家 reader 都必须在 fake http 下返回合法 Snapshot（不需要真机）。"""
        for name, (read_fn, _) in _ALL_READERS.items():
            fake_response = _minimal_ok_payload(name)
            with monkeypatch.context() as m:
                m.setattr(net, "http_get_json",
                          lambda url, key, timeout=10, _b=fake_response: _b)
                s = read_fn()
            from aegis.model import Snapshot
            assert isinstance(s, Snapshot), name
            assert s.provider == name
            assert s.status in ("ok", "error")
            assert s.source.endswith("-api")


def _minimal_ok_payload(name):
    """生成能让对应 reader 进入 ok 分支的最少字段 payload。"""
    if name == "zai":
        return {"code": 200, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 0,
             "nextResetTime": 1789112389135},
            {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 0,
             "nextResetTime": 1789193655997},
        ]}}
    if name == "minimax":
        return {"base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "start_time": 1789102800000,
                    "end_time": 1789120800000,
                    "weekly_start_time": 1788739200000,
                    "weekly_end_time": 1789344000000,
                    "current_interval_remaining_percent": 100,
                    "current_weekly_remaining_percent": 100,
                    "current_interval_status": 1,
                    "current_weekly_status": 1,
                }]}
    if name == "deepseek":
        return {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "0.00",
             "granted_balance": "0.00", "topped_up_balance": "0.00"},
        ]}
    raise AssertionError(name)
