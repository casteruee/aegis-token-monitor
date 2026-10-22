"""MonitorService —— 采集协调 / 调度 / 验证 / 安全错误映射（设计 §13 monitor.py）。

线程模型（对设计文档「每家一个 multiprocessing 子进程」的已记录偏离，
理由与证据见 proclife.py 模块注释与 DECISIONS v0.2 D-008）：

- **一个协调线程**（本模块）独占所有磁盘写入：cache 与 history 的唯一写者
  （受控单写者，防 ``write_provider`` read-modify-write 并发覆盖）。
- **ThreadPoolExecutor（4 workers）** 只跑 reader；reader 内部自带硬超时
  （Codex subprocess 60s、HTTP socket 10s），单家失败/超时不影响别家。
- **Windows Job Object kill-on-close**（proclife.py）保证应用退出时
  CodexBar 等孙进程被系统回收；绝不波及用户独立运行的同名进程。
- UI 通过 ``subscribe`` 收信封；回调在协调线程触发，GUI 侧自行转队列。

安全约束：
- 结果只保留验证后的 Snapshot 与固定错误码；不向信封传递 exception 原文。
- generation id 防过期结果覆盖新状态；shutdown 后丢弃迟到结果。

硬约束继承：
- 只有 ``status == ok`` 的 live 快照写缓存、写历史 observation；cached /
  stale / UI 重绘不产生 observation；失败仅记 attempt（history 内部处理）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from aegis import cache as cache_mod
from aegis.model import Snapshot
from aegis.services import validation
from aegis.services.contracts import (
    AVAIL_ERROR,
    AVAIL_OK,
    AVAIL_UNAVAILABLE,
    COMPLETE_FULL,
    FRESH_CACHED,
    FRESH_LIVE,
    FRESH_NONE,
    FRESH_STALE,
    PROVIDER_ORDER,
    SnapshotEnvelope,
    TimeEnvelope,
    compute_completeness,
    http_status_from_snapshot,
)

logger = logging.getLogger(__name__)

# 手动刷新冷却（设计 §9：重复点击合并，15 秒冷却）
MANUAL_COOLDOWN_SECONDS = 15.0

DEFAULT_TTLS: Dict[str, int] = {
    "codex": 300,
    "zai": 300,
    "minimax": 300,
    "deepseek": 1800,
}


def _now_utc_iso(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = datetime.now(tz=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _ProviderState:
    snapshot: Optional[Snapshot] = None          # 最近一次有效数据（live 或缓存）
    freshness: str = FRESH_NONE                  # LIVE / CACHED / STALE / NONE
    last_success_fetched_at: Optional[str] = None  # UTC ISO
    last_attempt_at: Optional[str] = None
    error_code: Optional[str] = None
    error_http_status: Optional[int] = None
    in_flight: bool = False
    generation: int = 0
    last_manual_at: float = -1e9                 # monotonic


@dataclass
class _FetchEvent:
    provider: str
    generation: int
    snapshot: Snapshot
    started_at: str
    finished_at: str


def _run_reader(reader: Callable[[], Snapshot]) -> Snapshot:
    """reader 包装。v0.1 reader 自身永不抛异常；此处兜底映射 internal_error，
    且**不**把异常原文放进 Snapshot（只留类型名，无消息）。
    ``None``（异常返回值）同样视为 internal_error，绝不进入信封。"""
    try:
        snap = reader()
    except Exception:  # noqa: BLE001
        snap = None
    if snap is None:
        from aegis.model import ErrorInfo
        return Snapshot(
            provider="?",
            status="error",
            error=ErrorInfo(type="internal_error", message=""),
        )
    return snap


def _map_error_code(snapshot: Snapshot) -> tuple[Optional[str], Optional[int]]:
    """Snapshot.error → (固定码, http_status)。未知类型映射 internal_error。"""
    err = snapshot.error
    if err is None:
        return None, None
    code = err.type or "internal_error"
    known = {
        "missing_credential", "http_error", "network_error", "parse_error",
        "unexpected_shape", "codex_source_unavailable", "SERVICE_UNAVAILABLE",
        "internal_error", "timeout",
    }
    if code == "http_error":
        status = http_status_from_snapshot(snapshot)
        if status == 429:
            return "http_rate_limited", 429
        return "http_error", status
    if code not in known:
        return "internal_error", None
    return code, None


class MonitorService:
    """四 provider 独立采集协调器。``start()`` 后先广播缓存态，再首发全量刷新。"""

    def __init__(
        self,
        history=None,                       # aegis.history.store.HistoryStore
        readers: Optional[Dict[str, Callable[[], Snapshot]]] = None,
        ttls: Optional[Dict[str, int]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._history = history
        self._readers = dict(readers) if readers else {}
        if not self._readers:
            from aegis.providers import READERS
            self._readers = dict(READERS)
        self._ttls = dict(ttls or DEFAULT_TTLS)
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))

        self._states: Dict[str, _ProviderState] = {
            name: _ProviderState() for name in PROVIDER_ORDER
        }
        self._events: "queue.Queue[object]" = queue.Queue()
        self._subscribers: List[Callable[[Dict[str, SnapshotEnvelope]], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._refresh_id = 0  # 单调递增轮次（history refresh_id）

    # ------------------------------------------------------------------
    # 公共 API（UI 线程调用）
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[Dict[str, SnapshotEnvelope]], None]) -> None:
        self._subscribers.append(callback)

    def start(self) -> None:
        """读缓存（严格验证）→ 立即广播 → 后台首发四家全量刷新。"""
        proclife_ok = True
        try:
            from aegis.services import proclife
            proclife_ok = proclife.install_kill_on_close()
        except Exception:  # noqa: BLE001
            proclife_ok = False
        if not proclife_ok:
            logger.warning("proclife: kill-on-close 未生效（非 Windows 或 ctypes 失败）")

        self._load_cache()
        self._publish()

        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="aegis-fetch"
        )
        self._thread = threading.Thread(
            target=self._coordinator_loop, name="aegis-coordinator", daemon=True
        )
        self._thread.start()
        # 首发：四家全部刷新（TTL 后续生效）
        self.refresh(manual=False)

    def refresh(self, provider: Optional[str] = None, manual: bool = True) -> None:
        """请求刷新。``provider=None`` = 全部。重复/在途合并；手动有冷却。"""
        names = [provider] if provider else list(PROVIDER_ORDER)
        now_mono = time.monotonic()
        for name in names:
            st = self._states.get(name)
            if st is None:
                continue
            if st.in_flight:
                continue  # 同 provider 只允许一项在途
            if manual and (now_mono - st.last_manual_at) < MANUAL_COOLDOWN_SECONDS:
                continue  # 冷却期内合并
            if manual:
                st.last_manual_at = now_mono
            self._dispatch(name)

    def shutdown(self, grace: float = 2.0) -> None:
        """停止调度、拒绝新任务、回收线程池。子进程由 Job Object 兜底回收。"""
        self._stop.set()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self._thread is not None:
            self._thread.join(timeout=grace)

    def get_envelopes(self) -> Dict[str, SnapshotEnvelope]:
        """当前状态快照（任意线程可读；字段为不可变快照意图）。"""
        return {name: self._envelope(name) for name in PROVIDER_ORDER}

    # ------------------------------------------------------------------
    # 内部：缓存加载（严格验证）
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        try:
            raw = cache_mod.read_all()
        except Exception:  # noqa: BLE001 - 缓存读失败视同无缓存
            raw = {}
        for name, entry in raw.items():
            st = self._states.get(name)
            if st is None:
                continue
            try:
                snap = validation.validate_snapshot(name, entry.snapshot.to_dict())
                # fetched_at 时间戳必须可解析，否则整条丢弃（不能显示假年龄）
                from aegis.cache import _parse_utc_iso
                _parse_utc_iso(entry.fetched_at_utc)
            except Exception:  # noqa: BLE001 - 损坏条目整体拒绝（不回退 0/0）
                logger.warning("monitor: cache entry %s rejected (invalid)", name)
                continue
            st.snapshot = snap
            st.freshness = FRESH_CACHED
            st.last_success_fetched_at = entry.fetched_at_utc

    # ------------------------------------------------------------------
    # 内部：调度与协调（仅协调线程写状态）
    # ------------------------------------------------------------------

    def _dispatch(self, name: str) -> None:
        st = self._states[name]
        reader = self._readers.get(name)
        if reader is None or self._executor is None:
            return
        st.generation += 1
        st.in_flight = True
        st.last_attempt_at = _now_utc_iso(self._clock())
        gen = st.generation
        self._set_loading(name, True)
        try:
            self._executor.submit(self._worker, name, gen, reader)
        except RuntimeError:
            st.in_flight = False
            self._set_loading(name, False)

    def _worker(self, name: str, gen: int, reader: Callable[[], Snapshot]) -> None:
        started = _now_utc_iso(self._clock())
        snap = _run_reader(reader)
        if snap.provider == "?":  # 兜底错误快照补名
            snap.provider = name
        self._events.put(
            _FetchEvent(
                provider=name,
                generation=gen,
                snapshot=snap,
                started_at=started,
                finished_at=_now_utc_iso(self._clock()),
            )
        )

    def _coordinator_loop(self) -> None:
        while not self._stop.is_set():
            # 计算下个 auto 刷新时刻（只在无在途且距成功 >= TTL 时触发）
            delay = self._auto_refresh_tick()
            try:
                ev = self._events.get(timeout=min(delay, 0.5))
            except queue.Empty:
                continue
            if isinstance(ev, _FetchEvent):
                self._handle_result(ev)
            elif isinstance(ev, str) and ev == "_loading_changed":
                self._publish()
        # drain：退出前不再处理新结果

    def _auto_refresh_tick(self) -> float:
        """到期 provider 自动重抓；返回建议 sleep 秒数。"""
        now = self._clock()
        now_mono = time.monotonic()
        next_wake = 5.0
        for name in PROVIDER_ORDER:
            st = self._states[name]
            if st.in_flight:
                continue
            ttl = self._ttls.get(name, 300)
            due = self._due_seconds(st, ttl)
            if due <= 0:
                if now_mono - st.last_manual_at >= MANUAL_COOLDOWN_SECONDS:
                    self.refresh(name, manual=False)
                    continue
            else:
                next_wake = min(next_wake, due)
        return next_wake

    def _due_seconds(self, st: _ProviderState, ttl: int) -> float:
        """距下次 auto 刷新的秒数；<=0 表示已到期。

        - LIVE 基准 = 最近成功时刻（TTL 语义）。
        - 失败 / stale 基准 = max(最近成功, 最近尝试) —— 失败后同样等一个
          TTL 再自动重试，禁止 0.5s 级重试风暴（设计 §9：429/失败按正常周期）。
        - 从未成功且从未尝试 → 已到期。
        """
        if st.freshness == FRESH_LIVE and st.last_success_fetched_at is not None:
            base = st.last_success_fetched_at
        else:
            candidates = [t for t in (st.last_success_fetched_at, st.last_attempt_at) if t]
            if not candidates:
                return 0.0
            from aegis.cache import _parse_utc_iso
            try:
                base_dt = max(_parse_utc_iso(t) for t in candidates)
            except Exception:  # noqa: BLE001
                return 0.0
            now = self._clock()
            return max(0.0, ttl - (now - base_dt).total_seconds())
        try:
            from aegis.cache import _parse_utc_iso
            last = _parse_utc_iso(base)
        except Exception:  # noqa: BLE001
            return 0.0
        now = self._clock()
        elapsed = (now - last).total_seconds()
        return max(0.0, ttl - elapsed)

    def _set_loading(self, name: str, loading: bool) -> None:
        # loading 是信封叠加态；这里只触发一次重绘广播
        self._events.put("_loading_changed")

    def _handle_result(self, ev: _FetchEvent) -> None:
        if self._stop.is_set():
            return   # shutdown 后拒绝一切迟到结果（不写缓存 / 不写历史）
        st = self._states[ev.provider]
        # 过期结果（generation 已推进 / shutdown 后）一律丢弃
        if ev.generation != st.generation or not st.in_flight:
            return
        st.in_flight = False

        snap = ev.snapshot
        if snap.status == "ok":
            if not self._validate_live(ev.provider, snap):
                snap = self._rejected_snapshot(ev.provider)
            else:
                st.snapshot = snap
                st.freshness = FRESH_LIVE
                st.last_success_fetched_at = ev.finished_at
                st.error_code = None
                st.error_http_status = None
                self._write_cache(ev.provider, snap)
                self._record_observation(ev, snap)
                self._publish()
                return
        # ---- 失败路径：安全映射 + stale 回退 ----
        code, http_status = _map_error_code(snap)
        st.error_code = code
        st.error_http_status = http_status
        if st.snapshot is not None:
            st.freshness = FRESH_STALE
        else:
            st.freshness = FRESH_NONE
        self._record_attempt_failed(ev, code)
        self._publish()

    def _validate_live(self, name: str, snap: Snapshot) -> bool:
        """live 结果再过一遍严格验证（数值/时间），坏值当失败处理。"""
        try:
            validation.validate_snapshot(name, snap.to_dict())
            return True
        except Exception:  # noqa: BLE001
            return False

    def _rejected_snapshot(self, name: str) -> Snapshot:
        from aegis.model import ErrorInfo
        return Snapshot(
            provider=name,
            status="error",
            source=f"{name}-api",
            error=ErrorInfo(type="unexpected_shape", message=""),
        )

    def _write_cache(self, name: str, snap: Snapshot) -> None:
        try:
            cache_mod.write_provider(name, snap, now=self._clock())
        except Exception as e:  # noqa: BLE001 - 缓存写失败不拖垮 live 结果
            logger.warning("monitor: cache write failed (%s): %s", name, type(e).__name__)

    def _record_observation(self, ev: _FetchEvent, snap: Snapshot) -> None:
        if self._history is None:
            return
        try:
            self._refresh_id += 1
            obs_id = self._history.record_observation(
                snapshot=snap,
                fetched_at_utc=ev.finished_at,
                refresh_id=self._refresh_id,
            )
            self._history.record_attempt(
                provider=ev.provider,
                started_at=ev.started_at,
                finished_at=ev.finished_at,
                result_code="ok",
                observation_id=obs_id,
            )
        except Exception as e:  # noqa: BLE001 - 历史故障不拖死实时卡片
            logger.warning("monitor: history write failed: %s", type(e).__name__)

    def _record_attempt_failed(self, ev: _FetchEvent, code: Optional[str]) -> None:
        if self._history is None:
            return
        try:
            self._history.record_attempt(
                provider=ev.provider,
                started_at=ev.started_at,
                finished_at=ev.finished_at,
                result_code=code or "internal_error",
                observation_id=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("monitor: history attempt write failed: %s", type(e).__name__)

    # ------------------------------------------------------------------
    # 内部：信封构造与广播
    # ------------------------------------------------------------------

    def _envelope(self, name: str) -> SnapshotEnvelope:
        st = self._states[name]
        snap = st.snapshot
        # availability 判定基于**最近一次尝试结果**，不是旧数据字段的状态：
        # stale（失败但有旧值）→ ERROR（“更新失败 · 旧数据”）；
        # 失败且无任何值 → UNAVAILABLE（“暂不可用”）。
        if st.freshness == FRESH_STALE:
            availability = AVAIL_ERROR
        elif snap is not None and snap.status == "ok":
            availability = AVAIL_OK
        elif snap is not None:
            availability = AVAIL_ERROR
        else:
            availability = AVAIL_UNAVAILABLE
        completeness = (
            compute_completeness(name, snap)
            if availability == AVAIL_OK else COMPLETE_FULL
        )
        return SnapshotEnvelope(
            provider=name,
            snapshot=snap,
            freshness=st.freshness,
            completeness=completeness,
            availability=availability,
            time=TimeEnvelope(
                source_updated_at=getattr(snap, "updated_at", None) if snap else None,
                last_success_fetched_at=st.last_success_fetched_at,
                last_attempt_at=st.last_attempt_at,
            ),
            error_code=st.error_code,
            error_http_status=st.error_http_status,
            loading=st.in_flight,
        )

    def _publish(self) -> None:
        envelopes = {name: self._envelope(name) for name in PROVIDER_ORDER}
        for cb in list(self._subscribers):
            try:
                cb(envelopes)
            except Exception:  # noqa: BLE001 - 订阅者异常不影响服务
                logger.exception("monitor: subscriber raised")


__all__ = ["MonitorService", "MANUAL_COOLDOWN_SECONDS", "DEFAULT_TTLS"]
