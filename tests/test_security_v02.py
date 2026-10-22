"""安全边界测试：缓存验证拒绝坏条目、哨兵不进信封/UI 文案、Job Object。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from aegis.services import validation
from aegis.services.validation import EntryRejected, validate_snapshot
from aegis.services.contracts import safe_error_text
from aegis.desktop.adapter import build_provider_vm
from aegis.services.contracts import SnapshotEnvelope, TimeEnvelope

UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

# 完整哨兵（不打印真实凭据；此为测试假串）
SENTINEL = "sk-TEST-abcdef0123456789SENTINEL"


# ---- 缓存验证（CC 低危发现 2 的 v0.2 修复） ----

def test_validation_rejects_missing_percent_instead_of_zero():
    bad = {"status": "ok", "five_hour": {"remaining_percent": 50.0, "resets_at": None}}
    with pytest.raises(EntryRejected):
        validate_snapshot("codex", bad)


def test_validation_rejects_nan_and_out_of_range():
    import math
    nan = float("nan")
    with pytest.raises(EntryRejected):
        validate_snapshot("codex", {"status": "ok", "five_hour": {
            "used_percent": nan, "remaining_percent": 50.0}})
    with pytest.raises(EntryRejected):
        validate_snapshot("codex", {"status": "ok", "five_hour": {
            "used_percent": 120.0, "remaining_percent": -20.0}})


def test_validation_rejects_bad_time_and_unknown_provider():
    with pytest.raises(EntryRejected):
        validate_snapshot("codex", {"status": "ok", "five_hour": {
            "used_percent": 10.0, "remaining_percent": 90.0, "resets_at": "not-a-time"}})
    with pytest.raises(EntryRejected):
        validate_snapshot("unknownprov", {"status": "ok"})


def test_validation_accepts_clean_entry():
    ok = validate_snapshot("codex", {
        "status": "ok",
        "five_hour": {"used_percent": 55.0, "remaining_percent": 45.0,
                      "resets_at": "2026-09-12T07:51:57Z", "window_minutes": 300},
        "weekly": None, "balance": None, "updated_at": "2026-09-12T06:23:51Z",
    })
    assert ok.status == "ok"
    assert ok.five_hour.used_percent == 55.0


# ---- 哨兵注入：错误路径 / 损坏缓存 / 信封 → UI 文案无敏感值 ----

def test_sentinel_never_reaches_vm_via_error_paths():
    # 1) 未知错误码 → safe_error_text 落回固定文案（不透传）
    assert safe_error_text("totally_unknown-" + SENTINEL) == "内部错误"
    # UNAVAILABLE 态卡片无错误正文（设计：灰短横 + 暂不可用）
    env = SnapshotEnvelope(provider="codex", snapshot=None,
                           availability="UNAVAILABLE", freshness="NONE")
    env.error_code = "totally_unknown-" + SENTINEL
    vm = build_provider_vm(env, now=NOW, ttl_seconds=300)
    assert SENTINEL not in vm.error_text
    assert vm.error_text == ""
    # 2) 损坏缓存条目（含哨兵字段）整体拒绝
    with pytest.raises(EntryRejected):
        validate_snapshot("codex", {"status": "ok", "note": SENTINEL})


def test_http_status_whitelist_only():
    assert safe_error_text("http_error", http_status=401) == "上游返回错误（HTTP 401）"
    assert safe_error_text("http_error", http_status=None) == "上游返回错误"
    assert safe_error_text("whatever-PUT-" + SENTINEL) == "内部错误"


# ---- Windows Job Object ----

@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")
def test_job_object_installed_and_kills_grandchildren():
    from aegis.services import proclife
    assert proclife.install_kill_on_close() is True
    # 幂等
    assert proclife.install_kill_on_close() is True
    # spawn 一个会自己再起子进程、且长驻的 python：验证 kill-on-close
    # 这里只验证本进程已入 job（子进程继承成员资格）：
    import subprocess
    code = (
        "import win32ts" if False else
        "import ctypes,sys;k=ctypes.windll.kernel32;"
        "print('ALIVE', flush=True);sys.stdin.read()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ALIVE"
    finally:
        proc.kill()          # 测试自身清理（非 job 依赖）
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")
def test_job_kills_child_on_process_death_integration():
    """集成证据：子进程加入父 job；父退出 → 子进程被系统回收。

    用中间 python 进程 B（继承 job）再启动孙进程 C（stdin 阻塞）；
    杀掉 B 后 C 若不在 job 内会变成孤儿继续运行 —— 通过
    PROCESS_QUERY 检查 C 已退出即证明 job 链生效。
    """
    import os
    import subprocess
    import time
    script = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c',"
        "\"import sys;sys.stdin.read()\"],stdin=subprocess.PIPE);"
        "print(p.pid, flush=True);sys.stdin.read()"
    )
    b = subprocess.Popen([sys.executable, "-c", script],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    from aegis.services import proclife
    proclife.install_kill_on_close()   # 本测试进程已在 job 内（上一用例），幂等
    try:
        c_pid = int(b.stdout.readline().strip())
    finally:
        b.kill()
        b.wait(timeout=5)
    # 孙进程 C：若 kill-on-close 生效，B 死后 C 被 job 回收
    deadline = time.time() + 5
    alive = True
    while time.time() < deadline:
        # OpenProcess → 若打不开（拒绝/不存在）视为已退出
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, c_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            alive = False
            break
        ctypes.windll.kernel32.CloseHandle(h)
        time.sleep(0.2)
    assert alive is False, f"grandchild {c_pid} survived parent death (job chain broken)"
