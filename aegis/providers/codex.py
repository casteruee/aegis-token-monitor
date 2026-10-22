"""Codex reader via CodexBar CLI（ARCHITECTURE §1 + §6 + §13）。

唯一数据源（README ⛔硬约束 1，逐字复用）::

    codexbar-cli.exe usage -p codex --json

约束（来自 README / ARCH / ROUTE_REVIEW）：

- 必须显式 ``-p codex``。**禁止**裸 ``usage --json`` 或 ``-p all``。
- 调用方式：``subprocess.run([EXE, "usage", "-p", "codex", "--json"],
  capture_output=True, text=True, encoding="utf-8", timeout=60)``。
- 凭据：CodexBar 自理 ``~/.codex/auth.json``；本项目**绝不**读该文件。
- 返回是数组（取 ``[0]``），忽略 ``pace`` 字段。
- 错误分类：
    * EXE 缺失 / 启动失败 / 超时 / 非零退出 → :class:`CodexSourceUnavailable`
    * 空数组 / 非 JSON / 字段缺失 → :class:`ParseError`
- ``source = "codexbar-cli"``；``usage.updated_at`` **截断到微秒**后存
  （CodexBar 输出纳秒精度，Python 3.11 fromisoformat 会保留 6 位小数，但
  在字符串层显式截断可避免依赖解释器版本的差异 —— ROUTE_REVIEW M-4）。
- 测试便利：:data:`EXE_PATH` / :data:`TIMEOUT_SECONDS` 为模块级常量，pytest
  用 :func:`monkeypatch.setattr` 注入假 EXE 路径即可覆盖子进程分支。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from aegis import normalize
from aegis.model import ExtraWindow, Snapshot, Window


# ---------------------------------------------------------------------------
# 模块级常量（便于测试注入 + 锁住硬编码命令）
# ---------------------------------------------------------------------------

# 默认位置按本机 %LOCALAPPDATA% 推算；可用环境变量 CODEXBAR_CLI 覆盖。
EXE_PATH = os.environ.get("CODEXBAR_CLI") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Programs", "CodexBar", "codexbar-cli.exe",
)
CLI_ARGS = ("usage", "-p", "codex", "--json")
TIMEOUT_SECONDS = 60

_PROVIDER = "codex"
_SOURCE = "codexbar-cli"

# 子进程启动时可能出现 / 不应出现的「凭据类」字串（NO_SECRET_LEAK 断言用）
_LEAK_TOKENS = (
    "sk-",
    "Bearer ",
    "ANTHROPIC_AUTH_TOKEN",
)


# ---------------------------------------------------------------------------
# 异常类型（独立、可捕获、不暴露 CodexBar 内部异常类）
# ---------------------------------------------------------------------------

class CodexSourceUnavailable(Exception):
    """CodexBar 子进程不可用：EXE 缺失、超时、启动失败、非零退出。"""


class ParseError(Exception):
    """CodexBar 输出不是合法 JSON object（空数组 / 非 JSON / 字段缺失）。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _error_snapshot(err_type: str, message: str) -> Snapshot:
    """构造一个 ``status='error'`` 的快照；所有数值字段显式 ``None``。"""
    from aegis.model import ErrorInfo
    return Snapshot(
        provider=_PROVIDER,
        status="error",
        source=_SOURCE,
        five_hour=None,
        weekly=None,
        balance=None,
        extras=[],
        error=ErrorInfo(type=err_type, message=message),
    )


# 截断 ISO 字符串的小数位到微秒（最多 6 位）；其余部分原样保留。
_FRAC_RE = re.compile(r"\.(\d+)(?=[Zz+\-])")


def _truncate_iso_to_microseconds(s: str) -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffffffZ`` → ``...SS.ffffffZ``（最多 6 位）。

    CodexBar 的 ``updated_at`` / ``resets_at`` 在部分字段含纳秒精度（9 位小
    数）。Python 3.11 的 :py:meth:`datetime.fromisoformat` 会截到微秒，但
    为**不**依赖解释器版本（ROUTE_REVIEW M-4 锁 3.11+ 但仍保守），这里在
    字符串层显式截断到 6 位小数。
    """
    if not isinstance(s, str) or "." not in s:
        return s

    def _sub(m: re.Match) -> str:
        frac = m.group(1)
        if len(frac) > 6:
            frac = frac[:6]
        return "." + frac

    return _FRAC_RE.sub(_sub, s, count=1)


def _to_utc_iso_z(s: str) -> str:
    """解析 ISO 字符串（可能含 9 位小数 / Z / +offset）→ UTC ISO Z 字符串。

    步骤：
      1) 截断小数位到微秒（防止纳秒解析差异）；
      2) :func:`normalize.iso_to_utc` 解析；
      3) 重新格式化为 ``YYYY-MM-DDTHH:MM:SS[.fff / .ffffff]Z``（**保留原始精
         度**：≤3 位小数 → 毫秒；>3 位小数 → 微秒）。

    例如：
      - ``.373Z`` → ``.373Z``（3 位毫秒）
      - ``.123456Z`` → ``.123456Z``（6 位微秒）
      - ``.373888600Z``（9 位纳秒）→ ``.373888Z``（截到 6 位微秒）
    """
    truncated = _truncate_iso_to_microseconds(s)

    # 探测原始小数位数
    original_frac_digits = 0
    if "." in truncated:
        head, _, rest = truncated.partition(".")
        tz_idx = len(rest)
        for i, ch in enumerate(rest):
            if ch in "Z+-":
                tz_idx = i
                break
        original_frac_digits = tz_idx

    dt = normalize.iso_to_utc(truncated)
    utc_dt = dt.astimezone(timezone.utc)
    out = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    if utc_dt.microsecond and original_frac_digits > 0:
        if original_frac_digits <= 3:
            out += f".{utc_dt.microsecond // 1000:03d}"
        else:
            out += f".{utc_dt.microsecond:06d}"
    return out + "Z"


def _to_window(raw: dict, *, window_minutes: int, kind: str) -> Window:
    """``primary`` / ``secondary`` → :class:`Window`；used 语义（``from_used``）。

    :raises normalize.UnexpectedShape: 字段缺失 / 百分比越界。
    """
    used_pct = raw.get("used_percent")
    if used_pct is None:
        raise normalize.UnexpectedShape(
            f"codex: {kind}: missing 'used_percent'"
        )
    used, remaining = normalize.from_used(used_pct)

    resets_at_raw = raw.get("resets_at")
    if not isinstance(resets_at_raw, str) or not resets_at_raw:
        raise normalize.UnexpectedShape(
            f"codex: {kind}: missing or empty 'resets_at'"
        )

    return Window(
        used_percent=used,
        remaining_percent=remaining,
        resets_at=_to_utc_iso_z(resets_at_raw),
        window_minutes=window_minutes,
    )


def _to_extra(raw_extra: dict) -> ExtraWindow:
    """``extra_rate_windows[]`` → :class:`ExtraWindow`（**原样透传**字段）。

    v0.1 范围：``id`` / ``title`` / ``used_percent`` / ``resets_at`` /
    ``reset_description`` / ``is_informational`` / ``window_minutes``。
    源 ``used_percent`` 已是 used 语义，按 ARCH §1 直接存。
    """
    wid = raw_extra.get("id")
    title = raw_extra.get("title")
    if not isinstance(wid, str) or not wid:
        raise normalize.UnexpectedShape("codex: extra_rate_windows entry missing 'id'")
    if not isinstance(title, str):
        title = ""

    window = raw_extra.get("window")
    if not isinstance(window, dict):
        raise normalize.UnexpectedShape(
            f"codex: extra_rate_windows[{wid!r}] missing 'window' object"
        )

    used_raw = window.get("used_percent")
    if used_raw is None:
        raise normalize.UnexpectedShape(
            f"codex: extra_rate_windows[{wid!r}].window missing 'used_percent'"
        )
    try:
        used = float(used_raw)
    except (TypeError, ValueError) as e:
        raise normalize.UnexpectedShape(
            f"codex: extra_rate_windows[{wid!r}].window.used_percent "
            f"not numeric: {used_raw!r}"
        ) from e

    resets_at_raw = window.get("resets_at")
    resets_at: Optional[str] = None
    if isinstance(resets_at_raw, str) and resets_at_raw:
        resets_at = _to_utc_iso_z(resets_at_raw)

    reset_description = window.get("reset_description")
    if reset_description is not None and not isinstance(reset_description, str):
        reset_description = str(reset_description)

    is_informational = bool(window.get("is_informational"))

    window_minutes_raw = window.get("window_minutes")
    window_minutes: Optional[int] = None
    if window_minutes_raw is not None:
        try:
            window_minutes = int(window_minutes_raw)
        except (TypeError, ValueError) as e:
            raise normalize.UnexpectedShape(
                f"codex: extra_rate_windows[{wid!r}].window.window_minutes "
                f"not int: {window_minutes_raw!r}"
            ) from e

    return ExtraWindow(
        id=wid,
        title=title,
        used_percent=used,
        resets_at=resets_at,
        reset_description=reset_description,
        is_informational=is_informational,
        window_minutes=window_minutes,
    )


def _to_snapshot(item: dict) -> Snapshot:
    """``codexbar-cli usage --json`` 的 ``[0]`` 项 → :class:`Snapshot`。

    :raises normalize.UnexpectedShape: 必需字段缺失。
    """
    usage = item.get("usage")
    if not isinstance(usage, dict):
        raise normalize.UnexpectedShape("codex: top-level 'usage' missing or not object")

    primary = usage.get("primary")
    if not isinstance(primary, dict):
        raise normalize.UnexpectedShape("codex: 'usage.primary' missing or not object")
    five_hour = _to_window(primary, window_minutes=300, kind="primary")

    secondary = usage.get("secondary")
    if not isinstance(secondary, dict):
        raise normalize.UnexpectedShape("codex: 'usage.secondary' missing or not object")
    weekly = _to_window(secondary, window_minutes=10080, kind="secondary")

    extras_raw = usage.get("extra_rate_windows") or []
    if not isinstance(extras_raw, list):
        raise normalize.UnexpectedShape(
            "codex: 'usage.extra_rate_windows' is not a list"
        )
    extras = [_to_extra(x) for x in extras_raw if isinstance(x, dict)]

    updated_at_raw = usage.get("updated_at")
    updated_at: Optional[str] = None
    if isinstance(updated_at_raw, str) and updated_at_raw:
        updated_at = _to_utc_iso_z(updated_at_raw)

    return Snapshot(
        provider=_PROVIDER,
        status="ok",
        source=_SOURCE,
        five_hour=five_hour,
        weekly=weekly,
        extras=extras,
        updated_at=updated_at,
        reliability="live",  # 当前快照直接来自子进程 → live
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def read() -> Snapshot:
    """读取 Codex 配额，归一化为 :class:`Snapshot`。

    **永不抛异常**（ARCH §7.1）；所有失败路径映射为 ``status='error'``
    快照。**不**读取 ``~/.codex/auth.json``，**不**解析 ``pace``。
    """
    # 1) 子进程
    try:
        completed = subprocess.run(  # noqa: S603 - 命令是常量列表，可信
            [EXE_PATH, *CLI_ARGS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _error_snapshot(
            "codex_source_unavailable",
            f"codexbar-cli not found at {EXE_PATH!r}",
        )
    except subprocess.TimeoutExpired:
        return _error_snapshot(
            "codex_source_unavailable",
            f"codexbar-cli timed out after {TIMEOUT_SECONDS}s",
        )
    except OSError as e:
        # WindowsError / PermissionError / 启动失败等系统层错误
        return _error_snapshot(
            "codex_source_unavailable",
            f"failed to invoke codexbar-cli: {type(e).__name__}",
        )

    # 2) 非零退出 → CodexSourceUnavailable（消息含 rc + stderr 前 200 字脱敏）
    if completed.returncode != 0:
        stderr = (completed.stderr or "")
        stderr_preview = stderr[:200]
        # 防止 stderr 偶然夹带 sk- / Bearer —— 替换为占位
        for tok in _LEAK_TOKENS:
            if tok in stderr_preview:
                stderr_preview = stderr_preview.replace(tok, "<redacted>")
        return _error_snapshot(
            "codex_source_unavailable",
            f"codexbar-cli rc={completed.returncode} "
            f"stderr={stderr_preview!r}",
        )

    # 3) JSON 解析
    stdout = completed.stdout or ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return _error_snapshot(
            "parse_error",
            f"codexbar-cli stdout is not valid JSON: {type(e).__name__}: {str(e)[:120]}",
        )

    # 4) 必须是数组
    if not isinstance(data, list):
        return _error_snapshot(
            "parse_error",
            "codexbar-cli stdout is not a JSON array",
        )
    if not data:
        return _error_snapshot(
            "parse_error",
            "codexbar-cli returned an empty array",
        )

    first = data[0]
    if not isinstance(first, dict):
        return _error_snapshot(
            "parse_error",
            "codexbar-cli array[0] is not an object",
        )

    # 5) 业务字段归一化（异常统一映射为 unexpected_shape）
    try:
        return _to_snapshot(first)
    except (normalize.UnexpectedShape, normalize.NormalizationError) as e:
        return _error_snapshot("unexpected_shape", str(e))