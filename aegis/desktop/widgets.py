"""v0.4 服务卡（纵向四行宽卡，安静用量仪表盘）。

v0.4 视觉规则（docs/UI_POLISH_CC_BRIEF.md 视觉合同）：
- **进度条填充始终表示已用**；轨道未填充=剩余。0% 不画最小假填充，100% 填满。
- 卡片取内容自然高度（不参与纵向 stretch）；窗口多余空间留在内容末尾。
- 每个额度区顺序：周期标签（左）+ 已用值（右）→ 细进度条 → 重置时间；
  两个额度区等宽。内容宽不足时上下排列，改向后自然高度重算。
- 界面统一 Segoe UI（含中文回退），数字启用等宽数字（tnum）；“暂无数据”
  “待登记”等文字用正文样式，不进大号数字路径。
- 卡片圆角 12、弱边线、无霓虹/装饰渐变/持续动画；错误用柔和红并伴随文字。
- 键盘焦点 2px 强调色外框；“详情 ›”与整卡同一操作，链接色与填充蓝分工。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aegis.desktop import theme
from aegis.desktop.adapter import (
    ST_CACHED,
    ST_CACHED_EXPIRED,
    ST_ERROR,
    ST_LIVE,
    ST_PARTIAL,
    ST_UNAVAILABLE,
    MoneyVM,
    ProviderVM,
    WindowVM,
)


def _font(pt: float, weight: int = 400, tabular: bool = False) -> QFont:
    """统一字体：Segoe UI + YaHei UI 回退；tabular=True 时启用等宽数字。"""
    f = QFont()
    f.setFamilies([theme.FONT_FAMILY, theme.FONT_FAMILY_FALLBACK])
    f.setPointSizeF(pt)
    f.setWeight(QFont.Weight.Normal if weight < 500 else
                QFont.Weight.DemiBold if weight >= 600 else QFont.Weight.Medium)
    if tabular:
        try:
            f.setFeature(QFont.Tag("tnum"), 1)
        except Exception:  # noqa: BLE001 - 旧版 Qt 无此特性时静默退回默认数字
            pass
    return f


def _color(name: str) -> QColor:
    return QColor(name)


# ---------------------------------------------------------------------------
# 状态图形（形状 + 颜色 + 文字说明；沿用 v0.2 合同）
# ---------------------------------------------------------------------------

class StatusChip(QWidget):
    """16×16 状态图形：实心点 / 时钟 / 半圆 / 短横 / 警示三角。颜色只是辅助，
    状态始终同时有文字说明（卡头右侧）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._status = ST_UNAVAILABLE
        self.setFixedSize(16, 16)

    def set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self._status
        rect = QRectF(2, 2, 12, 12)
        if c == ST_LIVE:
            p.setBrush(_color(theme.SUCCESS))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(4, 4, 8, 8))
        elif c in (ST_CACHED, ST_CACHED_EXPIRED):
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_color(theme.TEXT_SECONDARY), 1.4))
            p.drawEllipse(rect.adjusted(1, 1, -1, -1))
            p.drawLine(QRectF(8, 8, 0, 0).center(), QRectF(8, 5, 0, 0).center())
            p.drawLine(QRectF(8, 8, 0, 0).center(), QRectF(10, 9, 0, 0).center())
        elif c == ST_PARTIAL:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_color(theme.WARNING))
            p.drawPie(rect.adjusted(1, 1, -1, -1), 90 * 16, 180 * 16)
            p.setPen(QPen(_color(theme.WARNING), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(rect.adjusted(1, 1, -1, -1))
        elif c == ST_ERROR:
            p.setPen(QPen(_color(theme.ERROR), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            center = rect.center()
            p.drawPolygon(QPolygonF([
                QPointF(center.x(), center.y() - 6),
                QPointF(center.x() - 6, center.y() + 5),
                QPointF(center.x() + 6, center.y() + 5),
            ]))
        else:
            p.setPen(QPen(_color(theme.DIVIDER), 2.4))
            p.drawLine(4, 8, 12, 8)


# ---------------------------------------------------------------------------
# 已用进度条（核心语义：填充 = 已用；颜色随用量连续变化）
# ---------------------------------------------------------------------------

def _usage_color(percent: float) -> QColor:
    """已用百分比 → 填充色：在 theme.USAGE_COLOR_ANCHORS 锚点间线性插值。

    v0.4.2 极光渐变：0% 翡翠绿 → 50% 亮蓝 → 75% 亮紫 → 100% 珊瑚红。
    锚点均取鲜艳色且相邻色相相邻过渡，RGB 插值中段保持饱和、无灰色段；
    用量增加时颜色平滑变化，不到阈值突然换色。纯视觉映射，不承担告警语义。
    """
    anchors = theme.USAGE_COLOR_ANCHORS
    x = max(0.0, min(100.0, percent))
    for (p0, c0), (p1, c1) in zip(anchors, anchors[1:]):
        if x <= p1:
            t = 0.0 if p1 == p0 else (x - p0) / (p1 - p0)
            a, b = _color(c0), _color(c1)
            return QColor(
                int(round(a.red() + (b.red() - a.red()) * t)),
                int(round(a.green() + (b.green() - a.green()) * t)),
                int(round(a.blue() + (b.blue() - a.blue()) * t)),
            )
    return _color(anchors[-1][1])


class UsageBar(QWidget):
    """胶囊形已用条：填充长度=已用百分比，填充颜色随用量平滑变化。

    0% 无填充；小比例按真实比例绘制、不因圆角强行放大；100% 完整填满且
    不越界；无数据不画填充（由上层决定显隐）。低额度文案提示由上层负责，
    不靠颜色单独传递信息。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fill: Optional[float] = None   # 0–100 已用比例；None = 无数据
        self.setFixedHeight(theme.BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_used(self, fill_percent: Optional[float], warning: bool = False) -> None:
        # warning 参数保留兼容旧调用；颜色现由百分比连续映射决定，
        # 低额度提示以文字呈现（业务告警规则不变）。
        self._fill = fill_percent
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = h / 2.0   # 胶囊圆角
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_color(theme.TRACK))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)
        if self._fill is None:
            return
        frac = max(0.0, min(100.0, self._fill)) / 100.0
        fill_w = w * frac
        if fill_w <= 0:
            return   # 0% 不画最小假填充
        # 100% 时完整填满并收敛到轨道圆角内，不越界
        p.setBrush(_usage_color(self._fill))
        p.drawRoundedRect(QRectF(0, 0, fill_w, h), r, r)


# ---------------------------------------------------------------------------
# 单列额度模块（标签+已用值 → 进度条 → 重置时间）
# ---------------------------------------------------------------------------

class _UsageColumn(QWidget):
    """“五小时额度”或“本周额度”的一列。周期标签在左、已用值在右。"""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.COLUMN_ROW_SPACING)

        # 行 1：周期标签（左） + 已用值（右）
        head = QHBoxLayout()
        head.setSpacing(8)
        self._title = QLabel(title)
        self._title.setFont(_font(theme.POINT_SECTION))
        self._title.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        head.addWidget(self._title)
        head.addStretch(1)
        self._number = QLabel(theme.NO_DATA_TEXT)
        self._number.setFont(_font(theme.POINT_MAIN_NUMBER, 600, tabular=True))
        self._number.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
        self._suffix = QLabel("已用")
        self._suffix.setFont(_font(theme.POINT_BODY))
        self._suffix.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        head.addWidget(self._number)
        head.addWidget(self._suffix, 0, Qt.AlignmentFlag.AlignBaseline)
        lay.addLayout(head)

        # 行 2：细进度条
        self._bar = UsageBar()
        lay.addWidget(self._bar)

        # 行 3：重置时间（弱于额度值，但保持可读对比度）
        self._reset = QLabel()
        self._reset.setFont(_font(theme.POINT_SECONDARY))
        self._reset.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        self._reset.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._reset)

        # 行 4：低额度备注
        self._note = QLabel()
        self._note.setFont(_font(theme.POINT_SECONDARY))
        self._note.setStyleSheet(f"color:{theme.WARNING};")
        self._note.hide()
        lay.addWidget(self._note)

    def update_vm(self, vm: Optional[WindowVM], *, title: str) -> None:
        self._title.setText(title)
        if vm is None or vm.used_value is None:
            # 缺失：暂无数据（正文样式，不进大号数字路径），不显示 0%、不画空条
            self._number.setText(theme.NO_DATA_TEXT)
            self._number.setFont(_font(theme.POINT_BODY, 500))
            self._number.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
            self._suffix.hide()
            self._bar.setVisible(False)
            self._bar.set_used(None)
            self._reset.setText("重置时间未提供")
            self._reset.setToolTip("")
            self._note.hide()
            return
        self._number.setText(vm.used_text)
        self._number.setFont(_font(theme.POINT_MAIN_NUMBER, 600, tabular=True))
        self._number.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
        self._suffix.show()
        self._bar.setVisible(True)
        self._bar.set_used(vm.used_value, vm.low_remaining)
        self._reset.setText(f"重置：{vm.reset_text}")
        self._reset.setToolTip(vm.reset_tooltip)
        if vm.low_remaining:
            self._note.setText("额度较低（剩余不足 10%）")
            self._note.show()
        else:
            self._note.hide()


# ---------------------------------------------------------------------------
# 服务宽卡（前三家：Codex / z.ai GLM / MiniMax）
# ---------------------------------------------------------------------------

class ServiceCardRow(QFrame):
    """一整行服务卡：头部（名称+状态）→ 两列额度模块。自然高度。

    v0.4.1：卡片纯展示——无"详情 ›"入口、整卡不可点击、无手形光标与
    焦点描边，只保留安静细边框；键盘焦点留给刷新 / 历史等实际按钮。
    """

    def __init__(self, provider: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider = provider
        # 高度按内容自然收缩/增长（Preferred）；窗口多余空间不进卡片
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.CARD_PADDING, theme.CARD_PADDING,
                                theme.CARD_PADDING, theme.CARD_PADDING)
        root.setSpacing(theme.CARD_INNER_GAP)

        # 头部：服务名（左） + 状态（右）。
        # 正常状态不重复“已更新”与“采集刚刚”：刚更新时只显示状态，完整时间在 tooltip。
        head = QHBoxLayout()
        head.setSpacing(8)
        self._name = QLabel()
        self._name.setFont(_font(theme.POINT_CARD_TITLE, 600))
        self._name.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
        self._status_chip = StatusChip()
        self._status_text = QLabel()
        self._status_text.setFont(_font(theme.POINT_BODY, 500))
        self._status_text.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        self._fetched = QLabel()
        self._fetched.setFont(_font(theme.POINT_SECONDARY))
        self._fetched.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        head.addWidget(self._name)
        head.addStretch(1)
        head.addWidget(self._status_chip)
        head.addWidget(self._status_text)
        head.addSpacing(10)
        head.addWidget(self._fetched)
        root.addLayout(head)

        # 两列额度（内容宽不足时自动上下排列，高度随方向重算）
        self._columns_host = QWidget()
        self._columns_lay = QBoxLayout(QBoxLayout.Direction.LeftToRight, self._columns_host)
        self._columns_lay.setContentsMargins(0, 0, 0, 0)
        self._columns_lay.setSpacing(theme.COLUMN_GAP)
        self._col_five = _UsageColumn("五小时额度")
        self._col_weekly = _UsageColumn("本周额度")
        self._columns_lay.addWidget(self._col_five, 1)
        self._columns_lay.addWidget(self._col_weekly, 1)
        root.addWidget(self._columns_host)

        # 卡级备注（错误 / 部分数据；长中文可换行，卡片随之增高）
        self._note = QLabel()
        self._note.setFont(_font(theme.POINT_SECONDARY))
        self._note.setWordWrap(True)
        self._note.hide()
        root.addWidget(self._note)

    def update_vm(self, vm: ProviderVM) -> None:
        self._name.setText(vm.display_name)
        if vm.loading:
            self._status_text.setText(theme.STATUS_TEXT["REFRESHING"])
            self._status_text.setStyleSheet(f"color:{theme.ACCENT};")
        else:
            self._status_text.setText(vm.status_text)
            color = theme.TEXT_SECONDARY
            if vm.status == ST_ERROR or vm.status == ST_UNAVAILABLE:
                color = theme.ERROR
            elif vm.status == ST_PARTIAL:
                color = theme.WARNING
            self._status_text.setStyleSheet(f"color:{color};")
        self._status_chip.set_status(vm.status)
        self._fetched.setText(vm.fetched_text)
        self._fetched.setToolTip(vm.fetched_tooltip)
        # 正常已更新 + “采集刚刚”语义重复 → 隐藏文字，完整时间保留在 tooltip
        self._fetched.setVisible(not (vm.status == ST_LIVE and vm.fetched_text == "采集刚刚"))

        # 每次渲染时同步一次列方向（隐藏控件收不到 resizeEvent，双保险）
        self._update_column_direction()

        self._col_five.update_vm(vm.five, title="五小时额度")
        if vm.weekly is not None:
            self._col_weekly.update_vm(vm.weekly, title="本周额度")
        elif vm.five is not None:
            # 该服务不提供周额度：明确说明，不复制五小时值
            self._col_weekly.update_vm(None, title="本周额度")
            self._col_weekly._reset.setText("该来源未提供周额度")
        else:
            self._col_weekly.update_vm(None, title="本周额度")

        if vm.error_text:
            self._note.setText(vm.error_text)
            self._note.setStyleSheet(f"color:{theme.ERROR};")
            self._note.show()
        elif vm.partial_tooltip:
            self._note.setText("部分数据：" + vm.partial_tooltip)
            self._note.setStyleSheet(f"color:{theme.WARNING};")
            self._note.show()
        else:
            self._note.hide()

    # ---- 窄卡两列上下排列 ----

    def _update_column_direction(self) -> None:
        content = self.width() - 2 * theme.CARD_PADDING
        want_vertical = 0 < content < theme.COLUMN_STACK_THRESHOLD
        cur_vertical = self._columns_lay.direction() == QBoxLayout.Direction.TopToBottom
        if want_vertical != cur_vertical:
            self._columns_lay.setDirection(
                QBoxLayout.Direction.TopToBottom if want_vertical
                else QBoxLayout.Direction.LeftToRight
            )
            self._columns_host.updateGeometry()   # 方向改变后重算自然高度

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._update_column_direction()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        # 安静卡片：纯色表面 + 细边框；无悬停高亮、无焦点描边
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = float(theme.RADIUS_CARD)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_color(theme.SURFACE))
        p.drawRoundedRect(self.rect(), radius, radius)
        p.setPen(QPen(_color(theme.DIVIDER), theme.BORDER_WIDTH))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), radius, radius)


# ---------------------------------------------------------------------------
# DeepSeek 钱包卡（v0.4.1：钱包剩余 + 已花费，简洁两金额）
# ---------------------------------------------------------------------------

class MoneyCardRow(QFrame):
    """DeepSeek 钱包卡：服务名 + 简短状态 + 并排两个金额。

    口径（adapter.build_money_vm）：
    - 钱包剩余 = 官方 balance 当前余额（真实值）。
    - 已花费：上游无消费累计字段；仅当手动登记充值与余额口径一致时按
      累计充值 − 余额 计算，否则显示"已花费 —"，口径说明放悬停提示，
      不在首页长期堆配置说明。无额度条、无充值说明文本。
    """

    def __init__(self, provider: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.CARD_PADDING, theme.CARD_PADDING,
                                theme.CARD_PADDING, theme.CARD_PADDING)
        root.setSpacing(theme.CARD_INNER_GAP)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._name = QLabel("DeepSeek")
        self._name.setFont(_font(theme.POINT_CARD_TITLE, 600))
        self._name.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
        self._status_chip = StatusChip()
        self._status_text = QLabel()
        self._status_text.setFont(_font(theme.POINT_BODY, 500))
        self._status_text.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        head.addWidget(self._name)
        head.addStretch(1)
        head.addWidget(self._status_chip)
        head.addWidget(self._status_text)
        root.addLayout(head)

        # 并排两个金额：钱包剩余 / 已花费
        amounts = QHBoxLayout()
        amounts.setSpacing(theme.COLUMN_GAP)
        self._labels = {}
        for key, title in (("balance", "钱包剩余"), ("spent", "已花费")):
            box = QVBoxLayout()
            box.setSpacing(2)
            t = QLabel(title)
            t.setFont(_font(theme.POINT_SECTION))
            t.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
            v = QLabel("—")
            v.setFont(_font(theme.POINT_BALANCE, 600, tabular=True))
            v.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
            box.addWidget(t)
            box.addWidget(v)
            amounts.addLayout(box)
            self._labels[key] = v
        amounts.addStretch(1)
        root.addLayout(amounts)

    def update_vm(self, vm: MoneyVM, status: str, status_text: str,
                  loading: bool, fetched_text: str, fetched_tooltip: str) -> None:
        # 简短更新状态；真实采集时间放 tooltip（透明但不占版面）
        if loading:
            self._status_text.setText(theme.STATUS_TEXT["REFRESHING"])
            self._status_text.setStyleSheet(f"color:{theme.ACCENT};")
        else:
            self._status_text.setText(status_text)
            color = theme.TEXT_SECONDARY
            if status in (ST_ERROR, ST_UNAVAILABLE):
                color = theme.ERROR
            elif status == ST_PARTIAL:
                color = theme.WARNING
            self._status_text.setStyleSheet(f"color:{color};")
        tip = fetched_tooltip or fetched_text
        if vm.balance_time_text:
            tip = f"{tip}\n{vm.balance_time_text}" if tip else vm.balance_time_text
        self._status_text.setToolTip(tip)
        self._status_chip.set_status(status)

        # 钱包剩余：有真实金额才走大号数字路径
        balance_label = self._labels["balance"]
        has_value = vm.balance_text not in ("—", theme.NO_DATA_TEXT)
        if has_value:
            balance_label.setText(vm.balance_text)
            balance_label.setFont(_font(theme.POINT_BALANCE, 600, tabular=True))
            balance_label.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
        else:
            balance_label.setText(theme.NO_DATA_TEXT)
            balance_label.setFont(_font(theme.POINT_BODY, 500))
            balance_label.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")

        # 已花费：可靠口径才显示金额；无依据显示 "—"，绝不编造或填零
        spent_label = self._labels["spent"]
        if vm.spent_text == "—":
            spent_label.setText("—")
            spent_label.setFont(_font(theme.POINT_BALANCE, 600, tabular=True))
            spent_label.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
            spent_label.setToolTip(vm.spent_hint)
        else:
            spent_label.setText(vm.spent_text)
            spent_label.setFont(_font(theme.POINT_BALANCE, 600, tabular=True))
            spent_label.setStyleSheet(f"color:{theme.TEXT_PRIMARY};")
            spent_label.setToolTip("")

    def paintEvent(self, event) -> None:  # noqa: N802
        # 与 ServiceCardRow 一致的安静卡片
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = float(theme.RADIUS_CARD)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_color(theme.SURFACE))
        p.drawRoundedRect(self.rect(), radius, radius)
        p.setPen(QPen(_color(theme.DIVIDER), theme.BORDER_WIDTH))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), radius, radius)


# ---------------------------------------------------------------------------
# 额度观察记录（视觉弱于用量主体；沿用 v0.2 保守规则）
# ---------------------------------------------------------------------------

class FootprintStrip(QWidget):
    history_requested = Signal()

    KIND_FILLED = "confirmed_increase"
    KIND_HOLLOW = "observed"
    KIND_NONE = "none"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._marks: List[str] = [self.KIND_NONE] * 7

        row = QHBoxLayout(self)
        row.setContentsMargins(theme.MARGIN_OUTER, 4, theme.MARGIN_OUTER, 4)
        row.setSpacing(12)
        title = QLabel("额度观察记录")
        title.setFont(_font(theme.POINT_BODY, 600))
        title.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        row.addWidget(title)

        self._dots = _FootprintDots()
        row.addWidget(self._dots)

        self._text_label = QLabel("尚未开始记录")
        self._text_label.setFont(_font(theme.POINT_SECONDARY))
        self._text_label.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        row.addWidget(self._text_label)
        row.addStretch(1)

        self._hist = QLabel("历史 ›")
        self._hist.setFont(_font(theme.POINT_BODY, 500))
        self._hist.setStyleSheet(f"color:{theme.LINK}; padding:2px 8px;"
                                 f" border:1px solid {theme.CONTROL_BORDER};"
                                 f" border-radius:{theme.RADIUS_CONTROL}px;")
        self._hist.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hist.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hist.mousePressEvent = lambda e: self.history_requested.emit()
        row.addWidget(self._hist)

    def update_marks(self, marks: List[str], text: str) -> None:
        self._marks = list(marks) + [self.KIND_NONE] * (7 - len(marks))
        self._text_label.setText(text)
        self._dots.set_marks(self._marks)

    def set_history_focused(self) -> None:
        self._hist.setFocus()


class _FootprintDots(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._marks: List[str] = ["none"] * 7
        self.setFixedSize(7 * 20, 16)
        self.setToolTip("最近 7 日额度观测：实心=确认额度增加；空心=有观测；短横=无观测\n"
                        "无变化不等于未工作；余额变化不计为 Token 活动")

    def set_marks(self, marks: List[str]) -> None:
        self._marks = list(marks) + ["none"] * (7 - len(marks))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, kind in enumerate(self._marks):
            cx = i * 20 + 8
            if kind == "confirmed_increase":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_color(theme.ACCENT))
                p.drawEllipse(QRect(cx - 5, 3, 10, 10))
            elif kind == "observed":
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_color(theme.ACCENT), 1.4))
                p.drawEllipse(QRect(cx - 5, 3, 10, 10))
            else:
                p.setPen(QPen(_color(theme.DIVIDER), 2.2))
                p.drawLine(cx - 5, 8, cx + 5, 8)


# ---------------------------------------------------------------------------
# 刷新按钮
# ---------------------------------------------------------------------------

class RefreshButton(QPushButton):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("刷新", parent)
        self.setFont(_font(theme.POINT_BODY, 500))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(72, 30)
        self._apply_style(over=False, down=False)

    def _apply_style(self, over: bool, down: bool) -> None:
        bg = theme.ACTION_SURFACE
        if down:
            bg = theme.SURFACE
        elif over:
            bg = theme.SURFACE_HOVER
        self.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:{theme.TEXT_PRIMARY};"
            f" border:1px solid {theme.CONTROL_BORDER};"
            f" border-radius:{theme.RADIUS_BUTTON}px; padding:3px 16px; }}"
            f"QPushButton:focus {{ border:2px solid {theme.ACCENT}; }}"
        )

    def enterEvent(self, event) -> None:  # noqa: N802
        self._apply_style(over=True, down=False)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._apply_style(over=False, down=False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._apply_style(over=False, down=True)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._apply_style(over=self.underMouse(), down=False)
        super().mouseReleaseEvent(event)

    def set_refreshing(self, refreshing: bool) -> None:
        self.setText("刷新中" if refreshing else "刷新")
