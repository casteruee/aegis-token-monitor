"""SQLite 历史库离线测试：入账规则、备份恢复、损坏保护、保留策略。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aegis.history.store import HistoryStore, HistoryUnavailable
from aegis.history.activity import footprint_7d, confirmed_increase_days
from aegis.model import Balance, Snapshot, Window


UTC = timezone.utc


def _snap(provider="codex", used=10.0, resets="2026-09-20T00:00:00Z"):
    return Snapshot(
        provider=provider, status="ok",
        five_hour=Window(used, 100 - used, resets, 300),
        weekly=Window(used / 2, 100 - used / 2, resets, 10080),
        balance=Balance(value=12.34, currency="CNY") if provider == "deepseek" else None,
    )


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.sqlite3")
    s.open()
    yield s
    s.close()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_observation_roundtrip(store):
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    obs_id = store.record_observation(_snap(used=10.0), _iso(now), refresh_id=1)
    assert obs_id >= 1
    store.record_attempt("codex", _iso(now), _iso(now), "ok", obs_id)
    rows = store.query_observations("codex", now - timedelta(hours=1), now + timedelta(hours=1))
    assert len(rows) == 1
    r = rows[0]
    assert r.five_used == 10.0 and r.five_remaining == 90.0
    assert r.five_resets_at == "2026-09-20T00:00:00Z"
    assert r.five_window_minutes == 300


def test_disabled_store_raises_history_unavailable(tmp_path):
    s = HistoryStore(tmp_path / "bad.sqlite3")
    # 不 open 直接写 → 禁用
    with pytest.raises(HistoryUnavailable):
        s.record_observation(_snap(), "2026-09-12T12:00:00Z", 1)


def test_corrupt_db_open_keeps_file_and_raises(tmp_path):
    p = tmp_path / "corrupt.sqlite3"
    p.write_bytes(b"this is not a sqlite database" * 100)
    s = HistoryStore(p)
    with pytest.raises(HistoryUnavailable):
        s.open()
    # 现场保留，不删除重建
    assert p.exists()
    assert p.read_bytes().startswith(b"this is not")


def test_backup_and_restore_roundtrip(store, tmp_path):
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    for i in range(5):
        store.record_observation(_snap(used=10.0 + i), _iso(now + timedelta(minutes=i)), refresh_id=i + 1)
    dest = tmp_path / "backup" / "b.sqlite3"
    assert store.backup(dest) is True
    assert store.verify_backup(dest) is True
    # 恢复到隔离目录 → 条数与时间范围一致
    restored = HistoryStore(tmp_path / "restored.sqlite3")
    # 直接从备份文件复制内容打开（模拟恢复）
    import shutil
    shutil.copy(dest, tmp_path / "restored.sqlite3")
    conn = sqlite3.connect(str(tmp_path / "restored.sqlite3"))
    n, tmin, tmax = conn.execute(
        "SELECT COUNT(*), MIN(fetched_at_utc), MAX(fetched_at_utc) FROM observations"
    ).fetchone()
    conn.close()
    assert n == 5
    assert tmin == _iso(now) and tmax == _iso(now + timedelta(minutes=4))


def test_backup_verify_rejects_non_db(tmp_path):
    s = HistoryStore(tmp_path / "x.sqlite3")
    s.open()
    try:
        bad = tmp_path / "bad.sqlite3"
        bad.write_bytes(b"junk")
        assert s.verify_backup(bad) is False
        assert s.verify_backup(tmp_path / "missing.sqlite3") is False
    finally:
        s.close()


def test_maintenance_requires_backup_and_cuts_retention(store, tmp_path):
    now = datetime.now(tz=UTC)
    old = now - timedelta(days=200)
    store.record_observation(_snap(), _iso(old), refresh_id=1)
    store.record_observation(_snap(used=50), _iso(now), refresh_id=2)
    store.record_attempt("codex", _iso(old), _iso(old), "ok", None)
    assert store.maintenance(now) is True
    rows = store.query_observations("codex", now - timedelta(days=210), now)
    assert len(rows) == 1 and rows[0].five_used == 50.0
    conn = sqlite3.connect(str(store._path))
    n_att = conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0]
    conn.close()
    assert n_att == 0  # 30 天保留已清掉 200 天前的 attempt


def test_unique_refresh_provider(store):
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    store.record_observation(_snap(), _iso(now), refresh_id=7)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_observation(_snap(), _iso(now), refresh_id=7)


# ---------------------------------------------------------------------------
# 足迹判定（activity）
# ---------------------------------------------------------------------------

class _Obs:
    def __init__(self, fetched, five_used, five_resets="2026-09-20T00:00:00Z", five_wm=300):
        self.fetched_at_utc = fetched
        self.five_used = five_used
        self.five_resets_at = five_resets
        self.five_window_minutes = five_wm
        self.weekly_used = None
        self.weekly_resets_at = None
        self.weekly_window_minutes = None


def _iso_dt(y, mo, d, h, mi):
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:00Z"


def test_footprint_confirmed_on_same_window_increase():
    obs = [
        _Obs(_iso_dt(2026, 9, 12, 1, 0), 10.0),
        _Obs(_iso_dt(2026, 9, 12, 1, 4), 15.0),   # +5，间隔 4min ≤ 600s
    ]
    confirmed = confirmed_increase_days(obs)
    assert confirmed == {"2026-09-12"}


def test_footprint_negative_delta_is_reset_not_activity():
    obs = [
        _Obs(_iso_dt(2026, 9, 12, 1, 0), 90.0),
        _Obs(_iso_dt(2026, 9, 12, 1, 4), 10.0),   # used 下降 = 重置
    ]
    assert confirmed_increase_days(obs) == set()


def test_footprint_reset_change_breaks_comparison():
    obs = [
        _Obs(_iso_dt(2026, 9, 12, 1, 0), 10.0, five_resets="2026-09-20T00:00:00Z"),
        _Obs(_iso_dt(2026, 9, 12, 1, 4), 15.0, five_resets="2026-09-25T00:00:00Z"),
    ]
    assert confirmed_increase_days(obs) == set()


def test_footprint_gap_too_long_breaks():
    obs = [
        _Obs(_iso_dt(2026, 9, 12, 1, 0), 10.0),
        _Obs(_iso_dt(2026, 9, 12, 1, 30), 15.0),  # 30min > 2×TTL(300s)
    ]
    assert confirmed_increase_days(obs) == set()


def test_footprint_marks_days():
    obs = [
        _Obs(_iso_dt(2026, 9, 12, 1, 0), 10.0),
        _Obs(_iso_dt(2026, 9, 12, 1, 4), 15.0),
        _Obs(_iso_dt(2026, 9, 11, 3, 0), 20.0),
        _Obs(_iso_dt(2026, 9, 11, 3, 4), 18.0),   # 下降 → 只算 observed
    ]
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    marks = footprint_7d(obs, now)
    assert len(marks) == 7
    assert marks[-1].kind == "confirmed_increase"   # 今天（09-12）
    assert marks[-2].kind == "observed"             # 09-11
    assert marks[0].kind == "none"


def test_footprint_deepseek_balance_never_confirmed():
    # DeepSeek 无窗口字段 → 不产生 confirmed_increase
    obs = [
        _Obs(_iso_dt(2026, 9, 12, 1, 0), None, five_resets=None, five_wm=None),
        _Obs(_iso_dt(2026, 9, 12, 1, 4), None, five_resets=None, five_wm=None),
    ]
    assert confirmed_increase_days(obs) == set()
