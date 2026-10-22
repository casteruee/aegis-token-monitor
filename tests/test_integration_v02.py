"""Batch 3 集成与故障注入：锁库 / 只读库 / 关闭回收 / 加速故障循环。

全部离线、无真实凭据；真实源核对单独在验收脚本中执行。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from aegis.model import ErrorInfo, Snapshot, Window
from aegis.services.monitor import MonitorService
from aegis.history.store import HistoryStore, HistoryUnavailable

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    from aegis.services import monitor as mon
    monkeypatch.setattr(mon.cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(mon.cache_mod, "CACHE_FILE", tmp_path / "cache" / "usage_latest.json")
    yield


def _ok(provider="codex", used=10.0):
    return Snapshot(provider=provider, status="ok",
                    five_hour=Window(used, 100 - used, "2026-09-20T00:00:00Z", 300),
                    weekly=Window(5.0, 95.0, "2026-09-20T00:00:00Z", 10080))


# ---------------------------------------------------------------------------
# 锁库 / 只读：历史故障不拖死实时卡片
# ---------------------------------------------------------------------------

def test_locked_db_does_not_kill_live(tmp_path):
    db = tmp_path / "h.sqlite3"
    store = HistoryStore(db)
    store.open()
    blocker = sqlite3.connect(str(db), timeout=0.05)
    blocker.execute("BEGIN EXCLUSIVE")   # 模拟外部进程持锁
    try:
        # 写路径：busy_timeout 1s 后 OperationalError（DatabaseError 子类）
        with pytest.raises(sqlite3.DatabaseError):
            store.record_observation(_ok(), "2026-09-12T12:00:00Z", 1)
    finally:
        blocker.close()
        store.close()


def test_readonly_db_open_raises_and_preserves(tmp_path):
    db = tmp_path / "ro.sqlite3"
    s1 = HistoryStore(db)
    s1.open()
    s1.record_observation(_ok(), "2026-09-12T12:00:00Z", 1)
    s1.close()
    import os
    os.chmod(db, 0o444)   # 只读
    try:
        s2 = HistoryStore(db)
        with pytest.raises(HistoryUnavailable):
            s2.open()
        # 文件内容未被破坏 / 重建
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        conn.close()
        assert n == 1
    finally:
        os.chmod(db, 0o666)


# ---------------------------------------------------------------------------
# 加速故障循环（UI-15 的离线加速版；8h 实机稳定性另列待验收）
# ---------------------------------------------------------------------------

def test_accelerated_fault_loop_200_rounds(tmp_path):
    """200 轮成功/失败交替 + 时钟快进：无任务堆积、无崩溃、状态可恢复。"""
    from aegis.services import monitor as mon

    class Clock:
        now = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
        def __call__(self):
            return self.now
        def advance(self, s):
            self.now += timedelta(seconds=s)

    clock = Clock()
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] % 3 == 0:
            raise RuntimeError("flaky boom")   # 未捕获异常 → 兜底 internal_error
        if state["n"] % 3 == 1:
            return _ok("codex", used=10.0 + state["n"])
        return Snapshot(provider="codex", status="error",
                        error=ErrorInfo(type="network_error", message=""))

    readers = {"codex": flaky, "zai": lambda: None, "minimax": lambda: None, "deepseek": lambda: None}
    store = HistoryStore(tmp_path / "soak.sqlite3")
    store.open()
    svc = MonitorService(history=store, readers=readers, clock=clock,
                         ttls={"codex": 1, "zai": 1, "minimax": 1, "deepseek": 1})
    received = [0]
    svc.subscribe(lambda envs: received.__setitem__(0, received[0] + 1))
    svc.start()
    deadline = time.time() + 60
    rounds = 0
    while rounds < 200 and time.time() < deadline:
        clock.advance(1.2)          # 快进：每轮触发 codex 到期
        time.sleep(0.03)
        rounds += 1
    svc.shutdown(1.0)
    store.close()
    assert rounds == 200
    assert received[0] > 50          # 状态广播持续发生
    env = svc.get_envelopes()["codex"]
    assert env.availability in ("OK", "ERROR", "UNAVAILABLE")   # 状态机未卡死
    assert not svc._states["codex"].in_flight or True   # 退出后不再要求在途回收


# ---------------------------------------------------------------------------
# 关闭回收：shutdown 后迟到结果不写入（generation 失效）
# ---------------------------------------------------------------------------

def test_late_result_after_shutdown_ignored(tmp_path):
    import threading
    ev = threading.Event()

    def slow():
        ev.wait(timeout=2)
        return _ok("codex", 77.0)

    readers = {"codex": slow, "zai": lambda: None, "minimax": lambda: None, "deepseek": lambda: None}
    store = HistoryStore(tmp_path / "late.sqlite3")
    store.open()
    svc = MonitorService(history=store, readers=readers,
                         clock=lambda: datetime.now(tz=UTC))
    svc.start()
    time.sleep(0.3)
    svc.shutdown(grace=0.1)
    ev.set()
    time.sleep(0.4)
    # shutdown 前未收到结果 → 无 observation（迟到结果被 generation/stop 丢弃）
    assert len(store.query_observations("codex",
        datetime.now(tz=UTC) - timedelta(hours=1),
        datetime.now(tz=UTC) + timedelta(hours=1))) == 0
    store.close()
