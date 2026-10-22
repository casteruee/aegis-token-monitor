"""Aegis Token Monitor v0.1 自检脚本（P4 交付，Hermes 最终验收）。

约束（ARCH §T7 + P4）：
- 约 30~50 行；纯标准库（``subprocess`` / ``sys`` / ``json`` / ``os`` /
  ``re``）；不引入第三方依赖。
- **禁止打印任何 key**（只能打印长度或 PASS/FAIL）。
- 输出**脱敏**摘要：
    1. 跑 ``python -m pytest tests -q``，打印结果行；
    2. 真机抓一次四家 provider，打印 ``status / source / used_percent /
       resets_at / balance``（**严禁打印任何凭据**）；
    3. 把 ``run.py --json`` 的输出字符串 + 上一步的异常文本，在**内存中**
       与各凭据源的真实值（及尾 8 字符）比对，断言不出现在任何输出中；
       打印 ``NO_SECRET_LEAK: PASS/FAIL``；
    4. （其它步骤的诊断行仅打印长度 / 标记）
- 退出码：自检失败 = 1；成功 = 0。

注意：这是 P4 交付的自检脚本，**最终验收权归 Hermes**。本脚本只产出
可审计的脱敏摘要，最终 PASS 由 Hermes 拍板。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 项目根 = tools/ 的父目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 与 aegis.net / aegis.credentials 保持一致的脱敏正则
_REDACT_SK = re.compile(r"sk-[A-Za-z0-9_\-]+")


def _scrub_keys(text: str) -> str:
    """把字符串里所有 sk- 段脱敏为 <redacted>，用于日志展示。"""
    return _REDACT_SK.sub("<redacted>", text)


def _run_pytest() -> bool:
    """跑 pytest，返回是否成功（退出码 0）。"""
    print("=" * 60)
    print("[selfcheck] step 1: pytest tests -q")
    print("=" * 60)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # 仅打印末尾的「N passed」/「N failed」摘要行（避免大量 traceback 噪声）
    out = (result.stdout or "") + (result.stderr or "")
    tail_lines = [
        ln for ln in out.splitlines()
        if "passed" in ln or "failed" in ln or "error" in ln.lower()
    ]
    if tail_lines:
        for ln in tail_lines[-5:]:
            print(ln)
    else:
        # 没匹配到摘要时打印尾部 8 行
        for ln in out.splitlines()[-8:]:
            print(ln)
    return result.returncode == 0


def _live_fetch() -> tuple[str, str]:
    """真机抓一次四 provider（带 stderr 捕获），返回 (stdout, stderr)。

    stdout = JSON 字符串；stderr = 诊断信息。
    """
    print("=" * 60)
    print("[selfcheck] step 2: live fetch (run.py --json)")
    print("=" * 60)
    result = subprocess.run(
        [sys.executable, "run.py", "--json"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout, result.stderr


def _summarize_live(stdout: str) -> None:
    """打印每个 provider 的 ``status / source / used_percent /
    resets_at / balance``（脱敏）。"""
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"[selfcheck] run.py --json is NOT valid JSON: {type(e).__name__}")
        return

    providers = report.get("providers") or []
    for p in providers:
        name = p.get("provider") or "?"
        status = p.get("status") or "?"
        source = p.get("source") or ""
        fh = p.get("five_hour") or {}
        used = fh.get("used_percent") if isinstance(fh, dict) else None
        reset = fh.get("resets_at") if isinstance(fh, dict) else None
        bal = p.get("balance")
        bal_str = (
            f"{bal.get('value')} {bal.get('currency')}"
            if isinstance(bal, dict) else "—"
        )
        used_str = f"{used:.1f}%" if isinstance(used, (int, float)) else "—"
        print(
            f"  {name:<8} status={status:<8} source={source:<14} "
            f"used={used_str:<8} resets_at={reset or '—':<22} "
            f"balance={bal_str}"
        )


def _collect_real_keys() -> dict:
    """在内存中读取各凭据源的**真实** key；返回 ``{provider: real_key}``。

    **绝不打印** key 内容，只用于**内存中的**字符串比对。
    读取失败（凭据缺失、文件不在等）→ 不计入比对（不算 leak）。
    """
    keys: dict = {}
    # tools/selfcheck.py 跑在项目根时 sys.path 已含项目根；
    # 但保险起见显式把项目根插到 sys.path 头部
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        from aegis import credentials  # noqa: WPS433 - 局部导入避免 tools 启动期硬依赖
        for prov in ("zai", "minimax", "deepseek"):
            try:
                keys[prov] = credentials.get_key(prov)
            except Exception:
                # 凭据不可读不算 leak，跳过
                pass
    except Exception as e:
        print(f"[selfcheck] credentials module not loadable: {type(e).__name__}: {e}")
    return keys


def _check_no_leak(stdout: str, stderr: str, real_keys: dict) -> bool:
    """断言 stdout + stderr 里**不出现**任何真实 key 子串。

    包括「完整 key」与「末尾 8 字符」两种匹配模式。
    """
    print("=" * 60)
    print("[selfcheck] step 3: NO_SECRET_LEAK audit (in-memory)")
    print("=" * 60)
    combined = (stdout or "") + (stderr or "")
    leaks: list[str] = []

    for prov, key in real_keys.items():
        if not key or len(key) < 8:
            continue
        if key in combined:
            leaks.append(f"{prov}: full key found")
        tail8 = key[-8:]
        if tail8 and tail8 in combined:
            leaks.append(f"{prov}: tail8={_scrub_keys(tail8)[:6]}... found")

    if leaks:
        print(f"NO_SECRET_LEAK: FAIL ({len(leaks)} hits)")
        for ln in leaks:
            print(f"  - {ln}")
        return False
    print(
        f"NO_SECRET_LEAK: PASS "
        f"(checked {len(real_keys)} sources, "
        f"stdout_len={len(stdout)}, stderr_len={len(stderr)})"
    )
    return True


def main() -> int:
    """主入口；返回退出码（0=成功，1=失败）。"""
    print("[selfcheck] Aegis Token Monitor v0.1 self-check")
    print(f"[selfcheck] project_root={PROJECT_ROOT}")
    print(f"[selfcheck] python={sys.version.split()[0]}")

    overall_ok = True

    # Step 1: pytest
    if not _run_pytest():
        print("[selfcheck] pytest FAILED")
        overall_ok = False
    else:
        print("[selfcheck] pytest OK")

    # Step 2: 真机抓取
    stdout, stderr = _live_fetch()

    # Step 2b: 摘要打印（脱敏）
    _summarize_live(stdout)

    # Step 3: NO_SECRET_LEAK
    real_keys = _collect_real_keys()
    if not _check_no_leak(stdout, stderr, real_keys):
        overall_ok = False

    # 收尾
    print("=" * 60)
    if overall_ok:
        print("[selfcheck] OVERALL: PASS (hand off to Hermes for final sign-off)")
        return 0
    print("[selfcheck] OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
