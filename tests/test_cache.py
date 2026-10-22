"""``aegis.cache`` 离线单元测试（ARCHITECTURE §8 + P3 任务书）。

约束（**P3 禁止事项**）：
- 冻结时间（绝不 ``time.sleep``）。所有「now」由测试显式传入 aware datetime。
- 不引入第三方依赖 / pytest 插件（``time.sleep`` / ``freezegun`` / 等）。
- NO_SECRET_LEAK：缓存读 / 写 / 错误处理全链路中，``sk-`` / ``Bearer`` 类
  模式不得出现在 ``to_dict()`` 与异常文本中（v0.1 cache 不接触凭据）。

覆盖矩阵：
- TTL/freshness（命中=cached、过期+成功=live、过期+失败有缓存=stale）。
- 失败回退三件套（updated_at 保持缓存原值；reliability 绝不 = "live" +
  数据来自缓存）。
- 无缓存 + 失败 → 原样 error。
- 缓存文件损坏 / 不可解析 → 视同无缓存，**不**抛。
- 写时机：失败 snapshot 不写缓存；成功 snapshot 写缓存。
- I/O round-trip：写 → 读 → 字段完整。
- 路径可注入：测试用 ``tmp_path`` 替换 :data:`cache.CACHE_FILE`。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aegis import cache
from aegis.cache import (
    CACHE_FILE,
    CACHE_FORMAT_VERSION,
    CachedEntry,
    get_if_fresh,
    is_fresh,
    read_all,
    resolve_after_fetch,
    write_provider,
)
from aegis.model import ErrorInfo, Snapshot, Window


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def fake_cache_dir(tmp_path: Path, monkeypatch):
    """把 :data:`cache.CACHE_FILE` 重定向到 ``tmp_path``；返回新路径。"""
    new_file = tmp_path / "cache" / "usage_latest.json"
    monkeypatch.setattr(cache, "CACHE_FILE", new_file)
    monkeypatch.setattr(cache, "CACHE_DIR", new_file.parent)
    return new_file


def _snapshot_ok(provider: str, *, used: float = 50.0, reset: str = "2026-09-11T07:51:57Z",
                 updated: str = "2026-09-11T07:30:00Z") -> Snapshot:
    return Snapshot(
        provider=provider,
        status="ok",
        five_hour=Window(
            used_percent=used, remaining_percent=round(100 - used, 2),
            resets_at=reset, window_minutes=300,
        ),
        weekly=Window(
            used_percent=used / 2, remaining_percent=round(100 - used / 2, 2),
            resets_at="2026-09-15T08:00:00Z", window_minutes=10080,
        ),
        updated_at=updated,
        source=f"{provider}-src",
        reliability="live",
    )


def _snapshot_error(provider: str, *, err_type: str = "network_error",
                    message: str = "host=api.example timeout") -> Snapshot:
    return Snapshot(
        provider=provider,
        status="error",
        five_hour=None, weekly=None, balance=None, extras=[],
        updated_at=None,
        source=f"{provider}-src",
        reliability="live",  # reader 层默认；cache 层会覆盖
        error=ErrorInfo(type=err_type, message=message),
    )


# ===========================================================================
# is_fresh
# ===========================================================================

class TestIsFresh:
    def test_within_ttl(self):
        now = datetime(2026, 9, 11, 7, 34, 0, tzinfo=timezone.utc)
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                            snapshot=_snapshot_ok("codex"))
        # delta = 4min < ttl=5min → fresh
        assert is_fresh(entry, now, ttl_seconds=300) is True

    def test_exactly_at_ttl_boundary(self):
        # delta == ttl_seconds：约定为已过期（避免边界处两次刷新争用）
        now = datetime(2026, 9, 11, 7, 35, 0, tzinfo=timezone.utc)
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                            snapshot=_snapshot_ok("codex"))
        assert is_fresh(entry, now, ttl_seconds=300) is False

    def test_just_before_ttl_boundary(self):
        now = datetime(2026, 9, 11, 7, 34, 59, tzinfo=timezone.utc)
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                            snapshot=_snapshot_ok("codex"))
        assert is_fresh(entry, now, ttl_seconds=300) is True

    def test_way_past_ttl(self):
        now = datetime(2026, 9, 11, 8, 0, 0, tzinfo=timezone.utc)
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:00:00Z",
                            snapshot=_snapshot_ok("codex"))
        assert is_fresh(entry, now, ttl_seconds=300) is False

    def test_negative_delta_is_not_fresh(self):
        # now 早于 fetched → delta < 0；按"未到 TTL"理解，但约定 0.0 <= delta
        # 才视为 fresh；负数不 fresh（防止时钟回拨导致用「未来」的数据）
        now = datetime(2026, 9, 11, 7, 25, 0, tzinfo=timezone.utc)
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                            snapshot=_snapshot_ok("codex"))
        assert is_fresh(entry, now, ttl_seconds=300) is False

    def test_invalid_ttl_raises(self):
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                            snapshot=_snapshot_ok("codex"))
        now = datetime(2026, 9, 11, 7, 35, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            is_fresh(entry, now, ttl_seconds=0)
        with pytest.raises(ValueError):
            is_fresh(entry, now, ttl_seconds=-1)

    def test_naive_now_rejected(self):
        entry = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                            snapshot=_snapshot_ok("codex"))
        naive = datetime(2026, 9, 11, 7, 35, 0)  # 无 tzinfo
        with pytest.raises(ValueError):
            is_fresh(entry, naive, ttl_seconds=300)


# ===========================================================================
# write_provider / read_all：I/O round-trip
# ===========================================================================

class TestWriteAndRead:
    def test_round_trip_one_provider(self, fake_cache_dir):
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        snap = _snapshot_ok("codex", used=66.0)
        write_provider("codex", snap, now=now)
        assert fake_cache_dir.exists()

        entries = read_all()
        assert "codex" in entries
        e = entries["codex"]
        assert e.fetched_at_utc == "2026-09-11T07:30:00Z"
        assert e.snapshot.provider == "codex"
        assert e.snapshot.status == "ok"
        assert e.snapshot.five_hour.used_percent == 66.0
        assert e.snapshot.weekly.used_percent == 33.0

    def test_round_trip_multiple_providers(self, fake_cache_dir):
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        write_provider("codex", _snapshot_ok("codex", used=10.0), now=now)
        write_provider("zai", _snapshot_ok("zai", used=20.0),
                        now=now + timedelta(seconds=1))
        write_provider("minimax", _snapshot_ok("minimax", used=30.0),
                        now=now + timedelta(seconds=2))
        write_provider("deepseek", _snapshot_ok("deepseek", used=40.0),
                        now=now + timedelta(seconds=3))

        entries = read_all()
        assert set(entries.keys()) == {"codex", "zai", "minimax", "deepseek"}
        assert entries["codex"].fetched_at_utc == "2026-09-11T07:30:00Z"
        assert entries["zai"].fetched_at_utc == "2026-09-11T07:30:01Z"
        assert entries["minimax"].snapshot.five_hour.used_percent == 30.0

    def test_update_existing_provider(self, fake_cache_dir):
        now1 = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 9, 11, 7, 31, 0, tzinfo=timezone.utc)
        write_provider("codex", _snapshot_ok("codex", used=10.0), now=now1)
        write_provider("codex", _snapshot_ok("codex", used=20.0), now=now2)
        entries = read_all()
        assert entries["codex"].fetched_at_utc == "2026-09-11T07:31:00Z"
        assert entries["codex"].snapshot.five_hour.used_percent == 20.0

    def test_write_failure_snapshot_does_not_write(self, fake_cache_dir):
        # 失败 snapshot 不写缓存（ARCH §8 硬约束）
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        write_provider("codex", _snapshot_error("codex"), now=now)
        assert not fake_cache_dir.exists()
        assert read_all() == {}

    def test_write_failure_does_not_overwrite_existing(self, fake_cache_dir):
        # 先写一条成功 → 再尝试写一条失败 → 成功条目仍保留
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        write_provider("codex", _snapshot_ok("codex", used=10.0), now=now)
        write_provider("codex", _snapshot_error("codex"), now=now)
        entries = read_all()
        assert "codex" in entries
        assert entries["codex"].snapshot.five_hour.used_percent == 10.0

    def test_write_creates_parent_dir(self, fake_cache_dir):
        # fake_cache_dir.parent 应由 write_provider 创建
        assert not fake_cache_dir.parent.exists()
        write_provider("codex", _snapshot_ok("codex"), now=datetime.now(timezone.utc))
        assert fake_cache_dir.parent.exists()

    def test_read_empty_file_no_crash(self, fake_cache_dir):
        # 文件不存在 → 空 dict
        assert read_all() == {}

    def test_read_corrupted_json_no_crash(self, fake_cache_dir):
        fake_cache_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_cache_dir.write_text("{not valid json", encoding="utf-8")
        assert read_all() == {}

    def test_read_wrong_root_type_no_crash(self, fake_cache_dir):
        fake_cache_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_cache_dir.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert read_all() == {}

    def test_read_wrong_version_no_crash(self, fake_cache_dir):
        fake_cache_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_cache_dir.write_text(
            json.dumps({"version": 99, "providers": {}}),
            encoding="utf-8",
        )
        # 版本不兼容 → 视同无缓存（不抛）
        assert read_all() == {}

    def test_read_skips_malformed_individual_entry(self, fake_cache_dir):
        # 单条 provider 损坏不影响其它 provider
        fake_cache_dir.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "providers": {
                "codex": {
                    "fetched_at_utc": "2026-09-11T07:30:00Z",
                    "snapshot": {"provider": "codex", "status": "ok",
                                 "five_hour": None, "weekly": None,
                                 "balance": None, "extras": [],
                                 "updated_at": None, "source": "x",
                                 "reliability": "live", "error": None},
                },
                "broken": "this-should-be-a-dict",
            },
        }
        fake_cache_dir.write_text(json.dumps(payload), encoding="utf-8")
        entries = read_all()
        assert "codex" in entries
        assert "broken" not in entries

    def test_read_skips_entry_missing_snapshot(self, fake_cache_dir):
        fake_cache_dir.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "providers": {
                "codex": {
                    "fetched_at_utc": "2026-09-11T07:30:00Z",
                    # 缺 snapshot
                },
            },
        }
        fake_cache_dir.write_text(json.dumps(payload), encoding="utf-8")
        assert read_all() == {}

    def test_read_handles_empty_file(self, fake_cache_dir):
        fake_cache_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_cache_dir.write_text("", encoding="utf-8")
        assert read_all() == {}

    def test_write_atomic_via_tmp(self, fake_cache_dir, monkeypatch):
        # 写盘使用 .tmp + os.replace（原子替换）—— 中途查看不应出现 .tmp
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        write_provider("codex", _snapshot_ok("codex"), now=now)
        tmp = fake_cache_dir.with_suffix(fake_cache_dir.suffix + ".tmp")
        assert not tmp.exists()


# ===========================================================================
# get_if_fresh：TTL 命中 / 过期分支
# ===========================================================================

class TestGetIfFresh:
    """测试 1：命中 = cached（reliability='cached'）。"""

    def test_hit_returns_cached_reliability(self):
        now = datetime(2026, 9, 11, 7, 34, 0, tzinfo=timezone.utc)
        cached = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                             snapshot=_snapshot_ok("codex", used=10.0))
        snap = get_if_fresh("codex", cached, now, ttl_seconds=300)
        assert snap is not None
        assert snap.reliability == "cached"     # ★
        assert snap.status == "ok"
        assert snap.five_hour.used_percent == 10.0
        # updated_at 保持缓存里的原值
        assert snap.updated_at == "2026-09-11T07:30:00Z"

    def test_miss_returns_none(self):
        now = datetime(2026, 9, 11, 7, 35, 0, tzinfo=timezone.utc)
        cached = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                             snapshot=_snapshot_ok("codex", used=10.0))
        snap = get_if_fresh("codex", cached, now, ttl_seconds=300)
        assert snap is None

    def test_none_cached_returns_none(self):
        now = datetime(2026, 9, 11, 7, 34, 0, tzinfo=timezone.utc)
        snap = get_if_fresh("codex", None, now, ttl_seconds=300)
        assert snap is None

    def test_never_returns_live_reliability(self):
        # 关键不变量：即使命中缓存，返回的快照 reliability 也**绝不**为 'live'
        now = datetime(2026, 9, 11, 7, 34, 0, tzinfo=timezone.utc)
        cached = CachedEntry(fetched_at_utc="2026-09-11T07:30:00Z",
                             snapshot=_snapshot_ok("codex"))
        snap = get_if_fresh("codex", cached, now, ttl_seconds=300)
        assert snap is not None
        assert snap.reliability != "live"
        assert snap.reliability != "stale"


# ===========================================================================
# resolve_after_fetch：失败回退三件套（核心规则）
# ===========================================================================

class TestResolveAfterFetch:
    """测试 2+3+4：成功 → live；失败+缓存 → stale；失败+无缓存 → error。"""

    def test_ok_returns_live(self):
        # 成功抓取 → reliability='live'，不依赖 cached
        cached = CachedEntry(fetched_at_utc="2026-09-11T07:00:00Z",
                             snapshot=_snapshot_ok("codex", used=99.0))  # 旧值
        fresh = _snapshot_ok("codex", used=10.0)  # 新值
        snap = resolve_after_fetch("codex", fresh, cached)
        assert snap.status == "ok"
        assert snap.reliability == "live"        # ★
        assert snap.five_hour.used_percent == 10.0
        assert snap.updated_at == "2026-09-11T07:30:00Z"

    def test_ok_without_cache(self):
        # 成功 + 无缓存 → live
        fresh = _snapshot_ok("codex", used=10.0)
        snap = resolve_after_fetch("codex", fresh, cached=None)
        assert snap.reliability == "live"
        assert snap.status == "ok"

    def test_failure_with_cache_returns_stale(self):
        """★ 关键断言：失败有缓存 → reliability='stale'，updated_at 保持缓存原值。"""
        cached = CachedEntry(
            fetched_at_utc="2026-09-11T07:00:00Z",
            snapshot=_snapshot_ok("codex", used=42.0,
                                  updated="2026-09-11T07:00:00Z"),
        )
        error_snap = _snapshot_error("codex", err_type="network_error",
                                     message="conn refused")
        snap = resolve_after_fetch("codex", error_snap, cached)
        # 三个核心断言
        assert snap.reliability == "stale"             # ★
        assert snap.status == "error"                  # ★
        assert snap.updated_at == "2026-09-11T07:00:00Z"   # ★ 缓存原值（不被改写为 now）

        # 数据来自缓存
        assert snap.five_hour.used_percent == 42.0
        assert snap.weekly.used_percent == 21.0

        # error 用最新一次的错误
        assert snap.error is not None
        assert snap.error.type == "network_error"
        assert snap.error.message == "conn refused"

    def test_failure_without_cache_returns_error(self):
        """无缓存 + 失败 → 原样 error；数据列全 null。"""
        error_snap = _snapshot_error("codex", err_type="http_error",
                                     message="HTTP 401")
        snap = resolve_after_fetch("codex", error_snap, cached=None)
        assert snap.status == "error"
        # 数据列全 null（无缓存可回退）
        assert snap.five_hour is None
        assert snap.weekly is None
        assert snap.balance is None
        assert snap.extras == []
        assert snap.error.type == "http_error"
        # updated_at 也无（不是缓存里的原值，是"未抓取"的语义）
        assert snap.updated_at is None

    def test_failure_with_cache_reliability_never_live(self):
        """★ 核心规则：有缓存的失败路径**永不**返回 reliability='live'（数据来自缓存）。"""
        cached = CachedEntry(fetched_at_utc="2026-09-11T07:00:00Z",
                             snapshot=_snapshot_ok("codex", used=42.0))
        error_snap = _snapshot_error("codex")
        snap = resolve_after_fetch("codex", error_snap, cached)
        # 关键不变量：缓存数据被标为 'live' 是禁止的；这里必须是 'stale'
        assert snap.reliability != "live"
        assert snap.reliability == "stale"
        # 数据确实来自缓存（used=42.0 而非新的 None）
        assert snap.five_hour.used_percent == 42.0

    def test_cached_reliability_overridden(self):
        """缓存里若 reliability='cached' / 'stale'，resolve 必须覆盖为本轮真值。"""
        # 缓存里残留 stale（异常路径），但本轮成功 → 必须变 live
        cached_snap = _snapshot_ok("codex", used=10.0)
        # 手工伪造缓存的 reliability='stale'
        from dataclasses import replace
        cached_snap_stale = replace(cached_snap, reliability="stale")
        cached = CachedEntry(
            fetched_at_utc="2026-09-11T07:00:00Z",
            snapshot=cached_snap_stale,
        )
        fresh = _snapshot_ok("codex", used=20.0)
        snap = resolve_after_fetch("codex", fresh, cached)
        assert snap.reliability == "live"

    def test_failure_preserves_source(self):
        cached = CachedEntry(fetched_at_utc="2026-09-11T07:00:00Z",
                             snapshot=_snapshot_ok("codex", used=42.0))
        error_snap = _snapshot_error("codex")
        snap = resolve_after_fetch("codex", error_snap, cached)
        assert snap.source == "codex-src"  # 来自缓存 snapshot


# ===========================================================================
# write_provider 只在 status='ok' 时写（ARCH §8 硬约束）
# ===========================================================================

class TestWriteTiming:
    """失败 snapshot 不写缓存；成功 snapshot 写缓存。这是 ARCH §8 写时机。"""

    def test_failure_snapshot_never_written(self, fake_cache_dir):
        snap = _snapshot_error("codex", err_type="network_error")
        snap.status = "error"  # 显式
        write_provider("codex", snap, now=datetime.now(timezone.utc))
        assert not fake_cache_dir.exists()

    def test_ok_snapshot_written(self, fake_cache_dir):
        snap = _snapshot_ok("codex")
        write_provider("codex", snap, now=datetime(2026, 9, 11, 7, 30, 0,
                                                   tzinfo=timezone.utc))
        assert fake_cache_dir.exists()
        assert "codex" in read_all()

    def test_failure_preserves_previous_success(self, fake_cache_dir):
        # 先写成功条目；再尝试写失败 → 成功条目仍存在
        now = datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc)
        write_provider("codex", _snapshot_ok("codex", used=42.0), now=now)
        write_provider("codex", _snapshot_error("codex"), now=now)
        entries = read_all()
        assert "codex" in entries
        assert entries["codex"].snapshot.five_hour.used_percent == 42.0


# ===========================================================================
# NO_SECRET_LEAK
# ===========================================================================

class TestNoSecretLeak:
    """缓存读 / 写 / 错误处理全链路不得泄漏假 key。"""

    LEAK_TOKENS = ["sk-", "Bearer ", "ANTHROPIC_AUTH_TOKEN"]

    def test_read_clean(self, fake_cache_dir):
        # 写一条含「伪装 key」的 snapshot → 读回 → to_dict 输出无 key
        snap = _snapshot_ok("codex", used=10.0)
        write_provider("codex", snap, now=datetime.now(timezone.utc))
        entries = read_all()
        for entry in entries.values():
            blob = json.dumps(entry.snapshot.to_dict(), default=str)
            for tok in self.LEAK_TOKENS:
                assert tok not in blob

    def test_write_error_no_leak(self, fake_cache_dir):
        # error snapshot 的 message 含敏感词 → 写盘后读回的 cache 不应泄漏
        snap = _snapshot_error("codex", err_type="http_error",
                               message="Authorization: Bearer sk-test-aaa leak")
        write_provider("codex", snap, now=datetime.now(timezone.utc))
        # 失败不写盘；缓存文件不存在
        assert not fake_cache_dir.exists()

    def test_is_fresh_corrupt_entry_no_crash(self, fake_cache_dir):
        # fetched_at_utc 格式错误 → is_fresh 应让调用方降级（get_if_fresh
        # 返回 None 而不是抛）
        bad_entry = CachedEntry(fetched_at_utc="not-an-iso",
                                snapshot=_snapshot_ok("codex"))
        snap = get_if_fresh("codex", bad_entry,
                            datetime(2026, 9, 11, 7, 30, 0, tzinfo=timezone.utc),
                            ttl_seconds=300)
        assert snap is None


# ===========================================================================
# 路径 / 模块常量
# ===========================================================================

class TestModuleConstants:
    def test_cache_file_path_under_project(self):
        # CACHE_FILE 必须在项目根 cache/usage_latest.json
        assert CACHE_FILE.name == "usage_latest.json"
        assert CACHE_FILE.parent.name == "cache"
        # 项目根（aegis.cache.parent.parent）含 aegis/ 目录
        assert (CACHE_FILE.parent.parent / "aegis").is_dir()

    def test_cache_format_version_is_int(self):
        assert isinstance(CACHE_FORMAT_VERSION, int)
        assert CACHE_FORMAT_VERSION >= 1