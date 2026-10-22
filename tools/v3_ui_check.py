"""v3 截图与状态验证（测试数据内存注入，不写真实账户数据）。"""

from __future__ import annotations

import os
import sys
import tempfile
import time


def main() -> int:
    sys.path.insert(0, ".")
    from PySide6.QtWidgets import QApplication

    from aegis.desktop.app import MainWindow
    from aegis.desktop.adapter import TopupConfig
    from aegis.history.store import HistoryStore
    from aegis.model import Balance, Snapshot, Window
    from aegis.services.contracts import SnapshotEnvelope, TimeEnvelope
    from aegis.services.monitor import MonitorService

    shots = os.environ.get("SHOT_DIR", "docs/screenshots")
    os.makedirs(shots, exist_ok=True)
    app = QApplication(sys.argv)
    h = HistoryStore(os.path.join(tempfile.mkdtemp(), "s.sqlite3"))
    h.open()
    m = MonitorService(history=h,
                       readers={n: (lambda: None) for n in ("codex", "zai", "minimax", "deepseek")})
    w = MainWindow(m, h)
    w.show()
    app.processEvents()
    print("window size:", w.width(), "x", w.height())

    def qsnap(used5, usedw, r5="2026-09-12T21:29:00Z", rw="2026-09-19T16:28:00Z"):
        return Snapshot(provider="x", status="ok",
                        five_hour=Window(used5, 100 - used5, r5, 300),
                        weekly=Window(usedw, 100 - usedw, rw, 10080))

    def B(v, g=None):
        return Balance(value=v, currency="CNY", granted_balance=g)

    def render(envd, topup, shot=None):
        w._topup = topup
        w._on_envelopes(envd)
        w._drain_queue()
        app.processEvents()
        time.sleep(0.15)
        app.processEvents()
        if shot:
            w.grab().save(os.path.join(shots, shot))
            print("SAVED", shot)

    T100 = TopupConfig(total=100.0, currency="CNY")

    # ---- 1) 方案回归样例（测试数据）----
    env_ok = {
        "codex": SnapshotEnvelope(provider="codex", snapshot=qsnap(12.0, 2.0),
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope("2026-09-12T07:36:00Z", "2026-09-12T07:36:02Z", "2026-09-12T07:36:02Z")),
        "zai": SnapshotEnvelope(provider="zai", snapshot=qsnap(41.0, 8.0),
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:36:02Z", "2026-09-12T07:36:02Z")),
        "minimax": SnapshotEnvelope(provider="minimax", snapshot=qsnap(3.0, 18.0),
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:36:03Z", "2026-09-12T07:36:03Z")),
        "deepseek": SnapshotEnvelope(provider="deepseek",
            snapshot=Snapshot(provider="deepseek", status="ok", balance=B(64.80, 0.0)),
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:36:04Z", "2026-09-12T07:36:04Z")),
    }
    render(env_ok, T100, "v3_home_testdata.png")

    c = w._cards["codex"]
    assert c._col_five._number.text() == "12%" and c._col_weekly._number.text() == "2%"
    assert c._col_five._bar._fill == 12.0 and c._col_weekly._bar._fill == 2.0
    z = w._cards["zai"]
    assert z._col_five._number.text() == "41%" and z._col_weekly._number.text() == "8%"
    mm = w._cards["minimax"]
    assert mm._col_five._number.text() == "3%" and mm._col_weekly._number.text() == "18%"
    mc = w._money_card
    assert mc._topup_label.text() == "¥100.00" and mc._balance_label.text() == "¥64.80"
    assert mc._used.text() == "已用 ¥35.20 · 35.2%"
    assert mc._bar._fill == pytest_approx(35.2)
    print("ASSERT home testdata: PASS")

    # ---- 2) DeepSeek 各状态 ----
    def ds(name, bal, topup, note_probe=50):
        e = SnapshotEnvelope(provider="deepseek",
            snapshot=Snapshot(provider="deepseek", status="ok", balance=bal) if bal is not None else None,
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:00:00Z", "2026-09-12T07:00:00Z"))
        render({**env_ok, "deepseek": e}, topup)
        line = (f"[{name}] topup={mc._topup_label.text()!r} bal={mc._balance_label.text()!r} "
                f"used={mc._used.text()!r} fill={mc._bar._fill!r} note={mc._note.text()[:note_probe]!r}")
        print(line)
        return mc

    ds("pending", B(64.80), None)
    assert mc._topup_label.text() == "待登记" and mc._bar._fill is None
    assert "缺少充值总额" in mc._note.text()
    ds("mismatch_granted", B(64.80, 10.0), T100)
    assert mc._bar._fill is None and "赠送余额" in mc._note.text()
    ds("mismatch_over", B(150.0, 0.0), T100)
    assert "余额大于累计充值" in mc._note.text()
    ds("zero", B(0.0, 0.0), TopupConfig(0.0, "CNY"))
    assert "暂无充值" in mc._note.text()
    print("ASSERT deepseek states: PASS")

    # ---- 3) 前三家失败 / 缺失 / 刷新中 ----
    env_states = {
        "codex": SnapshotEnvelope(provider="codex", snapshot=qsnap(12.0, 2.0),
            freshness="STALE", completeness="FULL", availability="ERROR",
            error_code="network_error",
            time=TimeEnvelope(None, "2026-09-12T07:00:00Z", "2026-09-12T07:20:00Z")),
        "zai": SnapshotEnvelope(provider="zai",
            snapshot=Snapshot(provider="zai", status="ok",
                              five_hour=Window(0.0, 100.0, None, 300), weekly=None),
            freshness="LIVE", completeness="PARTIAL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:36:02Z", "2026-09-12T07:36:02Z")),
        "minimax": SnapshotEnvelope(provider="minimax", snapshot=None,
            freshness="NONE", completeness="FULL", availability="UNAVAILABLE",
            loading=True, time=TimeEnvelope()),
        "deepseek": SnapshotEnvelope(provider="deepseek",
            snapshot=Snapshot(provider="deepseek", status="ok", balance=B(64.80, 0.0)),
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:36:04Z", "2026-09-12T07:36:04Z")),
    }
    render(env_states, None, "v3_states.png")
    assert w._cards["codex"]._status_text.text() == "更新失败"
    assert w._cards["codex"]._col_five._number.text() == "12%"          # 旧值保留
    assert "旧数据" in w._cards["codex"]._note.text()
    assert w._cards["zai"]._col_weekly._number.text() == "暂无数据"      # 缺失不得显示 0%
    assert w._cards["zai"]._col_five._number.text() == "0%"             # 真实 0%（含重置未提供）
    assert w._cards["minimax"]._col_five._number.text() == "暂无数据"
    assert w._cards["minimax"]._status_text.text() == "刷新中"
    assert w._summary.text() == "0 家已更新 · 2 家失败 · 1 家部分数据", w._summary.text()
    print("ASSERT fail/missing states: PASS")

    # ---- 4) 窄容器（两列上下排列）----
    # 说明：主窗口最小宽 960（内容 852 > 800 阈值）时布局保持双列；
    # 堆叠机制用独立卡实例验证（对应方案“内容宽度不足 800px”的条件）。
    w.resize(960, 760)
    app.processEvents()
    from aegis.desktop.widgets import ServiceCardRow as _SCR
    from aegis.desktop.adapter import build_provider_vm as _bvm
    from aegis.services.monitor import DEFAULT_TTLS as _TTL
    solo = _SCR("codex")
    solo.show()
    app.processEvents()
    solo.update_vm(_bvm(env_ok["codex"], now=datetime_now(), ttl_seconds=300))
    solo.resize(1200, 210)
    app.processEvents()
    wide_dir = str(solo._columns_lay.direction())
    solo.resize(700, 210)             # 内容 652 < 800 → 堆叠
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()
    narrow_dir = str(solo._columns_lay.direction())
    solo.grab().save(os.path.join(shots, "v3_narrow_stacked.png"))
    print("SAVED v3_narrow_stacked.png; wide:", wide_dir, "narrow:", narrow_dir)
    assert "LeftToRight" in wide_dir and "TopToBottom" in narrow_dir
    print("ASSERT narrow stacking: PASS")

    m.shutdown(0.2)
    print("ALL_V3_UI_CHECKS: PASS")
    return 0


def pytest_approx(x):
    return x


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
