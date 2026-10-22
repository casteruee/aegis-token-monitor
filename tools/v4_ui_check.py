"""v0.4.1 截图与状态验证（测试数据内存注入，不写真实账户数据）。

覆盖第二轮精修验收项：
1) 无"详情 ›"入口、卡片不可点击、安静细边框
2) 额度条 12 DIP 胶囊 + 颜色锚点插值（0/1/50/80/100% 采样）
3) DeepSeek 钱包卡：钱包剩余 + 已花费；无口径时"已花费 —"+悬停说明
4) 底部无"Token 统计尚未接入"开发状态
5) 窗口缩放：变高沉底、变矮滚动、窄窗不截断
"""

from __future__ import annotations

import os
import sys
import tempfile
import time


def main() -> int:
    sys.path.insert(0, ".")
    try:  # GBK 控制台无法打印 ¥ 等字符：统一 UTF-8 输出
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel

    from aegis.desktop import theme
    from aegis.desktop.app import MainWindow
    from aegis.desktop.adapter import TopupConfig
    from aegis.desktop.widgets import ServiceCardRow, _usage_color
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

    # ---- 0) 颜色锚点插值（纯函数，无 UI）----
    a = lambda p: (_usage_color(p).red(), _usage_color(p).green(), _usage_color(p).blue())
    assert a(0) == (0x34, 0xD3, 0x99)
    assert a(50) == (0x60, 0xA5, 0xFA)
    assert a(75) == (0xA7, 0x8B, 0xFA)
    assert a(100) == (0xF8, 0x71, 0x71)
    # 中点插值精确（绿蓝 / 蓝紫 / 紫红三段各测一点）
    for pct, lo, hi in ((25, a(0), a(50)), (62.5, a(50), a(75)), (87.5, a(75), a(100))):
        for c, l, h in zip(a(pct), lo, hi):
            assert abs(c - (l + h) / 2) <= 1, (pct, a(pct), lo, hi)
    # 无灰色段：整条色带饱和度（max-min 通道差）不低于两侧锚点的较低者的一半
    def sat(t):
        return max(t) - min(t)
    for pct in range(0, 101, 5):
        s = sat(a(pct))
        floor = min(sat(a(0)), sat(a(50)), sat(a(75)), sat(a(100)))
        assert s >= floor // 2, (pct, a(pct), s)   # 旧方案 60-70% 处会跌破
    print("ASSERT color anchors + interpolation + no-gray ramp: PASS")

    # ---- 1) 正常窗口渲染（测试数据）----
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
    render(env_ok, T100, "v41_home_testdata.png")

    c = w._cards["codex"]
    # 1a) 无详情入口、不可点击、无手形光标、不接收焦点
    for card in list(w._cards.values()) + [w._money_card]:
        assert not hasattr(card, "_detail_hint") or card.findChild(QLabel, None) is not None
        assert "详情" not in "".join(ch.text() for ch in card.findChildren(QLabel))
        assert card.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert card.cursor().shape() != Qt.CursorShape.PointingHandCursor
    # 1b) 额度条 12 DIP；颜色随用量
    assert c._col_five._bar.height() == 12
    col = _usage_color(c._col_five._bar._fill)
    assert (col.red(), col.green(), col.blue()) == a(12.0)
    col_mm = _usage_color(w._cards["minimax"]._col_weekly._bar._fill)
    assert (col_mm.red(), col_mm.green(), col_mm.blue()) == a(18.0)
    # 1c) 钱包卡：钱包剩余 + 已花费（口径一致）
    mc = w._money_card
    assert mc._labels["balance"].text() == "¥64.80"
    assert mc._labels["spent"].text() == "¥35.20", mc._labels["spent"].text()
    assert not mc.findChild(QLabel) or "详情" not in "".join(
        ch.text() for ch in mc.findChildren(QLabel))
    # 1d) 汇总 = 四家口径
    assert w._summary.text() == "4 家已更新", w._summary.text()
    # 1e) 底部无开发状态说明
    fp_text = "".join(ch.text() for ch in w._footprint.findChildren(QLabel))
    assert "Token" not in fp_text, fp_text
    print("ASSERT home testdata (no detail entry, 12px bar, wallet, no dev note): PASS")

    # ---- 2) 额度条 0/1/50/80/100% 视觉与颜色（独立条实例）----
    from PySide6.QtGui import QPixmap, QPainter
    bar = type(c._col_five._bar)()
    bar.resize(400, 12)
    bar.show()
    app.processEvents()
    samples = {}
    BW = bar.width()
    for pct in (0.0, 1.0, 50.0, 80.0, 100.0):
        bar.set_used(pct)
        app.processEvents()
        img = bar.grab().toImage()
        # grab() 是物理像素（本环境 125% 缩放），按 DPR 折算逻辑坐标
        dpr = img.width() / max(1, BW)
        y = int(6 * dpr)
        mid_x = min(img.width() - 1, max(0, int((BW * pct / 100) - 3) * dpr)) \
            if pct > 0 else int((BW // 2) * dpr)          # 0%：中点应为轨道色
        track_x = int((BW - 12) * dpr)                    # 右端内侧（避开圆角帽）
        mid_px = img.pixelColor(mid_x, y)
        track_px = img.pixelColor(track_x, y)
        samples[pct] = (mid_px, track_px)
        expected = _usage_color(pct)
        if pct > 0:
            assert abs(mid_px.red() - expected.red()) <= 2 and \
                   abs(mid_px.green() - expected.green()) <= 2 and \
                   abs(mid_px.blue() - expected.blue()) <= 2, (pct, mid_px.name(), expected.name())
        print(f"[bar {pct:>5.1f}%] w={BW} fill_color={expected.name()} mid_px={mid_px.name()} "
              f"track_right={track_px.name()}")
    # 0%：中点即轨道色（无填充）
    assert samples[0.0][0].name().lower() == theme.TRACK.lower(), samples[0.0][0].name()
    # 80%：右端仍是轨道色（填充未越界）
    assert samples[80.0][1].name().lower() == theme.TRACK.lower(), samples[80.0][1].name()
    bar.grab().save(os.path.join(shots, "v41_bar_100.png"))
    # 各比例条截图（0/1/50/80/100 一图一张）
    for pct in (0.0, 1.0, 50.0, 80.0):
        bar.set_used(pct)
        app.processEvents()
        bar.grab().save(os.path.join(shots, f"v41_bar_{int(pct)}.png"))
    bar.set_used(None)   # 无数据
    app.processEvents()
    bar.grab().save(os.path.join(shots, "v41_bar_none.png"))
    print("ASSERT bar 0/1/50/80/100 colors & fill: PASS")

    # ---- 3) DeepSeek 钱包卡各状态 ----
    def ds(name, bal, topup):
        e = SnapshotEnvelope(provider="deepseek",
            snapshot=Snapshot(provider="deepseek", status="ok", balance=bal) if bal is not None else None,
            freshness="LIVE", completeness="FULL", availability="OK",
            time=TimeEnvelope(None, "2026-09-12T07:00:00Z", "2026-09-12T07:00:00Z"))
        render({**env_ok, "deepseek": e}, topup)
        spent = mc._labels["spent"]
        line = (f"[{name}] balance={mc._labels['balance'].text()!r} "
                f"spent={spent.text()!r} hint={spent.toolTip()[:44]!r}")
        print(line)
        return mc

    ds("ok", B(64.80), T100)
    assert mc._labels["spent"].text() == "¥35.20" and mc._labels["spent"].toolTip() == ""
    ds("pending", B(64.80), None)
    assert mc._labels["spent"].text() == "—"
    assert "累计充值未登记" in mc._labels["spent"].toolTip()
    ds("mismatch_granted", B(64.80, 10.0), T100)
    assert mc._labels["spent"].text() == "—" and "口径不一致" in mc._labels["spent"].toolTip()
    ds("zero", B(0.0, 0.0), TopupConfig(0.0, "CNY"))
    assert mc._labels["spent"].text() == "—"
    # 首页不再出现配置路径/充值说明正文
    mc_text = "".join(ch.text() for ch in mc.findChildren(QLabel))
    assert "config/topup.json" not in mc_text and "累计充值" not in mc_text, mc_text
    render(env_ok, T100, "v41_wallet_states.png")
    print("ASSERT wallet states (ok / — with tooltip): PASS")

    # ---- 4) 失败 / 缺失 / 刷新中 ----
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
    render(env_states, None, "v41_states.png")
    assert w._cards["codex"]._status_text.text() == "更新失败"
    assert w._cards["codex"]._col_five._number.text() == "12%"          # 旧值保留
    assert "旧数据" in w._cards["codex"]._note.text()
    assert w._cards["zai"]._col_weekly._number.text() == "暂无数据"      # 缺失不得显示 0%
    assert w._cards["zai"]._col_weekly._bar.isVisible() is False        # 缺失不画条
    assert w._cards["zai"]._col_five._number.text() == "0%"             # 真实 0%：空填充
    assert w._cards["zai"]._col_five._bar.isVisible() is True
    assert w._cards["minimax"]._col_five._number.text() == "暂无数据"
    assert w._summary.text() == "1 家已更新 · 2 家失败 · 1 家部分数据", w._summary.text()
    print("ASSERT fail/missing states: PASS")

    # ---- 5) 窗口缩放 ----
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    from aegis.desktop.adapter import build_provider_vm as _bvm

    # 5a) 变高：卡高不变、字号不变、空余沉底
    render(env_ok, T100)
    app.processEvents()
    h1 = w._cards["codex"].height()
    num_font = w._cards["codex"]._col_five._number.font().pointSizeF()
    w.resize(w.width(), 1000)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    assert w._cards["codex"].height() == h1
    assert w._cards["codex"]._col_five._number.font().pointSizeF() == num_font
    w.grab().save(os.path.join(shots, "v41_tall_window.png"))
    print("SAVED v41_tall_window.png; ASSERT tall: PASS")

    # 5b) 变矮：滚动出现、字号卡高稳定
    w.resize(w.width(), 520)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    sb = w._home_page.verticalScrollBar()
    assert sb.maximum() > 0
    assert w._cards["codex"].height() == h1
    assert w._cards["codex"]._col_five._number.font().pointSizeF() == num_font
    w.grab().save(os.path.join(shots, "v41_short_window.png"))
    print("SAVED v41_short_window.png; scrollbar max =", sb.maximum(), "; ASSERT short: PASS")

    # 5c) 变窄：720 最小宽下不截断
    w.resize(720, 640)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    for name in ("codex", "zai", "minimax"):
        card = w._cards[name]
        assert "LeftToRight" in str(card._columns_lay.direction())
        reset_lbl = card._col_five._reset
        assert reset_lbl.width() >= reset_lbl.sizeHint().width() - 1
    w.grab().save(os.path.join(shots, "v41_narrow_window.png"))
    print("SAVED v41_narrow_window.png; ASSERT narrow: PASS")

    # 5d) 独立卡实例验证堆叠机制
    solo = ServiceCardRow("codex")
    solo.show(); app.processEvents()
    solo.update_vm(_bvm(env_ok["codex"], now=now, ttl_seconds=300))
    solo.resize(1200, 160); app.processEvents()
    assert "LeftToRight" in str(solo._columns_lay.direction())
    solo.resize(600, 200)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    assert "TopToBottom" in str(solo._columns_lay.direction())
    solo.grab().save(os.path.join(shots, "v41_narrow_stacked_card.png"))
    print("SAVED v41_narrow_stacked_card.png; ASSERT solo stacking: PASS")

    m.shutdown(0.2)
    print("ALL_V41_UI_CHECKS: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
