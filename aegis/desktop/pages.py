"""详情页与历史页（设计 §13 pages.py；同窗口导航，Esc 返回）。

历史页图表（DESIGN.md §8）：额度 0–100% 轴、同 Provider 同窗口绘制、
Reset 与缺测断线、禁平滑曲线；余额独立金额轴；附近标注指标 / 单位 /
时间范围；悬停给实际值与采集时间。Token 累计区显示"尚未接入"（闸门未通过）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aegis.desktop import theme
from aegis.desktop.adapter import ProviderVM
from aegis.desktop.widgets import _font

_TTLS = {"codex": 300, "zai": 300, "minimax": 300, "deepseek": 1800}


def _back_hint_label() -> QLabel:
    lbl = QLabel("Esc 返回")
    lbl.setFont(_font(theme.POINT_SECONDARY))
    lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
    return lbl


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(_font(theme.POINT_CARD_TITLE, 600))
    lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
    return lbl


def _body_label(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(_font(theme.POINT_BODY))
    lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
    return lbl


def _secondary_label(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(_font(theme.POINT_SECONDARY))
    lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
    lbl.setWordWrap(True)
    return lbl


# ---------------------------------------------------------------------------
# 详情页
# ---------------------------------------------------------------------------

class DetailPage(QWidget):
    back_requested = Signal()
    retry_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.MARGIN_OUTER, theme.MARGIN_OUTER, theme.MARGIN_OUTER, theme.MARGIN_OUTER)
        root.setSpacing(12)

        head = QHBoxLayout()
        self._title = _section_label("详情")
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(_back_hint_label())
        root.addLayout(head)

        self._status_line = _body_label()
        root.addWidget(self._status_line)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self._rows = {}
        for i, key in enumerate(
            ("five", "five_reset", "weekly", "weekly_reset",
             "source_updated", "fetched", "attempt", "error")
        ):
            cap = {
                "five": "5h 窗口", "five_reset": "5h 重置",
                "weekly": "Weekly 窗口", "weekly_reset": "Weekly 重置",
                "source_updated": "源数据时刻", "fetched": "最近成功采集",
                "attempt": "最近尝试", "error": "最近错误",
            }[key]
            k = _secondary_label(cap)
            v = _body_label("—")
            v.setWordWrap(True)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self._rows[key] = v
        root.addLayout(grid)

        self._extras_title = _section_label("附加窗口（Codex）")
        root.addWidget(self._extras_title)
        self._extras_box = QVBoxLayout()
        root.addLayout(self._extras_box)

        note = _secondary_label(
            "“已更新”指数据获取成功，不代表模型推理服务经过健康检测。"
            "余额变化不等于 Token 消耗或实际费用。"
        )
        root.addWidget(note)
        root.addStretch(1)

        foot = QHBoxLayout()
        self._retry = QPushButton("重试本项")
        self._retry.setFont(_font(theme.POINT_BODY, 500))
        self._retry.setMinimumSize(88, 32)
        self._retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry.setStyleSheet(
            f"QPushButton {{ background:{theme.ACTION_SURFACE}; color:{theme.ACCENT};"
            f" border:1px solid {theme.CONTROL_BORDER}; border-radius:{theme.RADIUS_BUTTON}px;"
            f" padding:4px 16px; }} QPushButton:focus {{ border:2px solid {theme.ACCENT}; }}"
        )
        self._retry.clicked.connect(self._emit_retry)
        foot.addWidget(self._retry)
        foot.addStretch(1)
        root.addLayout(foot)

    def _emit_retry(self) -> None:
        if self._provider:
            self.retry_requested.emit(self._provider)

    def show_provider(self, provider: str, vm: ProviderVM, env, now: datetime) -> None:
        """env 为 SnapshotEnvelope（读取时间信封原始值）。"""
        self._provider = provider
        self._title.setText(f"详情 · {vm.display_name}")
        self._status_line.setText(f"状态：{vm.status_text}" + ("（刷新中）" if vm.loading else ""))

        def _win_text(w) -> str:
            if w is None:
                return "不提供"
            return f"已用 {w.used_text} · 剩余 {w.remaining_text}"

        five = vm.five
        weekly = vm.weekly
        self._rows["five"].setText(_win_text(five))
        self._rows["weekly"].setText(_win_text(weekly))
        self._rows["five_reset"].setText(
            five.reset_text + (("　" + five.reset_tooltip) if five.reset_tooltip else "")
            if five else "—"
        )
        self._rows["weekly_reset"].setText(
            weekly.reset_text + (("　" + weekly.reset_tooltip) if weekly.reset_tooltip else "")
            if weekly else "—"
        )

        t = env.time
        self._rows["source_updated"].setText(
            self._fmt_utc(t.source_updated_at) or "源未提供（不做本地时间填充）"
        )
        self._rows["fetched"].setText(self._fmt_utc(t.last_success_fetched_at) or "尚未成功更新")
        self._rows["attempt"].setText(self._fmt_utc(t.last_attempt_at) or "—")
        self._rows["error"].setText(vm.error_text or "—")

        # extras（白名单：id / title / used%）
        while self._extras_box.count():
            item = self._extras_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        has_extras = bool(vm.extras)
        self._extras_title.setVisible(has_extras)
        for ex in vm.extras:
            lbl = _body_label(f"{ex.id}（{ex.title}）· 已用 {ex.used_percent_text}")
            self._extras_box.addWidget(lbl)

    @staticmethod
    def _fmt_utc(iso: Optional[str]) -> str:
        if not iso:
            return ""
        from aegis.cache import _parse_utc_iso
        try:
            local = _parse_utc_iso(iso).astimezone()
        except Exception:  # noqa: BLE001
            return ""
        return local.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 历史图表（QPainter 折线；0-100% 或余额独立轴）
# ---------------------------------------------------------------------------

class QuotaChart(QWidget):
    """单 Provider 单窗口折线。缺测 / Reset 变化断线，不平滑、不补零。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._points: List[Tuple[datetime, Optional[float]]] = []  # (utc, remaining%)
        self._breaks: List[bool] = []          # 与 _points 对齐：该点与前点之间是否断线
        self._minimum_size = (400, 160)
        self.setMinimumSize(*self._minimum_size)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._color = theme.ACCENT
        self._axis_note = ""

    def set_series(self, points: List[Tuple[datetime, Optional[float]]],
                   breaks: List[bool], color: str, axis_note: str) -> None:
        self._points = points
        self._breaks = breaks
        self._color = color
        self._axis_note = axis_note
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 36, 12, 10, 18
        # 背景
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.SURFACE))
        p.drawRoundedRect(self.rect(), theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)
        # 0/100 轴线（额度模式）或 min/max（余额模式）
        p.setPen(QPen(QColor(theme.DIVIDER), 1))
        p.drawLine(pad_l, pad_t, pad_l, h - pad_b)
        p.drawLine(pad_l, h - pad_b, w - pad_r, h - pad_b)
        p.setFont(_font(theme.POINT_SECONDARY))
        p.setPen(QPen(QColor(theme.TEXT_SECONDARY), 1))
        p.drawText(QRect(0, pad_t - 6, pad_l - 4, 12), Qt.AlignmentFlag.AlignRight, "100%")
        p.drawText(QRect(0, h - pad_b - 6, pad_l - 4, 12), Qt.AlignmentFlag.AlignRight, "0%")
        if self._axis_note:
            p.drawText(QRect(pad_l + 4, h - pad_b - 4, w - pad_l - pad_r, 14),
                       Qt.AlignmentFlag.AlignLeft, self._axis_note)
        if len(self._points) < 2:
            p.setPen(QPen(QColor(theme.TEXT_SECONDARY), 1))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无可绘制的观测")
            return

        t0 = self._points[0][0]
        t1 = self._points[-1][0]
        span = max(1.0, (t1 - t0).total_seconds())

        def xy(i: int, val: float) -> QPointF:
            dt, _ = self._points[i]
            x = pad_l + (w - pad_l - pad_r) * ((dt - t0).total_seconds() / span)
            y = (h - pad_b) - (h - pad_t - pad_b) * (max(0.0, min(100.0, val)) / 100.0)
            return QPointF(x, y)

        pen = QPen(QColor(self._color), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        prev: Optional[QPointF] = None
        for i, (_, val) in enumerate(self._points):
            if val is None:
                prev = None
                continue
            cur = xy(i, val)
            if prev is not None and not (i < len(self._breaks) and self._breaks[i]):
                p.drawLine(prev, cur)
            prev = cur

    def _nearest(self, x: int) -> Optional[Tuple[datetime, float]]:
        if not self._points:
            return None
        best = None
        best_dx = 1e18
        for dt, val in self._points:
            if val is None:
                continue
            t0 = self._points[0][0]
            t1 = self._points[-1][0]
            span = max(1.0, (t1 - t0).total_seconds())
            pad_l, pad_r = 36, 12
            px = pad_l + (self.width() - pad_l - pad_r) * ((dt - t0).total_seconds() / span)
            dx = abs(px - x)
            if dx < best_dx:
                best_dx = dx
                best = (dt, val)
        return best

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._nearest(event.position().toPoint().x())
        if hit:
            dt, val = hit
            local = dt.astimezone()
            self.setToolTip(f"{val:.1f}%\n采集于 {local.strftime('%m-%d %H:%M:%S')}")
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.back_requested_emit()
            return
        super().keyPressEvent(event)

    def back_requested_emit(self) -> None:  # pragma: no cover - 由页面接管
        pass


# ---------------------------------------------------------------------------
# 历史页
# ---------------------------------------------------------------------------

class HistoryPage(QWidget):
    back_requested = Signal()
    # (kind, payload) 查询请求 → app 层转 worker
    query_requested = Signal(str, object)
    backup_requested = Signal(str)

    WINDOW_KINDS = [("5h", "five"), ("Weekly", "weekly"), ("余额", "balance")]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.MARGIN_OUTER, theme.MARGIN_OUTER, theme.MARGIN_OUTER, theme.MARGIN_OUTER)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        self._title = _section_label("历史")
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(_back_hint_label())
        root.addLayout(head)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        self._provider_combo = QComboBox()
        self._provider_combo.addItems(["Codex", "z.ai / GLM", "MiniMax", "DeepSeek"])
        self._days_combo = QComboBox()
        self._days_combo.addItems(["最近 7 日", "最近 30 日"])
        self._kind_combo = QComboBox()
        self._kind_combo.addItems([k for k, _ in self.WINDOW_KINDS])
        for w in (self._provider_combo, self._days_combo, self._kind_combo):
            w.setFont(_font(theme.POINT_BODY))
            w.setStyleSheet(
                f"QComboBox {{ background:{theme.SURFACE}; color:{theme.TEXT_PRIMARY};"
                f" border:1px solid {theme.CONTROL_BORDER}; border-radius:{theme.RADIUS_CONTROL}px;"
                f" padding:4px 8px; min-height:24px; }}"
                f"QComboBox:focus {{ border:2px solid {theme.ACCENT}; }}"
                f"QComboBox QAbstractItemView {{ background:{theme.SURFACE_HOVER};"
                f" color:{theme.TEXT_PRIMARY}; selection-background-color:{theme.ACTION_SURFACE}; }}"
            )
            controls.addWidget(w)
        self._backup_btn = QPushButton("备份历史…")
        self._backup_btn.setFont(_font(theme.POINT_BODY, 500))
        self._backup_btn.setMinimumHeight(32)
        self._backup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._backup_btn.setStyleSheet(
            f"QPushButton {{ background:{theme.ACTION_SURFACE}; color:{theme.ACCENT};"
            f" border:1px solid {theme.CONTROL_BORDER}; border-radius:{theme.RADIUS_BUTTON}px;"
            f" padding:4px 16px; }} QPushButton:focus {{ border:2px solid {theme.ACCENT}; }}"
        )
        controls.addWidget(self._backup_btn)
        controls.addStretch(1)
        root.addLayout(controls)

        self._coverage = _secondary_label()
        root.addWidget(self._coverage)

        self._chart = QuotaChart()
        root.addWidget(self._chart, 1)

        self._token_note = _secondary_label(
            "Token 统计尚未接入（数据源口径闸门未通过，见 TOKEN_SOURCE_CAPABILITY.md）。"
            "额度百分比不是 Token 数；本页不显示任何 Token 累计。"
        )
        root.addWidget(self._token_note)

        # 信号
        for w in (self._provider_combo, self._days_combo, self._kind_combo):
            w.currentIndexChanged.connect(self._emit_query)
        self._backup_btn.clicked.connect(self._on_backup)

    # ---- 属性 ----

    @property
    def provider(self) -> str:
        return ["codex", "zai", "minimax", "deepseek"][self._provider_combo.currentIndex()]

    @property
    def days(self) -> int:
        return 7 if self._days_combo.currentIndex() == 0 else 30

    @property
    def window_kind(self) -> str:
        return self.WINDOW_KINDS[self._kind_combo.currentIndex()][1]

    # ---- 查询 ----

    def _emit_query(self, *_a) -> None:
        self.query_requested.emit(self.provider, (self.window_kind, self.days))

    def refresh_query(self) -> None:
        self._emit_query()

    def show_result(
        self,
        coverage_text: str,
        points: List[Tuple[datetime, Optional[float]]],
        breaks: List[bool],
        axis_note: str,
    ) -> None:
        self._coverage.setText(coverage_text)
        color = theme.CHART_CATEGORY.get(
            ["codex", "zai", "minimax", "deepseek"][self._provider_combo.currentIndex()],
            theme.ACCENT,
        )
        self._chart.set_series(points, breaks, color, axis_note)

    def _on_backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "备份历史数据库", "aegis_history_backup.sqlite3",
            "SQLite 数据库 (*.sqlite3 *.db)",
        )
        if path:
            self.backup_requested.emit(path)

    def show_backup_result(self, ok: bool, detail: str) -> None:
        if ok:
            QMessageBox.information(self, "备份完成", f"备份成功并通过 quick_check 验证。\n{detail}")
        else:
            QMessageBox.warning(self, "备份失败", f"备份未完成或验证失败。\n{detail}")
