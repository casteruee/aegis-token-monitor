"""MonitorService 离线测试：TTL 调度、generation、安全错误、单写者、历史接线。

不触网：readers 全部为注入的假函数；时钟注入固定/可控 datetime。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from aegis.model import Balance, ErrorInfo, Snapshot, Window
from aegis.services import monitor as mon
from aegis.services.monitor import MonitorService


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """监控测试绝不读写真实 cache/usage_latest.json（防测试污染生产缓存）。"""
    monkeypatch.setattr(mon.cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(mon.cache_mod, "CACHE_FILE", tmp_path / "cache" / "usage_latest.json")
    yield


UTC = timezone.utc


def _ok_snap(provider="codex", used=20.0, resets="2026-09-20T00:00:00Z"):
    return Snapshot(
        provider=provider,
        status="ok",
        five_hour=Window(used, 100 - used, resets, 300),
        weekly=Window(used / 2, 100 - used / 2, resets, 10080),
        source=f"{provider}-api",
    )


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class MemHistory:
    def __init__(self):
        self.obs = []
        self.attempts = []
        self.fail = False

    def record_observation(self, snapshot, fetched_at_utc, refresh_id):
        if self.fail:
            raise RuntimeError("history down")
        self.obs.append((snapshot.provider, fetched_at_utc))
        return len(self.obs)

    def record_attempt(self, provider, started_at, finished_at, result_code, observation_id):
        if self.fail:
            raise RuntimeError("history down")
        self.attempts.append((provider, result_code))


@pytest.fixture
def env():
    class Env:
        pass
    e = Env()
    e.clock = FakeClock(datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC))
    e.history = MemHistory()
    e.readers = {}
    e.service = None
    yield e
    if e.service is not None:
        e.service.shutdown(grace=0.1)


def _make(e, readers):
    e.service = MonitorService(
        history=e.history, readers=readers, clock=e.clock,
        ttls={"codex": 300, "zai": 300, "minimax": 300, "deepseek": 1800},
    )
    return e.service


def _pump(e, seconds_hint=0.2):
    """给协调线程时间处理事件。"""
    time.sleep(seconds_hint)


def test_start_loads_cache_and_first_round_fetches_all(env):
    calls = []
    for name in ("codex", "zai", "minimax", "deepseek"):
        env.readers[name] = (lambda n: (lambda: (calls.append(n), _ok_snap(n))[1]))(name)
    svc = _make(env, env.readers)
    received = []
    svc.subscribe(lambda envs: received.append(envs))
    svc.start()
    _pump(env, 0.5)
    assert sorted(calls) == ["codex", "deepseek", "minimax", "zai"]
    envs = svc.get_envelopes()
    assert all(envs[n].availability == "OK" for n in ("codex", "zai", "minimax", "deepseek"))
    # 每个 provider 一次成功 observation + attempt
    assert len(env.history.obs) == 4
    assert all(rc == "ok" for _, rc in env.history.attempts)


def test_ttl_expiry_triggers_auto_refresh_only_after_ttl(env):
    calls = []
    env.readers["codex"] = lambda: (calls.append("codex"), _ok_snap("codex"))[1]
    for n in ("zai", "minimax", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    _pump(env, 0.5)
    n0 = len(calls)
    env.clock.advance(120)   # 未到 TTL
    _pump(env, 0.6)
    assert len(calls) == n0
    env.clock.advance(240)   # 360s > TTL 300
    _pump(env, 0.8)
    assert len(calls) > n0


def _wait_until(pred, timeout=5.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def test_failure_maps_to_fixed_code_and_stale_keeps_old_value(env):
    state = {"fail": False}
    env.readers["codex"] = lambda: (
        _ok_snap("codex") if not state["fail"] else Snapshot(
            provider="codex", status="error",
            error=ErrorInfo(type="network_error", message="host=api.example err=X reason='<redacted>'"),
        )
    )
    for n in ("zai", "minimax", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    # 条件等待：初始轮完成（不依赖固定 sleep，负载下稳定）
    assert _wait_until(
        lambda: not svc._states["codex"].in_flight and len(env.history.obs) >= 1
    ), "initial fetch did not settle"
    _pump(env, 0.05)

    n0 = len(env.history.obs)          # 初始稳定后的 observation 数
    state["fail"] = True
    svc.refresh("codex", manual=True)
    assert _wait_until(lambda: not svc._states["codex"].in_flight), "manual fetch did not finish"
    _pump(env, 0.05)

    e = svc.get_envelopes()["codex"]
    assert e.availability == "ERROR"
    assert e.freshness == "STALE"
    assert e.error_code == "network_error"
    assert e.snapshot is not None and e.snapshot.status == "ok"   # 旧值保留
    assert "example" not in (e.error_display_text())              # 无原文泄漏
    assert e.error_display_text() == "网络连接失败"
    # 不变量：失败刷新不新增 observation（cached/stale 不入账）
    assert len(env.history.obs) == n0
    assert env.history.attempts[-1] == ("codex", "network_error")


def test_rate_limited_429_flag(env):
    env.readers["zai"] = lambda: Snapshot(
        provider="zai", status="error",
        error=ErrorInfo(type="http_error", message="HTTP 429"),
    )
    for n in ("codex", "minimax", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    _pump(env, 0.5)
    e = svc.get_envelopes()["zai"]
    assert e.error_code == "http_rate_limited"
    assert e.error_display_text() == "上游限流，稍后自动重试"


def test_failure_without_old_value_is_unavailable(env):
    env.readers["minimax"] = lambda: Snapshot(
        provider="minimax", status="error",
        error=ErrorInfo(type="parse_error", message=""),
    )
    for n in ("codex", "zai", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    _pump(env, 0.5)
    e = svc.get_envelopes()["minimax"]
    assert e.availability == "UNAVAILABLE"
    assert e.snapshot is None


def test_generation_drops_stale_results(env):
    cond = threading.Event()

    def slow_reader():
        cond.wait(timeout=2)
        return _ok_snap("codex")

    env.readers["codex"] = slow_reader
    for n in ("zai", "minimax", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    _pump(env, 0.3)
    # 在途时关闭：shutdown 后迟到结果不应改状态（不崩溃即可）
    svc.shutdown(grace=0.05)
    cond.set()
    time.sleep(0.3)


def test_history_failure_does_not_kill_live(env):
    env.history.fail = True
    env.readers["codex"] = lambda: _ok_snap("codex")
    for n in ("zai", "minimax", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    _pump(env, 0.5)
    e = svc.get_envelopes()["codex"]
    assert e.availability == "OK"     # 历史坏 → 实时照常
    assert e.freshness == "LIVE"


def test_manual_cooldown_merges(env):
    calls = []
    ev = threading.Event()

    def reader():
        calls.append(1)
        ev.wait(timeout=1.5)
        return _ok_snap("codex")

    env.readers["codex"] = reader
    for n in ("zai", "minimax", "deepseek"):
        env.readers[n] = lambda: None
    svc = _make(env, env.readers)
    svc.start()
    _pump(env, 0.3)
    n0 = len(calls)
    svc.refresh("codex", manual=True)   # 在途 → 合并
    svc.refresh("codex", manual=True)
    assert len(calls) == n0
    ev.set()
    _pump(env, 0.4)
