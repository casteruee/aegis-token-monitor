"""``run.py`` CLI 行为测试（P4 任务 + ARCH §11）。

约束（P4）：
- ``python -m pytest tests -q`` 必须**全绿**；本测试是其中之一。
- 离线运行：不依赖真实网络 / CodexBar 子进程 / 凭据。``READERS`` 通过
  ``monkeypatch.setattr`` 整体替换为假 reader。
- ``time.sleep`` 永不调用（``--watch`` 轮转通过冻结时间 + 计数器模拟）。

覆盖矩阵：

1. **版本断言**（M-4）：Python < 3.11 时 ``main()`` 返回 2。
2. **``--json`` 是纯 JSON**：
    * stdout 唯一内容是 JSON 字符串（不混诊断文本）；
    * ``json.loads(stdout)`` 解析成功；
    * 结构与 ARCH §7.1 一致（含 ``generated_at`` + ``providers[]``）。
3. **单 provider 失败不崩溃**：
    * monkeypatch 注入 1 个失败 + 3 个成功的假 reader；
    * 退出码仍为 0；
    * 失败 provider 显示 ``status='error'``，其余三个 ``status='ok'``。
4. **``--watch`` 的 due time 逻辑**：
    * 冻结当前时间，验证 fresh → cached（``reliability='cached'``）；
    * 过期 → 重抓（``reliability='live'``）；
    * 失败 + 有缓存 → stale；
    * 失败 + 无缓存 → error。
5. **NO_SECRET_LEAK**：所有 reader 返回值与异常文本不含 ``sk-...`` /
   ``Bearer ...`` 模式（基于 fake reader 的占位文本）。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pytest

import run as run_mod
from aegis import cache as cache_mod
from aegis.model import (
    Balance,
    ErrorInfo,
    Report,
    Snapshot,
    Window,
)


# 项目根目录 = tests/ 的父目录（子进程测试需要传 cwd）
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------------------
# 公共 fixtures
# ---------------------------------------------------------------------------

def _make_snap(
    provider: str,
    *,
    status: str = "ok",
    used_5h: Optional[float] = 50.0,
    used_wk: Optional[float] = 30.0,
    balance_value: Optional[float] = None,
    balance_currency: str = "CNY",
    updated: Optional[str] = "2026-09-11T06:30:00Z",
    error_type: Optional[str] = None,
) -> Snapshot:
    """构造一个简单 snapshot（用于 fake reader）。"""
    if status == "ok":
        kwargs = dict(
            status="ok",
            source=f"{provider}-src",
            updated_at=updated,
            reliability="live",
        )
        if used_5h is not None:
            kwargs["five_hour"] = Window(
                used_percent=used_5h,
                remaining_percent=round(100 - used_5h, 2),
                resets_at="2026-09-11T07:51:57Z",
                window_minutes=300,
            )
        if used_wk is not None:
            kwargs["weekly"] = Window(
                used_percent=used_wk,
                remaining_percent=round(100 - used_wk, 2),
                resets_at="2026-09-15T05:37:00Z",
                window_minutes=10080,
            )
        if balance_value is not None:
            kwargs["balance"] = Balance(value=balance_value, currency=balance_currency)
        return Snapshot(provider=provider, **kwargs)

    # status == 'error'
    return Snapshot(
        provider=provider,
        status="error",
        source=f"{provider}-src",
        updated_at=updated,
        reliability="live",
        error=ErrorInfo(
            type=error_type or "network_error",
            message="simulated failure (no real key here)",
        ),
    )


@pytest.fixture
def fake_readers(monkeypatch):
    """把 READERS 替换为四个假 reader；返回字典便于测试单独修改。"""

    def _read_codex(): return _make_snap("codex", used_5h=55.0, used_wk=53.0)
    def _read_zai(): return _make_snap("zai", used_5h=1.0, used_wk=95.0)
    def _read_minimax(): return _make_snap("minimax", used_5h=7.0, used_wk=15.0)
    def _read_deepseek(): return _make_snap(
        "deepseek", used_5h=None, used_wk=None,
        balance_value=74.16, balance_currency="CNY",
        updated="2026-09-11T06:30:04Z",
    )

    fake = {
        "codex": _read_codex,
        "zai": _read_zai,
        "minimax": _read_minimax,
        "deepseek": _read_deepseek,
    }
    monkeypatch.setattr(run_mod, "READERS", fake)
    return fake


@pytest.fixture
def fake_cache_dir(tmp_path, monkeypatch):
    """把缓存文件重定向到 tmp_path。"""
    new_file = tmp_path / "cache" / "usage_latest.json"
    monkeypatch.setattr(cache_mod, "CACHE_FILE", new_file)
    monkeypatch.setattr(cache_mod, "CACHE_DIR", new_file.parent)
    return new_file


def _captured_run(argv: List[str], *, monkeypatch=None) -> tuple[str, str, int]:
    """运行 ``run.main(argv)``，捕获 stdout/stderr；返回 (stdout, stderr, rc)。

    用 ``main()`` 而不是 ``run.py`` 子进程 → 跳过 Python 版本断言的 sys.exit
    （该断言本身另有专门测试覆盖）。
    """
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    rc = -1
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = run_mod.main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    return buf_out.getvalue(), buf_err.getvalue(), rc


# ===========================================================================
# 1. Python 版本断言（M-4）
# ===========================================================================

class TestPythonVersion:

    def test_assert_python_version_passes_on_current(self):
        # 当前测试运行在 3.11+（否则连测试都跑不起来）
        # 直接调用不应抛
        run_mod._assert_python_version()

    def test_main_exits_2_on_old_python(self, monkeypatch):
        """强制 sys.version_info 为 (3, 9) → main() 应返回 2。"""
        # 替换内部的 sys 引用（run.py 顶部 import 的 sys 模块对象）
        fake_version_info = type("V", (), {
            "major": 3, "minor": 9, "micro": 0,
            "releaselevel": "final", "serial": 0,
        })()
        fake_sys = type("S", (), {"version_info": fake_version_info})()
        # 把 _REQUIRED_PY 提高以模拟低版本 → 避免动 sys.version_info 影响整
        # 个 pytest 进程（更安全的反向测试）。
        monkeypatch.setattr(run_mod, "_REQUIRED_PY", (3, 99))
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            with pytest.raises(SystemExit) as exc:
                run_mod._assert_python_version()
        assert exc.value.code == 2
        # 错误消息含「Python 版本过低」或类似中文
        err = buf_err.getvalue()
        assert "Python" in err and "3.99" in err


# ===========================================================================
# 2. --json 是纯 JSON
# ===========================================================================

class TestJsonMode:

    def test_json_output_is_pure_json(self, fake_readers):
        out, err, rc = _captured_run(["--json"])
        assert rc == 0
        # stdout 必须是 JSON 字符串（去掉末尾换行后 parse）
        stripped = out.strip()
        parsed = json.loads(stripped)  # 必须解析成功
        assert "providers" in parsed
        assert "generated_at" in parsed
        assert len(parsed["providers"]) == 4

    def test_json_stdout_has_no_diagnostics(self, fake_readers):
        """所有诊断信息走 stderr；stdout 只有 JSON。"""
        out, err, rc = _captured_run(["--json"])
        assert rc == 0
        # stdout 解析后是合法 JSON → 若夹带诊断文本（如 "[aegis]"），解析会爆
        # 这里再确保 stdout 里没有 ``[aegis]`` 字样
        assert "[aegis]" not in out

    def test_json_has_required_fields(self, fake_readers):
        out, _, _ = _captured_run(["--json"])
        data = json.loads(out)
        for p in data["providers"]:
            assert "provider" in p
            assert "status" in p
            assert "five_hour" in p
            assert "weekly" in p
            assert "balance" in p
            assert "extras" in p
            assert "updated_at" in p
            assert "source" in p
            assert "reliability" in p
            assert "error" in p


# ===========================================================================
# 3. 单 provider 失败不崩溃
# ===========================================================================

class TestProviderFailure:

    def test_one_failure_does_not_block_others(self, fake_readers, monkeypatch):
        # 把 zai 换成失败 reader
        def _fail_zai():
            return _make_snap("zai", status="error", error_type="network_error")
        fake_readers["zai"] = _fail_zai

        out, err, rc = _captured_run(["--json"])
        assert rc == 0  # 退出码仍为 0
        data = json.loads(out)
        statuses = {p["provider"]: p["status"] for p in data["providers"]}
        assert statuses["codex"] == "ok"
        assert statuses["zai"] == "error"
        assert statuses["minimax"] == "ok"
        assert statuses["deepseek"] == "ok"

    def test_all_failures_still_return_zero(self, fake_readers):
        def _fail():
            return _make_snap("x", status="error", error_type="network_error")
        for name in fake_readers:
            fake_readers[name] = _fail

        out, err, rc = _captured_run(["--json"])
        assert rc == 0
        data = json.loads(out)
        for p in data["providers"]:
            assert p["status"] == "error"
            # error 态的数据列全 null
            assert p["five_hour"] is None
            assert p["weekly"] is None
            assert p["balance"] is None


# ===========================================================================
# 4. --watch 的 due time 逻辑（per-provider TTL）
# ===========================================================================

class TestWatchDueTime:
    """``--watch`` 一轮的内部逻辑由 :func:`run._watch_round` 暴露。
    通过冻结 now + 注入假 reader 验证 TTL 命中 / 过期 / 失败回退。
    """

    def test_fresh_cache_returns_cached_reliability(self, fake_cache_dir, fake_readers):
        # 预热缓存：把 codex 写入（5 分钟前）
        earlier = datetime(2026, 9, 11, 6, 30, 0, tzinfo=timezone.utc)
        cache_mod.write_provider(
            "codex",
            _make_snap("codex", used_5h=42.0, updated="2026-09-11T06:00:00Z"),
            now=earlier,
        )
        # now = earlier + 60s（远未到 300s TTL）
        now = earlier + timedelta(seconds=60)
        snaps = run_mod._watch_round(now)
        codex = next(s for s in snaps if s.provider == "codex")
        assert codex.reliability == "cached"
        assert codex.status == "ok"
        # 数据来自缓存（used=42.0，不是 fake reader 的 55.0）
        assert codex.five_hour.used_percent == 42.0

    def test_expired_cache_triggers_refetch(self, fake_cache_dir, fake_readers):
        # 缓存写入时间是「很久以前」
        old = datetime(2026, 9, 11, 6, 0, 0, tzinfo=timezone.utc)
        cache_mod.write_provider(
            "codex",
            _make_snap("codex", used_5h=42.0, updated="2026-09-11T05:30:00Z"),
            now=old,
        )
        # now 远在 TTL 之外
        now = datetime(2026, 9, 11, 7, 0, 0, tzinfo=timezone.utc)
        snaps = run_mod._watch_round(now)
        codex = next(s for s in snaps if s.provider == "codex")
        # 过期 → 重抓 → 成功 → reliability='live'
        assert codex.reliability == "live"
        # 数据来自 fake reader（used=55.0）
        assert codex.five_hour.used_percent == 55.0

    def test_failure_with_cache_returns_stale(self, fake_cache_dir, fake_readers):
        # 写入缓存（10 分钟前）
        earlier = datetime(2026, 9, 11, 6, 30, 0, tzinfo=timezone.utc)
        cache_mod.write_provider(
            "codex",
            _make_snap("codex", used_5h=42.0, updated="2026-09-11T06:30:00Z"),
            now=earlier,
        )
        # 让 codex 的 fake reader 失败
        def _fail():
            return _make_snap("codex", status="error", error_type="network_error")
        fake_readers["codex"] = _fail
        # now 远在 TTL 之外 → 触发重抓 → 失败 → 回退缓存
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        snaps = run_mod._watch_round(now)
        codex = next(s for s in snaps if s.provider == "codex")
        # 三个核心断言（ARCH §8）
        assert codex.status == "error"
        assert codex.reliability == "stale"
        assert codex.updated_at == "2026-09-11T06:30:00Z"  # 缓存原值，不被改写

    def test_failure_without_cache_returns_error(self, fake_cache_dir, fake_readers):
        # 无缓存
        def _fail():
            return _make_snap("codex", status="error", error_type="network_error")
        fake_readers["codex"] = _fail
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        snaps = run_mod._watch_round(now)
        codex = next(s for s in snaps if s.provider == "codex")
        assert codex.status == "error"
        # reliability 保留 reader 默认 'live'（无缓存时 cache 层不覆盖）
        assert codex.five_hour is None
        assert codex.weekly is None


# ===========================================================================
# 5. NO_SECRET_LEAK（fake reader 不会泄漏；这里主要保护正常路径）
# ===========================================================================

class TestNoSecretLeak:

    def test_json_output_has_no_sk_or_bearer(self, fake_readers):
        out, _, _ = _captured_run(["--json"])
        # fake reader 的 error.message 含 "simulated failure (no real key here)"
        # 完全没有 sk- / Bearer
        assert "sk-" not in out
        assert "Bearer " not in out

    def test_panel_output_has_no_sk_or_bearer(self, fake_readers):
        out, _, _ = _captured_run([])
        assert "sk-" not in out
        assert "Bearer " not in out


# ===========================================================================
# 6. 默认 / --watch / --json 三个互斥模式（argparse）
# ===========================================================================

class TestArgparse:

    def test_default_mode_does_not_require_arg(self, fake_readers):
        """无参数 → 默认模式（一次性 + 面板），不应报错。"""
        out, err, rc = _captured_run([])
        assert rc == 0
        # stdout 应含面板特征
        assert "Aegis Token Monitor" in out

    def test_json_flag(self, fake_readers):
        out, _, rc = _captured_run(["--json"])
        assert rc == 0
        json.loads(out)  # 解析成功

    def test_watch_flag_default_interval(self, fake_readers, monkeypatch):
        """``--watch`` 不带 interval → 默认 300s；不实际睡眠，用 monkeypatch
        替换 ``time.sleep`` 抛 KeyboardInterrupt 提前退出循环。"""
        import time as time_mod
        monkeypatch.setattr(run_mod.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
        out, err, rc = _captured_run(["--watch"])
        assert rc == 0
        # 至少渲染过一帧面板
        assert "Aegis Token Monitor" in out

    def test_json_and_watch_are_mutually_exclusive(self, fake_readers):
        """``--json`` 与 ``--watch`` 不能同时使用（argparse 应拒绝）。"""
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            with pytest.raises(SystemExit):
                run_mod.main(["--json", "--watch"])


# ===========================================================================
# 7. 集成测试：run.py 子进程（保留 Python 版本断言的真实性）
# ===========================================================================

class TestSubprocessRun:

    @pytest.mark.live
    def test_run_json_subprocess(self):
        """通过真子进程跑 ``python run.py --json``，验证 stdout 是 JSON。

        标 ``@pytest.mark.live``：依赖真机凭据 + 网络。无环境时 pytest 自身
        跳过。
        """
        proc = subprocess.run(
            [sys.executable, "run.py", "--json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        # 进程退出码
        assert proc.returncode == 0, proc.stderr
        # stdout 可解析
        parsed = json.loads(proc.stdout)
        assert "providers" in parsed
        assert len(parsed["providers"]) == 4

    @pytest.mark.live
    def test_run_default_subprocess(self):
        proc = subprocess.run(
            [sys.executable, "run.py"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert proc.returncode == 0
        assert "Aegis Token Monitor" in proc.stdout
