"""Codex reader 离线 + 真机测试（ARCHITECTURE §1 + §13 + P3 任务书）。

离线（默认跑）：monkeypatch :func:`subprocess.run` 返回 ``tests/fixtures/codex.json``
的脱敏内容；断言 §1 字段映射、used 语义、resets_at ISO、extras 透传、
``pace`` 不被解析、错误分类。

约束（**P3 禁止事项**）：
- 不裸调 ``usage --json`` / ``-p all``（已由 ``CLI_ARGS`` 模块级常量锁住）。
- 不读 ``~/.codex/auth.json``（本模块不引入任何凭据读取路径）。
- 不解析 ``pace`` 字段（_to_snapshot 不引用 ``item['pace']``）。
- 不引入第三方依赖 / pytest 插件。
- NO_SECRET_LEAK：所有错误消息 / to_dict() 输出中**不得**出现假 key。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from aegis import normalize
from aegis.model import Snapshot
from aegis.providers.codex import (
    CLI_ARGS,
    EXE_PATH,
    TIMEOUT_SECONDS,
    CodexSourceUnavailable,
    ParseError,
    _truncate_iso_to_microseconds,
    read,
)


FIXTURES = Path(__file__).parent / "fixtures"

# 模块级假 EXE 路径：用于所有「子进程被 monkeypatch」的离线场景
FAKE_EXE = r"C:/fake/codexbar-cli.exe"

# 假 key：用于 NO_SECRET_LEAK 断言（必须与 fixtures 一致）
FAKE_KEY = "sk-test-aaaaaaaaaaaaaaaaa"
LEAK_NEVER = [FAKE_KEY]


# ===========================================================================
# CLI 常量硬约束
# ===========================================================================

class TestCliConstants:
    """硬约束：必须显式 ``-p codex``，禁止裸 ``usage --json`` / ``-p all``。"""

    def test_usage_arg_present(self):
        assert "usage" in CLI_ARGS

    def test_p_codex_present(self):
        assert "codex" in CLI_ARGS
        # 显式 -p codex（不是 -p all / -p zai / ...）
        idx = CLI_ARGS.index("codex")
        assert idx > 0
        assert CLI_ARGS[idx - 1] == "-p"

    def test_json_flag_present(self):
        assert "--json" in CLI_ARGS

    def test_no_p_all(self):
        # 必须**不**包含 -p all —— 历史坑
        assert "all" not in CLI_ARGS

    def test_no_bare_usage_json(self):
        # 序列中无 "usage", "--json" 紧邻成对（避免出现裸 usage --json 顺序）
        # 这条主要防 "usage" 与 "--json" 顺序颠倒导致 CLI 解释为别的子命令
        idx_usage = CLI_ARGS.index("usage")
        idx_json = CLI_ARGS.index("--json")
        assert idx_usage < idx_json


class TestTimeoutConstant:
    def test_timeout_is_60(self):
        # ARCH §1 锁 60s
        assert TIMEOUT_SECONDS == 60


# ===========================================================================
# 工具：_truncate_iso_to_microseconds
# ===========================================================================

class TestTruncateIso:
    def test_nanoseconds_truncated_to_microseconds(self):
        # CodexBar 实际纳秒精度（9 位小数）→ 截到 6 位（微秒）
        s = "2026-09-11T07:20:40.373888600Z"
        out = _truncate_iso_to_microseconds(s)
        assert out == "2026-09-11T07:20:40.373888Z"

    def test_microseconds_unchanged(self):
        s = "2026-09-11T07:20:40.373000Z"
        assert _truncate_iso_to_microseconds(s) == s

    def test_no_fraction_unchanged(self):
        s = "2026-09-11T07:20:40Z"
        assert _truncate_iso_to_microseconds(s) == s

    def test_offset_form_also_handled(self):
        # 含 +00:00 而非 Z；截断必须同样生效
        s = "2026-09-11T07:20:40.123456789+08:00"
        out = _truncate_iso_to_microseconds(s)
        assert out == "2026-09-11T07:20:40.123456+08:00"

    def test_only_first_dot_replaced(self):
        s = "2026-09-11.12345.999Z"
        out = _truncate_iso_to_microseconds(s)
        # 只截第一个 .dddd 处（time 部分）；后面的 .999 在 reset 字段外不应处理
        assert out == "2026-09-11.12345.999Z"

    def test_three_digit_unchanged(self):
        # 3 位毫秒精度：保留
        s = "2026-09-11T07:20:40.373Z"
        assert _truncate_iso_to_microseconds(s) == s


# ===========================================================================
# Happy path：用 fixture（脱敏）解析
# ===========================================================================

class TestFixtureHappyPath:
    """用 ``tests/fixtures/codex.json`` 替换子进程输出，断言 §1 映射。"""

    def test_parses_fixture(self, monkeypatch):
        fixture = json.loads((FIXTURES / "codex.json").read_text(encoding="utf-8"))
        fixture_str = json.dumps(fixture)

        with monkeypatch.context() as m:
            # 重定向 EXE_PATH 到假路径 + monkeypatch subprocess.run
            m.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)

            def fake_run(cmd, **kwargs):
                # 验证命令参数：必须是 [FAKE_EXE, "usage", "-p", "codex", "--json"]
                assert cmd == [FAKE_EXE, "usage", "-p", "codex", "--json"]
                # 关键参数校验
                assert kwargs.get("capture_output") is True
                assert kwargs.get("text") is True
                assert kwargs.get("encoding") == "utf-8"
                assert kwargs.get("timeout") == TIMEOUT_SECONDS
                cp = subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=fixture_str, stderr="",
                )
                return cp

            m.setattr("aegis.providers.codex.subprocess.run", fake_run)
            s = read()

        # 基础字段
        assert s.provider == "codex"
        assert s.status == "ok"
        assert s.source == "codexbar-cli"
        assert s.reliability == "live"
        assert s.error is None

        # five_hour 字段
        assert s.five_hour is not None
        assert s.five_hour.used_percent == 66.0    # fixture 实际值
        assert s.five_hour.remaining_percent == 34.0
        assert s.five_hour.resets_at == "2026-09-15T10:15:00Z"
        assert s.five_hour.window_minutes == 300

        # weekly 字段
        assert s.weekly is not None
        assert s.weekly.used_percent == 44.0
        assert s.weekly.remaining_percent == 56.0
        assert s.weekly.resets_at == "2026-09-19T07:42:00Z"
        assert s.weekly.window_minutes == 10080

        # updated_at：fixture 含 6 位小数（微秒），保持原值
        assert s.updated_at == "2026-09-15T08:00:00.123456Z"

        # extras：必须含 codex-gpt-reserve 与 reset-credits
        extra_ids = {e.id for e in s.extras}
        assert "codex-gpt-reserve" in extra_ids
        assert "reset-credits" in extra_ids

    def test_extras_passthrough_fields(self, monkeypatch):
        fixture = json.loads((FIXTURES / "codex.json").read_text(encoding="utf-8"))
        fixture_str = json.dumps(fixture)

        with monkeypatch.context() as m:
            m.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
            m.setattr(
                "aegis.providers.codex.subprocess.run",
                lambda cmd, **kwargs: subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=fixture_str, stderr="",
                ),
            )
            s = read()

        by_id = {e.id: e for e in s.extras}
        # codex-gpt-reserve：window_minutes=10080, used_percent=0.0
        gpt = by_id["codex-gpt-reserve"]
        assert gpt.title == "Gpt Reserve"
        assert gpt.used_percent == 0.0
        assert gpt.is_informational is False
        assert gpt.reset_description == "6d 23h"
        assert gpt.window_minutes == 10080
        assert gpt.resets_at == "2026-09-22T08:00:00Z"
        # reset-credits：is_informational=True, used_percent=0.0
        rc = by_id["reset-credits"]
        assert rc.title == "Reset credits"
        assert rc.used_percent == 0.0
        assert rc.is_informational is True
        assert rc.window_minutes is None      # fixture 中无此字段
        assert rc.reset_description == "1 reset credit available"
        assert rc.resets_at == "2026-10-08T03:30:00.250000Z"

    def test_pace_field_ignored(self, monkeypatch):
        """ARCH §1 + README：``pace`` 字段不解析、不报错。"""
        fixture = [{
            "provider": "codex",
            "source": "oauth",
            "pace": {"primary": {"deltaPercent": -99, "stage": "ahead"}},
            "usage": {
                "primary": {"used_percent": 10.0, "resets_at": "2026-09-11T08:00:00Z",
                            "window_minutes": 300},
                "secondary": {"used_percent": 20.0, "resets_at": "2026-09-15T08:00:00Z",
                              "window_minutes": 10080},
                "updated_at": "2026-09-11T07:00:00Z",
            },
        }]
        with monkeypatch.context() as m:
            m.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
            m.setattr(
                "aegis.providers.codex.subprocess.run",
                lambda cmd, **kwargs: subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=json.dumps(fixture), stderr="",
                ),
            )
            s = read()
        # pace 在顶层，但 snapshot 不应有 pace 字段；解析必须成功
        assert s.status == "ok"
        assert s.five_hour.used_percent == 10.0


# ===========================================================================
# 纳秒精度 updated_at（关键：避免纳秒解析问题）
# ===========================================================================

class TestNanosecondTruncation:
    """真实 CodexBar ``updated_at`` 含 9 位小数 → 必须截断到微秒。"""

    def test_nanosecond_updated_at_truncated(self, monkeypatch):
        payload = [{
            "provider": "codex",
            "source": "oauth",
            "usage": {
                "primary": {"used_percent": 10.0,
                            "resets_at": "2026-09-11T08:00:00Z",
                            "window_minutes": 300},
                "secondary": {"used_percent": 20.0,
                              "resets_at": "2026-09-15T08:00:00Z",
                              "window_minutes": 10080},
                "updated_at": "2026-09-11T07:30:00.123456789Z",  # 9 位纳秒
            },
        }]
        with monkeypatch.context() as m:
            m.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
            m.setattr(
                "aegis.providers.codex.subprocess.run",
                lambda cmd, **kwargs: subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=json.dumps(payload), stderr="",
                ),
            )
            s = read()
        assert s.status == "ok"
        # 必须截断到 6 位（微秒）；原值 .123456789 → .123456
        assert s.updated_at == "2026-09-11T07:30:00.123456Z"

    def test_microsecond_resets_at_in_extras_truncated(self, monkeypatch):
        """extras 里的 resets_at 也走同一条截断路径。"""
        payload = [{
            "provider": "codex",
            "source": "oauth",
            "usage": {
                "primary": {"used_percent": 0.0,
                            "resets_at": "2026-09-11T08:00:00Z",
                            "window_minutes": 300},
                "secondary": {"used_percent": 0.0,
                              "resets_at": "2026-09-15T08:00:00Z",
                              "window_minutes": 10080},
                "extra_rate_windows": [{
                    "id": "codex-gpt-reserve",
                    "title": "Gpt Reserve",
                    "window": {
                        "is_informational": False,
                        "resets_at": "2026-09-22T10:00:00.987654321Z",  # 纳秒
                        "used_percent": 0.0,
                        "window_minutes": 10080,
                    },
                }],
                "updated_at": "2026-09-11T07:00:00Z",
            },
        }]
        with monkeypatch.context() as m:
            m.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
            m.setattr(
                "aegis.providers.codex.subprocess.run",
                lambda cmd, **kwargs: subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=json.dumps(payload), stderr="",
                ),
            )
            s = read()
        gpt = next(e for e in s.extras if e.id == "codex-gpt-reserve")
        assert gpt.resets_at == "2026-09-22T10:00:00.987654Z"


# ===========================================================================
# 子进程错误分类
# ===========================================================================

class TestSubprocessErrors:
    def _patched_with_error(self, monkeypatch, exc):
        """子进程抛异常的版本（FileNotFoundError / Timeout / OSError）。"""
        monkeypatch.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
        monkeypatch.setattr(
            "aegis.providers.codex.subprocess.run",
            lambda cmd, **kwargs: (_ for _ in ()).throw(exc),
        )

    def _patched_with_result(self, monkeypatch, *, returncode=0, stdout="", stderr=""):
        """子进程返回 CompletedProcess 的版本。"""
        monkeypatch.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
        monkeypatch.setattr(
            "aegis.providers.codex.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                args=cmd, returncode=returncode, stdout=stdout, stderr=stderr,
            ),
        )

    def test_file_not_found(self, monkeypatch):
        # 直接抛 FileNotFoundError：EXE 不存在 → CodexSourceUnavailable 路径
        # 但 read() 返回 error snapshot，不抛
        self._patched_with_error(
            monkeypatch,
            FileNotFoundError(2, "No such file", FAKE_EXE),
        )
        s = read()
        assert s.status == "error"
        assert s.error.type == "codex_source_unavailable"
        assert FAKE_EXE in s.error.message
        assert s.five_hour is None
        assert s.weekly is None
        assert s.extras == []

    def test_timeout(self, monkeypatch):
        self._patched_with_error(
            monkeypatch,
            subprocess.TimeoutExpired(cmd=[FAKE_EXE], timeout=60),
        )
        s = read()
        assert s.status == "error"
        assert s.error.type == "codex_source_unavailable"
        assert "timed out" in s.error.message
        assert "60" in s.error.message

    def test_oserror_on_launch(self, monkeypatch):
        self._patched_with_error(monkeypatch, OSError("permission denied"))
        s = read()
        assert s.status == "error"
        assert s.error.type == "codex_source_unavailable"
        assert "OSError" in s.error.message or "permission" in s.error.message

    def test_nonzero_exit_with_stderr(self, monkeypatch):
        self._patched_with_result(
            monkeypatch, returncode=2, stderr="fatal: auth file missing",
        )
        s = read()
        assert s.status == "error"
        assert s.error.type == "codex_source_unavailable"
        # 消息含 rc 与 stderr
        assert "rc=2" in s.error.message
        assert "auth file missing" in s.error.message

    def test_nonzero_exit_stderr_truncated_to_200(self, monkeypatch):
        # 用不会出现在错误消息其它部分（含 codexbar-cli）的字符'A'计数，
        # 避免误判。
        long_stderr = "A" * 500
        self._patched_with_result(monkeypatch, returncode=1, stderr=long_stderr)
        s = read()
        assert s.status == "error"
        # 截断到 200 字符：stderr 副本中 'A' 个数 ≤ 200（实际等于 200）。
        # 整条错误消息中 'A' 个数也只可能 == 200（除 stderr 副本外无 'A'）。
        assert s.error.message.count("A") == 200

    def test_nonzero_exit_no_stderr(self, monkeypatch):
        self._patched_with_result(monkeypatch, returncode=1, stderr="")
        s = read()
        assert s.status == "error"
        assert s.error.type == "codex_source_unavailable"
        assert "rc=1" in s.error.message


# ===========================================================================
# ParseError：空数组 / 非 JSON
# ===========================================================================

class TestParseErrors:
    def _patched(self, monkeypatch, stdout):
        monkeypatch.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
        monkeypatch.setattr(
            "aegis.providers.codex.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            ),
        )

    def test_non_json_stdout(self, monkeypatch):
        self._patched(monkeypatch, stdout="not-json-at-all")
        s = read()
        assert s.status == "error"
        assert s.error.type == "parse_error"

    def test_empty_stdout(self, monkeypatch):
        self._patched(monkeypatch, stdout="")
        s = read()
        assert s.status == "error"
        assert s.error.type == "parse_error"

    def test_non_array_stdout(self, monkeypatch):
        self._patched(monkeypatch, stdout='{"not": "an array"}')
        s = read()
        assert s.status == "error"
        assert s.error.type == "parse_error"
        assert "array" in s.error.message

    def test_empty_array(self, monkeypatch):
        self._patched(monkeypatch, stdout="[]")
        s = read()
        assert s.status == "error"
        assert s.error.type == "parse_error"
        assert "empty" in s.error.message

    def test_array_with_non_object(self, monkeypatch):
        self._patched(monkeypatch, stdout='[1, 2, 3]')
        s = read()
        assert s.status == "error"
        assert s.error.type == "parse_error"

    def test_array_with_null_first(self, monkeypatch):
        self._patched(monkeypatch, stdout="[null]")
        s = read()
        assert s.status == "error"
        assert s.error.type == "parse_error"


# ===========================================================================
# 字段缺失 / 异常结构
# ===========================================================================

class TestFieldMissing:
    def _patched(self, monkeypatch, payload):
        monkeypatch.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
        monkeypatch.setattr(
            "aegis.providers.codex.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps(payload), stderr="",
            ),
        )

    def test_missing_usage(self, monkeypatch):
        self._patched(monkeypatch, payload=[{"provider": "codex", "source": "oauth"}])
        s = read()
        assert s.status == "error"
        assert s.error.type == "unexpected_shape"
        assert "usage" in s.error.message

    def test_missing_primary(self, monkeypatch):
        self._patched(monkeypatch, payload=[{
            "provider": "codex",
            "usage": {
                "secondary": {"used_percent": 10, "resets_at": "2026-09-15T08:00:00Z",
                              "window_minutes": 10080},
                "updated_at": "2026-09-11T07:00:00Z",
            },
        }])
        s = read()
        assert s.status == "error"
        assert "primary" in s.error.message

    def test_missing_used_percent(self, monkeypatch):
        self._patched(monkeypatch, payload=[{
            "provider": "codex",
            "usage": {
                "primary": {"resets_at": "2026-09-11T08:00:00Z", "window_minutes": 300},
                "secondary": {"used_percent": 10, "resets_at": "2026-09-15T08:00:00Z",
                              "window_minutes": 10080},
                "updated_at": "2026-09-11T07:00:00Z",
            },
        }])
        s = read()
        assert s.status == "error"

    def test_used_percent_out_of_range(self, monkeypatch):
        self._patched(monkeypatch, payload=[{
            "provider": "codex",
            "usage": {
                "primary": {"used_percent": 150, "resets_at": "2026-09-11T08:00:00Z",
                            "window_minutes": 300},
                "secondary": {"used_percent": 10, "resets_at": "2026-09-15T08:00:00Z",
                              "window_minutes": 10080},
                "updated_at": "2026-09-11T07:00:00Z",
            },
        }])
        s = read()
        assert s.status == "error"


# ===========================================================================
# NO_SECRET_LEAK
# ===========================================================================

class TestNoSecretLeak:
    """所有错误快照的 to_dict() 与 error.message 不得出现假 key 子串。"""

    def _assert_clean(self, snap):
        blob = json.dumps(snap.to_dict(), default=str)
        for s in LEAK_NEVER:
            assert s not in blob, f"key leaked: {s!r} in {blob!r}"
        if snap.error:
            for s in LEAK_NEVER:
                assert s not in snap.error.message

    def test_parse_error_path_clean(self, monkeypatch):
        # 把假 key 塞进 stderr / stdout，看是否被脱敏或忽略
        monkeypatch.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
        monkeypatch.setattr(
            "aegis.providers.codex.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="not-json " + FAKE_KEY,
                stderr=FAKE_KEY,
            ),
        )
        s = read()
        self._assert_clean(s)

    def test_nonzero_exit_with_key_in_stderr_clean(self, monkeypatch):
        monkeypatch.setattr("aegis.providers.codex.EXE_PATH", FAKE_EXE)
        monkeypatch.setattr(
            "aegis.providers.codex.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="",
                stderr=f"err: {FAKE_KEY} something leaked",
            ),
        )
        s = read()
        self._assert_clean(s)


# ===========================================================================
# 真机调用（标 @pytest.mark.live，默认跳过；Hermes 验收时执行）
# ===========================================================================

class TestLiveCodex:
    """真机：调用 ``codexbar-cli usage -p codex --json``。

    默认跳过（CI / 单元测试场景）。在已安装 CodexBar 的机器上
    ``pytest -m live`` 跑通即可；脱敏输出见 P3 结束报告。
    """

    @pytest.mark.live
    @pytest.mark.skipif(
        not Path(EXE_PATH).exists(),
        reason=f"codexbar-cli not found at {EXE_PATH}",
    )
    def test_real_invoke(self, capsys):
        s = read()
        # 真机至少有四个字段非空（CodexBar 真实返回至少有 primary + secondary + 1 extra）
        assert s.provider == "codex"
        assert s.source == "codexbar-cli"
        if s.status == "ok":
            assert s.five_hour is not None
            assert s.weekly is not None
            assert s.five_hour.used_percent is not None
            assert s.five_hour.resets_at.endswith("Z")
            assert s.five_hour.window_minutes == 300
            assert s.weekly.window_minutes == 10080
            assert s.updated_at is not None
            assert any(e.id == "codex-gpt-reserve" for e in s.extras)
            assert any(e.id == "reset-credits" for e in s.extras)
        else:
            # 若 CodexBar 不可用，错误信息必须不含任何 key
            cap = capsys.readouterr()
            for leak in LEAK_NEVER:
                assert leak not in cap.out
                assert leak not in cap.err
            if s.error:
                for leak in LEAK_NEVER:
                    assert leak not in s.error.message