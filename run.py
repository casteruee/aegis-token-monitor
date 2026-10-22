"""Aegis Token Monitor v0.1 CLI 入口。

约束（P4 + ARCH §11 + ROUTE_REVIEW M-2/M-3/M-4 锁定）：

- **零依赖**：仅 Python 3.11+ 标准库。
- **M-4 解释器硬约束**：启动时断言 ``sys.version_info >= (3, 11)``，否则打
  印明确中文错误并 ``sys.exit(2)``。
- **三种模式**：
    * 默认（无参）→ 四 provider 全量重抓（不走 TTL 跳过）→ 渲染文本面板。
    * ``--json`` → 输出统一模型 JSON（``json.loads`` 可解析；**stdout 只
      有 JSON**，诊断信息走 stderr）。
    * ``--watch [interval]`` → 默认 300s 一轮，按 **per-provider due
      time** 只重抓到期者；未到期复用缓存（``reliability='cached'``），
      ANSI 清屏原地重画。
- **单 provider 失败不崩溃**：失败的 provider 进 error/stale 态，其余照
  常渲染。退出码仍为 0（成功执行了流程即视为 0）。
- **抓取串行**：v0.1 不引入并发。

诊断 / 进度信息走 :py:func:`print(..., file=sys.stderr)`，绝不污染 stdout
（``--json`` 模式下游 ``| python -c "import json,sys; ..."`` 解析）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from aegis import cache as cache_mod
from aegis import ui
from aegis.model import Report, Snapshot
from aegis.providers import READERS


# ---------------------------------------------------------------------------
# 解释器版本断言（ROUTE_REVIEW M-4）
# ---------------------------------------------------------------------------

_REQUIRED_PY = (3, 11)
_ANSI_CLEAR_FOR_PY = "\x1b[2J\x1b[H"


def _assert_python_version() -> None:
    """启动时硬约束：必须 Python 3.11+。失败：中文错误 + sys.exit(2)。"""
    if sys.version_info >= _REQUIRED_PY:
        return
    msg = (
        f"[aegis] Python 版本过低: 当前 {sys.version_info.major}."
        f"{sys.version_info.minor}.{sys.version_info.micro},"
        f" 要求 >= {_REQUIRED_PY[0]}.{_REQUIRED_PY[1]}\n"
        "[aegis] 请使用 py -3.12 / py -3.11 / python 3.11+ 重跑。\n"
        "[aegis] Windows:  项目已附 run.bat，优先选择解释器。"
    )
    # 中文输出走 stderr，避免污染 stdout / 退出码识别
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()
    sys.exit(2)


# ---------------------------------------------------------------------------
# 单 provider 抓取（不重抛异常 → 已由 reader 内部映射为 error snapshot）
# ---------------------------------------------------------------------------

def _fetch_one(
    name: str,
    reader: Callable[[], Snapshot],
) -> Snapshot:
    """调用 ``reader()`` 抓取一个 provider；捕获所有异常并映射为 error snapshot。"""
    try:
        snap = reader()
    except Exception as e:  # noqa: BLE001 - 防御性兜底
        from aegis.model import ErrorInfo
        snap = Snapshot(
            provider=name,
            status="error",
            source=f"{name}-api",
            reliability="live",
            error=ErrorInfo(type=type(e).__name__, message=str(e)[:200]),
        )
    return snap


# ---------------------------------------------------------------------------
# 单轮抓取：所有 provider 全量重抓
# ---------------------------------------------------------------------------

def _fetch_all_now() -> List[Snapshot]:
    """全量重抓四个 provider；每个独立捕获异常（绝不互相影响）。"""
    sys.stderr.write(
        f"[aegis] 抓取 {len(READERS)} 个 provider "
        f"({', '.join(READERS.keys())})...\n"
    )
    sys.stderr.flush()
    snaps: List[Snapshot] = []
    for name, reader in READERS.items():
        try:
            snap = _fetch_one(name, reader)
        except Exception as e:  # noqa: BLE001 - 终极防御
            from aegis.model import ErrorInfo
            snap = Snapshot(
                provider=name,
                status="error",
                source=f"{name}-api",
                reliability="live",
                error=ErrorInfo(type=type(e).__name__, message=str(e)[:200]),
            )
        # 仅 ok 快照写缓存（ARCH §8）
        if snap.status == "ok":
            try:
                cache_mod.write_provider(name, snap)
            except OSError as e:
                sys.stderr.write(f"[aegis] 写缓存失败 ({name}): {type(e).__name__}\n")
                sys.stderr.flush()
        snaps.append(snap)
        _log_fetch(name, snap)
    sys.stderr.flush()
    return snaps


def _log_fetch(name: str, snap: Snapshot) -> None:
    """抓取结果的诊断日志（stderr）。"""
    if snap.status == "ok":
        used = (
            snap.five_hour.used_percent if snap.five_hour is not None else None
        )
        sys.stderr.write(
            f"[aegis] {name}: status=ok reliability={snap.reliability} "
            f"used={used}%\n"
        )
    else:
        sys.stderr.write(
            f"[aegis] {name}: status=error reliability={snap.reliability} "
            f"type={(snap.error.type if snap.error else 'unknown')}\n"
        )
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Per-provider TTL（ARCH §9）
# ---------------------------------------------------------------------------

# Per-provider TTL（秒）。测试可通过 monkeypatch 修改。
PROVIDER_TTL: Dict[str, int] = {
    "codex": 300,
    "zai": 300,
    "minimax": 300,
    "deepseek": 1800,
}


def _watch_round(now: datetime) -> List[Snapshot]:
    """``--watch`` 模式的一轮：按 per-provider due time 决定是否重抓。

    规则（ARCH §9）：
    - 每个 provider 各自独立的 TTL（``PROVIDER_TTL``）。
    - 缓存 fresh → 直接复用 + ``reliability='cached'``。
    - 过期 / 无缓存 → 重抓，失败时按 ARCH §8 三件套回退。
    """
    cached_map = cache_mod.read_all()
    snaps: List[Snapshot] = []

    sys.stderr.write(
        f"[aegis] watch round at {_format_iso(now)} "
        f"(ttl={PROVIDER_TTL})\n"
    )
    sys.stderr.flush()

    for name, reader in READERS.items():
        ttl = PROVIDER_TTL.get(name, 300)
        cached_entry = cached_map.get(name)

        # 1) 先看缓存 fresh？
        cached_snap = cache_mod.get_if_fresh(name, cached_entry, now, ttl)
        if cached_snap is not None:
            sys.stderr.write(f"[aegis] {name}: cached (fresh within {ttl}s)\n")
            sys.stderr.flush()
            snaps.append(cached_snap)
            continue

        # 2) 不 fresh → 抓取
        try:
            fetched = _fetch_one(name, reader)
        except Exception as e:  # noqa: BLE001
            from aegis.model import ErrorInfo
            fetched = Snapshot(
                provider=name,
                status="error",
                source=f"{name}-api",
                reliability="live",
                error=ErrorInfo(type=type(e).__name__, message=str(e)[:200]),
            )

        # 3) resolve_after_fetch（成功=live；失败+缓存=stale；失败+无缓存=error）
        resolved = cache_mod.resolve_after_fetch(name, fetched, cached_entry)

        # 4) 写缓存（仅 ok 写）
        if fetched.status == "ok":
            try:
                cache_mod.write_provider(name, fetched)
            except OSError as e:
                sys.stderr.write(
                    f"[aegis] 写缓存失败 ({name}): {type(e).__name__}\n"
                )
                sys.stderr.flush()

        snaps.append(resolved)
        _log_fetch(name, resolved)

    sys.stderr.flush()
    return snaps


def _format_iso(dt: datetime) -> str:
    """aware datetime → 短形式 ISO（仅用于诊断日志）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 报告构造（统一模型 → JSON）
# ---------------------------------------------------------------------------

def _build_report(snaps: List[Snapshot]) -> Report:
    """构造 :class:`aegis.model.Report`（统一模型 §7.1）。"""
    now = datetime.now(tz=timezone.utc)
    return Report(
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        providers=list(snaps),
    )


def _render_json(report: Report) -> str:
    """统一模型 → JSON 字符串（ensure_ascii=False 保持中文可读）。"""
    return json.dumps(
        report.to_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    """构造 argparse。``--watch`` 可选地接受一个 interval 参数。"""
    parser = argparse.ArgumentParser(
        prog="aegis",
        description=(
            "Aegis Token Monitor — 统一读取 Codex / z.ai / MiniMax / "
            "DeepSeek 的额度与余额。"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--json",
        action="store_true",
        help="以统一 JSON 模型输出到 stdout（只含 JSON；诊断信息走 stderr）。",
    )
    mode.add_argument(
        "--watch",
        nargs="?",
        type=int,
        const=300,
        default=None,
        metavar="INTERVAL",
        help=(
            "每 INTERVAL 秒重抓一轮（默认 300）；按 per-provider TTL "
            "决定每个 provider 是否到 due time。"
        ),
    )
    return parser


def _emit_panel(snaps: List[Snapshot], *, watch: bool) -> None:
    """渲染文本面板并写到 stdout。``--watch`` 时使用 ANSI 清屏版。"""
    rendered = ui.render_panel_watch(snaps) if watch else ui.render_panel(snaps)
    sys.stdout.write(rendered + "\n")
    sys.stdout.flush()


def _emit_json(report: Report) -> None:
    """输出统一模型 JSON（stdout 唯一内容）。"""
    sys.stdout.write(_render_json(report) + "\n")
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口。

    :returns: 退出码（v0.1 约定：执行成功 = 0；解释器过低 = 2）。
    """
    _assert_python_version()

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # ---- 默认模式（一次性 + 面板）----
    if args.watch is None and not args.json:
        snaps = _fetch_all_now()
        _emit_panel(snaps, watch=False)
        return 0

    # ---- --json 模式 ----
    if args.json:
        snaps = _fetch_all_now()
        report = _build_report(snaps)
        _emit_json(report)
        return 0

    # ---- --watch 模式 ----
    interval = max(1, int(args.watch))
    sys.stderr.write(
        f"[aegis] watch mode: 每轮 interval={interval}s "
        f"(per-provider TTL: {PROVIDER_TTL})\n"
    )
    sys.stderr.flush()

    # 首次立刻跑一轮（让用户立即看到面板）
    while True:
        now = datetime.now(tz=timezone.utc)
        snaps = _watch_round(now)
        _emit_panel(snaps, watch=True)
        # 等待 interval 秒；支持 Ctrl+C 干净退出
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            sys.stderr.write("\n[aegis] watch mode interrupted, exiting.\n")
            sys.stderr.flush()
            return 0


if __name__ == "__main__":
    sys.exit(main())
