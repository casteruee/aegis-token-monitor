"""SQLite 额度观测历史（UI_NEXT_VERSION_DESIGN §12.1 基础层）。

硬约束（设计 §7 + §12.1）：
- **只有成功实际采集**（live + status ok + 验证通过）新增 observation；
  cached / stale / UI 重绘不产生观测；失败记 attempt（不冒充成功数据）。
- 原采集时间（fetched_at_utc，UTC）与源时间（source_updated_at）**分开保存**。
- 单写者：所有写路径在 MonitorService 协调线程；busy_timeout 1s。
- rollback journal + synchronous FULL（不引入 WAL）；时刻一律 UTC。
- 损坏库：保留现场、禁用写入、**不**自动删除重建；实时卡片继续。
- 备份走 sqlite3 backup API，完成后 quick_check；恢复校验在隔离目录进行。
- 保留策略：observation 180 天、attempt 30 天；每天最多一次维护，且必须
  先有可验证备份。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
OBSERVATION_RETENTION_DAYS = 180
ATTEMPT_RETENTION_DAYS = 30


class HistoryUnavailable(Exception):
    """历史库不可用（损坏 / 只读 / 锁死）——调用方降级为“历史暂未保存”。"""


@dataclass(frozen=True)
class Observation:
    observation_id: int
    provider: str
    fetched_at_utc: str
    source_updated_at: Optional[str]
    five_used: Optional[float]
    five_remaining: Optional[float]
    five_resets_at: Optional[str]
    five_window_minutes: Optional[int]
    weekly_used: Optional[float]
    weekly_remaining: Optional[float]
    weekly_resets_at: Optional[str]
    weekly_window_minutes: Optional[int]
    balance_value: Optional[float]
    currency: Optional[str]


@dataclass(frozen=True)
class DayMark:
    """观测足迹的单日标记（设计 §7E）。"""

    date: str                      # 统计时区本地日期 YYYY-MM-DD
    kind: str                      # "confirmed_increase" | "observed" | "none"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    refresh_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    source_updated_at TEXT,
    five_used REAL,
    five_remaining REAL,
    five_resets_at TEXT,
    five_window_minutes INTEGER,
    weekly_used REAL,
    weekly_remaining REAL,
    weekly_resets_at TEXT,
    weekly_window_minutes INTEGER,
    balance_value REAL,
    currency TEXT,
    UNIQUE (refresh_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_obs_provider_time
    ON observations (provider, fetched_at_utc);
CREATE TABLE IF NOT EXISTS fetch_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT NOT NULL,
    result_code TEXT NOT NULL,
    related_observation_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_att_provider_time
    ON fetch_attempts (provider, finished_at_utc);
"""


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HistoryStore:
    """SQLite 历史库。所有方法在**单写者线程**调用（MonitorService 协调线程），
    查询方法可从查询 worker 调用（SQLite 连接按需打开，避免跨线程共享）。"""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._write_conn: Optional[sqlite3.Connection] = None
        self._disabled = False   # 损坏 / 只读后置 True，禁写不禁读（读另开连接）

    # ------------------------------------------------------------------
    # 打开 / 关闭
    # ------------------------------------------------------------------

    def open(self) -> bool:
        """打开写连接并初始化 schema。失败 → HistoryUnavailable，禁写。"""
        if self._disabled:
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False：连接在调用方线程创建、由 MonitorService
            # 协调线程**独占**写入（单写者纪律，见模块注释）；查询走独立只读连接。
            conn = sqlite3.connect(
                str(self._path), timeout=1.0, check_same_thread=False
            )
            conn.execute("PRAGMA journal_mode=DELETE")   # rollback journal（默认）
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=1000")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('timezone', 'Asia/Shanghai')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at_utc', ?)",
                (_iso(_utc_now()),),
            )
            conn.commit()
            self._write_conn = conn
            return True
        except sqlite3.DatabaseError as e:
            logger.warning("history: open failed: %s", type(e).__name__)
            self._disabled = True
            raise HistoryUnavailable(str(type(e).__name__)) from e

    def close(self) -> None:
        if self._write_conn is not None:
            try:
                self._write_conn.close()
            except sqlite3.Error:
                pass
            self._write_conn = None

    @property
    def available(self) -> bool:
        return self._write_conn is not None and not self._disabled

    # ------------------------------------------------------------------
    # 写入（单写者线程）
    # ------------------------------------------------------------------

    def record_observation(self, snapshot, fetched_at_utc: str, refresh_id: int) -> int:
        """成功采集 → 一条 observation。返回 observation_id。"""
        if not self.available:
            raise HistoryUnavailable("history disabled")
        five = snapshot.five_hour
        weekly = snapshot.weekly
        bal = snapshot.balance
        cur = self._write_conn.execute(
            """INSERT INTO observations
               (refresh_id, provider, fetched_at_utc, source_updated_at,
                five_used, five_remaining, five_resets_at, five_window_minutes,
                weekly_used, weekly_remaining, weekly_resets_at, weekly_window_minutes,
                balance_value, currency)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                refresh_id, snapshot.provider, fetched_at_utc, snapshot.updated_at,
                five.used_percent if five else None,
                five.remaining_percent if five else None,
                five.resets_at if five else None,
                five.window_minutes if five else None,
                weekly.used_percent if weekly else None,
                weekly.remaining_percent if weekly else None,
                weekly.resets_at if weekly else None,
                weekly.window_minutes if weekly else None,
                bal.value if bal else None,
                bal.currency if bal else None,
            ),
        )
        self._write_conn.commit()
        return int(cur.lastrowid)

    def record_attempt(
        self,
        provider: str,
        started_at: str,
        finished_at: str,
        result_code: str,
        observation_id: Optional[int],
    ) -> None:
        """每次尝试记一行（含成功）；只存固定码，不存异常原文。"""
        if not self.available:
            raise HistoryUnavailable("history disabled")
        self._write_conn.execute(
            """INSERT INTO fetch_attempts
               (provider, started_at_utc, finished_at_utc, result_code, related_observation_id)
               VALUES (?,?,?,?,?)""",
            (provider, started_at, finished_at, result_code, observation_id),
        )
        self._write_conn.commit()

    # ------------------------------------------------------------------
    # 查询（可从查询 worker 调用；只读新连接）
    # ------------------------------------------------------------------

    def _query_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, timeout=1.0)
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def query_observations(
        self, provider: str, start_utc: datetime, end_utc: datetime
    ) -> List[Observation]:
        """按 provider 与 UTC 时间窗（左闭右闭）查询观测，时间升序。"""
        if self._disabled:
            raise HistoryUnavailable("history unavailable")
        conn = self._query_conn()
        try:
            rows = conn.execute(
                """SELECT observation_id, provider, fetched_at_utc, source_updated_at,
                          five_used, five_remaining, five_resets_at, five_window_minutes,
                          weekly_used, weekly_remaining, weekly_resets_at, weekly_window_minutes,
                          balance_value, currency
                   FROM observations
                   WHERE provider=? AND fetched_at_utc BETWEEN ? AND ?
                   ORDER BY fetched_at_utc ASC""",
                (provider, _iso(start_utc), _iso(end_utc)),
            ).fetchall()
        finally:
            conn.close()
        return [Observation(*r) for r in rows]

    def latest_observation(self, provider: str) -> Optional[Observation]:
        obs = self.query_observations(
            provider,
            _utc_now() - timedelta(days=3650),
            _utc_now() + timedelta(days=1),
        )
        return obs[-1] if obs else None

    # ------------------------------------------------------------------
    # 备份 / 恢复验证 / 维护
    # ------------------------------------------------------------------

    def backup(self, dest_path) -> bool:
        """sqlite3 backup API → 目标文件；完成后 quick_check 验证。"""
        if self._write_conn is None:
            raise HistoryUnavailable("history not open")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest_conn = sqlite3.connect(str(dest))
        try:
            self._write_conn.backup(dest_conn)
            row = dest_conn.execute("PRAGMA quick_check").fetchone()
            ok = bool(row) and row[0] == "ok"
            return ok
        finally:
            dest_conn.close()

    def verify_backup(self, backup_path) -> bool:
        """独立验证一个备份文件（恢复验证用，不动主库）。"""
        path = Path(backup_path)
        if not path.is_file():
            return False
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                return False
            n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            n_att = conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0]
            return isinstance(n_obs, int) and isinstance(n_att, int)
        except sqlite3.DatabaseError:
            return False
        finally:
            conn.close()

    def maintenance(self, now: Optional[datetime] = None) -> bool:
        """保留策略清理。**先**做一次临时备份并验证，失败则延后（返回 False）。"""
        if not self.available:
            return False
        now = now or _utc_now()
        tmp_backup = self._path.with_suffix(".maintenance.bak")
        try:
            if not self.backup(tmp_backup):
                return False
            obs_cutoff = _iso(now - timedelta(days=OBSERVATION_RETENTION_DAYS))
            att_cutoff = _iso(now - timedelta(days=ATTEMPT_RETENTION_DAYS))
            self._write_conn.execute(
                "DELETE FROM observations WHERE fetched_at_utc < ?", (obs_cutoff,)
            )
            self._write_conn.execute(
                "DELETE FROM fetch_attempts WHERE finished_at_utc < ?", (att_cutoff,)
            )
            self._write_conn.commit()
            return True
        except sqlite3.DatabaseError as e:
            logger.warning("history: maintenance failed: %s", type(e).__name__)
            return False
        finally:
            try:
                tmp_backup.unlink(missing_ok=True)
            except OSError:
                pass
