"""Aegis Token Monitor v0.2 —— 桌面 GUI 入口（PySide6 Widgets，方案 A）。

spawn / 导入安全：本文件是 GUI 进程的 ``__main__``，任何 multiprocessing
spawn 场景下都不会被重复执行（v0.2 实际使用线程 + Job Object，见 D-008）。

启动顺序（设计 §9）：先创建并显示窗口（不等待网络），随后启动
MonitorService（读缓存并显式标注 → 自动首发四家刷新）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 项目根 = v0.2 目录（aegis 在其下）
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from aegis.desktop.app import MainWindow          # noqa: E402
from aegis.history.store import HistoryStore      # noqa: E402
from aegis.services.monitor import MonitorService  # noqa: E402

DATA_DIR = ROOT / "data"
HISTORY_PATH = DATA_DIR / "history.sqlite3"

_REQUIRED_PY = (3, 11)


def _assert_python_version() -> None:
    if sys.version_info >= _REQUIRED_PY:
        return
    sys.stderr.write(
        f"[aegis] Python 版本过低: {sys.version_info.major}.{sys.version_info.minor},"
        f" 要求 >= {_REQUIRED_PY[0]}.{_REQUIRED_PY[1]}\n"
    )
    sys.exit(2)


def main() -> int:
    _assert_python_version()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QPalette, QColor

    app = QApplication(sys.argv)
    app.setApplicationName("Aegis Token Monitor")
    app.setStyle("Fusion")

    # 深色调色板（DESIGN.md 常量；控件默认色与画布一致）
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme_canvas()))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#F1F2F5"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#1B1C20"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#F1F2F5"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1F2937"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#F1F2F5"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#286BFF"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#F1F2F5"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#232429"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#F1F2F5"))
    app.setPalette(palette)

    history = HistoryStore(HISTORY_PATH)
    try:
        history.open()
    except Exception:
        # 损坏库：保留现场，实时卡片继续（设计 §12.1）；UI 足迹会显示降级文案
        history = _disabled_history(HISTORY_PATH)

    monitor = MonitorService(history=history)

    win = MainWindow(monitor, history)
    win.show()
    win.start_service()
    return app.exec()


def theme_canvas() -> str:
    from aegis.desktop.theme import CANVAS
    return CANVAS


def _disabled_history(path):
    """打开失败时返回一个全禁用的 HistoryStore 兼容对象（不重建、不覆盖）。"""
    store = HistoryStore(path)
    store._disabled = True
    return store


if __name__ == "__main__":
    sys.exit(main())
