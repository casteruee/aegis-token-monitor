"""主窗口与应用装配（v0.4：四行自然高度卡 + 内容最大宽 + 纵向滚动）。

布局（docs/UI_POLISH_CC_BRIEF.md 窗口缩放合同）：初始 1080×700 DIP（按可用
工作区夹紧并留安全余量），最小 720×460；顶部 56 DIP（标题 + 最近刷新 + 汇总
+ 刷新，汇总含全部四家）→ 主纵向滚动区。卡片按内容自然高度排列，窗口变高
多余空间留在内容末尾，窗口变矮自然出滚动条；**没有按高度切换样式的阈值**，
字号、间距、进度条高度全程稳定。页面内容最大宽 1120 DIP，超宽窗口居中。
用户窗口尺寸保存在 data/window.json，恢复时限制在当前可用工作区内。

线程边界（继承 v0.2）：
- UI 线程只渲染；MonitorService 回调在协调线程 → queue + QTimer 轮询。
- 历史查询在专用 worker 线程；UI 主线程零 SQLite I/O。
- 关闭：停定时器 → monitor.shutdown → history.close（Job Object 兜底）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from aegis.desktop import theme
from aegis.desktop.adapter import (
    build_money_vm,
    build_provider_vm,
    build_summary,
)
from aegis.desktop.pages import DetailPage, HistoryPage
from aegis.desktop.widgets import (
    FootprintStrip,
    MoneyCardRow,
    RefreshButton,
    ServiceCardRow,
    _font,
)
from aegis.history.activity import footprint_7d
from aegis.services.contracts import PROVIDER_ORDER

logger = logging.getLogger(__name__)

MONEY_PROVIDER = "deepseek"

# 窗口几何持久化（个人运行时，不入库；损坏一律按无保存处理）
_WINDOW_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "window.json"
# 恢复/夹紧时的屏幕安全余量（DIP）
_SCREEN_MARGIN = 24


class MainWindow(QMainWindow):
    def __init__(
        self,
        monitor,
        history_store,
        *,
        app_title: str = "Aegis Token Monitor",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._monitor = monitor
        self._history = history_store
        self._envelopes = {}
        self._ui_queue: "queue.Queue[object]" = queue.Queue()
        self._closed = False

        # 手动登记的累计充值（服务层读取；UI 不碰文件）
        from aegis.services import topup as topup_mod
        self._topup_mod = topup_mod
        self._topup = topup_mod.load_topup(MONEY_PROVIDER)

        self.setWindowTitle(app_title)
        self.setMinimumSize(*theme.WINDOW_MIN)

        # ---- 中央结构：固定头部 + 主纵向滚动区 ----
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        self._stack_host = QWidget()
        self._stack = QStackedLayout(self._stack_host)
        self._stack.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack_host, 1)

        # ---- 首页（滚动区）：内容最大宽 1120 居中；四行自然高度卡 + 观察记录 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 超宽窗口：内容达最大宽后由滚动区水平居中，不把进度条无限拉长
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        home = QWidget()
        home.setMaximumWidth(theme.CONTENT_MAX_WIDTH)
        home.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._home_lay = QVBoxLayout(home)
        self._home_lay.setContentsMargins(theme.MARGIN_OUTER, theme.GAP,
                                          theme.MARGIN_OUTER, theme.GAP)
        self._home_lay.setSpacing(theme.GAP)

        self._cards: Dict[str, ServiceCardRow] = {}
        self._money_card = MoneyCardRow(MONEY_PROVIDER)
        for name in PROVIDER_ORDER:
            if name == MONEY_PROVIDER:
                card = self._money_card
            else:
                card = ServiceCardRow(name)
                self._cards[name] = card
            # v0.4.1：卡片纯展示，无"详情 ›"入口、整卡不可点击；
            # 键盘焦点保留给刷新 / 历史等实际按钮。
            # 卡片取内容自然高度（stretch 0）：窗口变高时多余空间留在内容末尾，
            # 窗口变矮时滚动区自然出滚动条；字号与间距全程不随高度缩放
            self._home_lay.addWidget(card, 0)

        self._footprint = FootprintStrip()
        self._footprint.history_requested.connect(self._open_history)
        self._home_lay.addWidget(self._footprint, 0)
        self._home_lay.addStretch(1)   # 剩余空间统一沉底

        scroll.setWidget(home)
        self._home_page = scroll
        self._stack.addWidget(scroll)

        # ---- 详情 / 历史页（各自包滚动容器；Esc 行为不变） ----
        self._detail_page = DetailPage()
        self._detail_page.back_requested.connect(self._go_home)
        self._detail_page.retry_requested.connect(self._on_retry)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_scroll.setWidget(self._detail_page)
        self._stack.addWidget(detail_scroll)

        self._history_page = HistoryPage()
        self._history_page.back_requested.connect(self._go_home)
        self._history_page.query_requested.connect(self._on_history_query)
        self._history_page.backup_requested.connect(self._on_backup)
        history_scroll = QScrollArea()
        history_scroll.setWidgetResizable(True)
        history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        history_scroll.setWidget(self._history_page)
        self._stack.addWidget(history_scroll)

        # Esc 返回
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.activated.connect(self._go_home)

        # 布局完成后定初始尺寸（首页自然高度参与计算）
        self._apply_start_size()

        # ---- 定时器 ----
        self._poll = QTimer(self)
        self._poll.setInterval(300)
        self._poll.timeout.connect(self._drain_queue)
        self._poll.start()

        self._foot_timer = QTimer(self)
        self._foot_timer.setInterval(30_000)
        self._foot_timer.timeout.connect(self._request_footprint)
        self._foot_timer.start()

        # ---- 历史查询 worker ----
        self._jobs: "queue.Queue[object]" = queue.Queue()
        self._results: "queue.Queue[object]" = queue.Queue()
        self._worker = threading.Thread(
            target=self._history_worker, name="aegis-history-query", daemon=True
        )
        self._worker.start()

        monitor.subscribe(self._on_envelopes)

    # ------------------------------------------------------------------
    # 窗口尺寸：初始 → 恢复保存值（夹紧到当前工作区）→ 默认值 → 关闭时保存
    # ------------------------------------------------------------------

    def _apply_start_size(self) -> None:
        """优先恢复上次窗口尺寸；否则取默认宽度 + 首页内容自然高度（正常内容
        完整出现，不强求一屏显示）。恢复值/默认值都按当前可用工作区夹紧，
        不超出任务栏或屏幕边界。"""
        geo = None
        try:
            geo = QGuiApplication.primaryScreen().availableGeometry()
        except Exception:  # noqa: BLE001 - 无屏幕信息时退回默认
            geo = None
        max_w = max(theme.WINDOW_MIN[0], (geo.width() - 2 * _SCREEN_MARGIN) if geo else 4096)
        max_h = max(theme.WINDOW_MIN[1], (geo.height() - 2 * _SCREEN_MARGIN) if geo else 4096)

        saved = self._load_saved_window()
        if saved is not None:
            w = max(theme.WINDOW_MIN[0], min(int(saved["w"]), max_w))
            h = max(theme.WINDOW_MIN[1], min(int(saved["h"]), max_h))
        else:
            w = min(theme.WINDOW_DEFAULT[0], max_w)
            # 默认高度：不低于基准值，且足够容纳首页全部内容（无滚动完整出现；
            # 少量安全余量抵消字体度量在真实渲染时的细微差异）
            natural = self._home_page.widget().sizeHint().height()
            h = min(max(theme.WINDOW_DEFAULT[1], natural + theme.HEADER_HEIGHT + 12), max_h)
        self.resize(w, h)
        if saved is not None and geo is not None:
            # 跨屏恢复：把保存的位置限制回当前可用工作区
            x = int(saved.get("x", geo.left()))
            y = int(saved.get("y", geo.top()))
            x = max(geo.left(), min(x, geo.right() - self.width()))
            y = max(geo.top(), min(y, geo.bottom() - self.height()))
            self.move(x, y)

    @staticmethod
    def _load_saved_window() -> Optional[dict]:
        try:
            data = json.loads(_WINDOW_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and {"w", "h"} <= data.keys():
                return data
        except Exception:  # noqa: BLE001 - 缺失/损坏一律按无保存处理
            pass
        return None

    def _save_window_geometry(self) -> None:
        try:
            _WINDOW_FILE.parent.mkdir(parents=True, exist_ok=True)
            g = self.normalGeometry() if self.isMaximized() else self.geometry()
            _WINDOW_FILE.write_text(
                json.dumps({"w": g.width(), "h": g.height(),
                            "x": g.x(), "y": g.y()}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 保存失败不影响退出
            pass

    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(theme.HEADER_HEIGHT)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(theme.MARGIN_OUTER, 8, theme.MARGIN_OUTER, 8)
        lay.setSpacing(12)
        self._title_label = QLabel("Aegis Token Monitor")
        self._title_label.setFont(_font(theme.POINT_PAGE_TITLE, 600))
        self._title_label.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
        self._last_refresh = QLabel("")
        self._last_refresh.setFont(_font(theme.POINT_SECONDARY))
        self._last_refresh.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        self._summary = QLabel("正在读取…")
        self._summary.setFont(_font(theme.POINT_BODY))
        self._summary.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        self._refresh_btn = RefreshButton()
        self._refresh_btn.clicked.connect(self._on_refresh)
        lay.addWidget(self._title_label)
        lay.addStretch(1)
        lay.addWidget(self._last_refresh)
        lay.addSpacing(8)
        lay.addWidget(self._summary)
        lay.addWidget(self._refresh_btn)
        return header

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start_service(self) -> None:
        self._request_footprint()
        QTimer.singleShot(0, self._monitor.start)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        self._poll.stop()
        self._foot_timer.stop()
        self._save_window_geometry()
        self._monitor.shutdown(grace=1.5)
        try:
            self._history.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 服务回调 → UI 线程
    # ------------------------------------------------------------------

    def _on_envelopes(self, envelopes) -> None:
        self._ui_queue.put(("envelopes", envelopes))

    def _drain_queue(self) -> None:
        drained = False
        while True:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "envelopes":
                self._envelopes = payload
                self._render_cards()
                drained = True
            elif kind == "footprint":
                marks, text = payload
                self._footprint.update_marks(marks, text)
            elif kind == "history_result":
                self._history_page.show_result(*payload)
            elif kind == "backup_result":
                self._history_page.show_backup_result(*payload)
        if drained and self._stack.currentIndex() == 2:
            self._history_page.refresh_query()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _render_cards(self) -> None:
        now = datetime.now(tz=timezone.utc)
        from aegis.services.monitor import DEFAULT_TTLS as _T
        # 汇总口径 = 当前四个服务（含 DeepSeek），与四张卡一致
        vms = []
        for name in PROVIDER_ORDER:
            env = self._envelopes.get(name)
            if env is None:
                continue
            vm = build_provider_vm(env, now=now, ttl_seconds=_T.get(name, 300))
            vms.append(vm)
            if name == MONEY_PROVIDER:
                ttl_ds = _T.get(MONEY_PROVIDER, 1800)
                # 刷新时重读登记（用户可能刚编辑 config/topup.json）
                self._money_card.update_vm(
                    build_money_vm(env, now=now, ttl_seconds=ttl_ds, topup=self._topup),
                    status=vm.status,
                    status_text=vm.status_text,
                    loading=env.loading,
                    fetched_text=vm.fetched_text,
                    fetched_tooltip="",
                )
            else:
                self._cards[name].update_vm(vm)

        if vms:
            self._summary.setText(build_summary(vms))
        refreshing = any(v.loading for v in vms)
        self._refresh_btn.set_refreshing(refreshing)
        self._last_refresh.setText(
            "最近刷新 " + datetime.now().strftime("%H:%M:%S")
        )

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    # v0.4.1：首页卡片不再进入详情页（DetailPage 保留在代码中但无入口）；
    # Esc 仍从历史页返回首页。

    def _open_history(self) -> None:
        self._stack.setCurrentIndex(2)
        self._history_page.refresh_query()
        self._history_page._provider_combo.setFocus()

    def _go_home(self) -> None:
        if self._stack.currentIndex() != 0:
            self._stack.setCurrentIndex(0)

    def _on_refresh(self) -> None:
        # 重读手动登记（用户可能刚编辑 config/topup.json）
        self._topup = self._topup_mod.load_topup(MONEY_PROVIDER)
        self._monitor.refresh(manual=True)

    def _on_retry(self, provider: str) -> None:
        self._monitor.refresh(provider, manual=True)

    # ------------------------------------------------------------------
    # 足迹 / 历史（worker 查询；继承 v0.2）
    # ------------------------------------------------------------------

    def _request_footprint(self) -> None:
        if self._closed:
            return
        self._jobs.put(("footprint", None))

    def _on_history_query(self, provider: str, payload: object) -> None:
        self._jobs.put(("series", (provider, payload)))

    def _on_backup(self, path: str) -> None:
        self._jobs.put(("backup", path))

    def _history_worker(self) -> None:
        while not self._closed:
            try:
                kind, payload = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "footprint":
                    self._do_footprint()
                elif kind == "series":
                    provider, (kind_w, days) = payload
                    self._do_series(provider, kind_w, days)
                elif kind == "backup":
                    self._do_backup(payload)
            except Exception as e:  # noqa: BLE001
                logger.warning("history worker failed: %s", type(e).__name__)

    def _do_footprint(self) -> None:
        from aegis.history.store import HistoryUnavailable
        now = datetime.now(tz=timezone.utc)
        marks = ["none"] * 7
        try:
            all_marks: List[str] = ["none"] * 7
            any_obs = False
            for name in PROVIDER_ORDER:
                obs = self._history.query_observations(name, now - timedelta(days=8), now)
                if obs:
                    any_obs = True
                fp = footprint_7d(obs, now)
                for i, d in enumerate(fp):
                    if d.kind == "confirmed_increase":
                        all_marks[i] = "confirmed_increase"
                    elif d.kind == "observed" and all_marks[i] == "none":
                        all_marks[i] = "observed"
            marks = all_marks
            if any(m == "confirmed_increase" for m in marks):
                text = "观察到额度变化"
            elif any(m == "observed" for m in marks):
                text = "有观测记录，未确认额度增加"
            else:
                text = "尚未开始记录"
        except HistoryUnavailable:
            text = "历史暂未保存（数据库不可用）"
        self._ui_queue.put(("footprint", (marks, text)))

    def _do_series(self, provider: str, window_kind: str, days: int) -> None:
        from aegis.history.store import HistoryUnavailable
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(days=days)
        try:
            obs = self._history.query_observations(provider, start, now)
        except HistoryUnavailable:
            self._ui_queue.put(("history_result", ("历史暂未保存（数据库不可用）", [], [], "")))
            return
        if not obs:
            self._ui_queue.put((
                "history_result",
                (f"最近 {days} 日无观测记录（仅记录工具运行期间的成功采集）", [], [], ""),
            ))
            return

        covered_days = {o.fetched_at_utc[:10] for o in obs}
        coverage = f"已记录 {len(covered_days)}/{days} 天（仅记录工具运行期间的成功采集）"
        points: List = []
        breaks: List[bool] = []
        from aegis.cache import _parse_utc_iso

        if window_kind == "balance":
            values = [o.balance_value for o in obs if o.balance_value is not None]
            if not values:
                self._ui_queue.put(("history_result", (coverage, [], [], "")))
                return
            vmin, vmax = min(values), max(values)
            note = f"余额（币种 {obs[0].currency or '?'}）· 独立金额轴 {vmin:.2f}–{vmax:.2f} · 最近 {days} 日"
            scale = (vmax - vmin) or 1.0
            for o in obs:
                if o.balance_value is None:
                    continue
                dt = _parse_utc_iso(o.fetched_at_utc)
                val = (o.balance_value - vmin) / scale * 100.0
                points.append((dt, val))
                breaks.append(False)
        else:
            prefix = "five" if window_kind == "five" else "weekly"
            note = f"{'5h' if prefix == 'five' else 'Weekly'} 窗口剩余百分比（0–100%）· 最近 {days} 日"
            prev_key = None
            for o in obs:
                dt = _parse_utc_iso(o.fetched_at_utc)
                val = getattr(o, f"{prefix}_remaining")
                resets = getattr(o, f"{prefix}_resets_at")
                wmin = getattr(o, f"{prefix}_window_minutes")
                key = (resets, wmin)
                brk = False
                if prev_key is not None and key != prev_key:
                    brk = True
                if points and (dt - points[-1][0]).total_seconds() > 2 * 300:
                    brk = True
                points.append((dt, val))
                breaks.append(brk)
                prev_key = key
        self._ui_queue.put(("history_result", (coverage, points, breaks, note)))

    def _do_backup(self, path: str) -> None:
        from aegis.history.store import HistoryUnavailable
        try:
            ok = self._history.backup(path)
            detail = f"目标：{path}"
            verified = ok and self._history.verify_backup(path)
            self._ui_queue.put(("backup_result", (bool(verified), detail)))
        except Exception as e:  # noqa: BLE001
            self._ui_queue.put(("backup_result", (False, f"{type(e).__name__}")))
