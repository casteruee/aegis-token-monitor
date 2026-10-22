"""真实启动冒烟（临时工具）：正常启动流程 → 等待首轮真实采集 → 截图 → 干净关闭。

用途：v0.4 验收用真实数据截图。读取凭据沿用既有只读路径；历史写入本版
data/history.sqlite3（个人运行时，不入库）。验收后可删除本文件。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from aegis.desktop import theme
from aegis.desktop.app import MainWindow
from aegis.history.store import HistoryStore
from aegis.services.monitor import MonitorService

app = QApplication(sys.argv)
app.setApplicationName("Aegis Token Monitor")
app.setStyle("Fusion")
palette = QPalette()
palette.setColor(QPalette.ColorRole.Window, QColor(theme.CANVAS))
palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.TEXT_PRIMARY))
palette.setColor(QPalette.ColorRole.Base, QColor(theme.SURFACE))
palette.setColor(QPalette.ColorRole.Text, QColor(theme.TEXT_PRIMARY))
palette.setColor(QPalette.ColorRole.Button, QColor(theme.ACTION_SURFACE))
palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.TEXT_PRIMARY))
palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.ACCENT))
app.setPalette(palette)

history = HistoryStore(ROOT / "data" / "history.sqlite3")
try:
    history.open()
except Exception:
    history = HistoryStore(ROOT / "data" / "history.sqlite3")
    history._disabled = True
monitor = MonitorService(history=history)
win = MainWindow(monitor, history)
win.show()
win.start_service()

state = {"noted": False, "t0": time.time()}

def tick():
    elapsed = time.time() - state["t0"]
    win._drain_queue()
    if elapsed > 25:
        win.grab().save(str(ROOT / "docs/screenshots/v42_home_real_aurora.png"))
        print("SAVED v42_home_real_aurora.png; summary =", win._summary.text(),
              "; scroll max =", win._home_page.verticalScrollBar().maximum())
        win.close()
        app.quit()
    elif elapsed > 20 and not state["noted"]:
        state["noted"] = True
        print("after 20s summary =", win._summary.text())

timer = QTimer()
timer.timeout.connect(tick)
timer.start(250)
rc = app.exec()
print("exit code:", rc)
