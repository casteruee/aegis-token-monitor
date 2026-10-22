"""MiniMax coding_plan remains reader（ARCHITECTURE §3 + §6 + §13）。

直连（非 CodexBar，D-003 实测确定）：

    GET https://api.minimax.io/v1/api/openplatform/coding_plan/remains
    Authorization: Bearer <sk-cp-... key>

响应（``probe_raw/minimax_remains.json`` + ``tests/fixtures/minimax_remains.json``）：

    {"model_remains": [
        {"model_name": "general",
         "start_time": 1789102800000, "end_time": 1789120800000,
         "current_interval_remaining_percent": 93,
         "weekly_start_time": 1788739200000, "weekly_end_time": 1789344000000,
         "current_weekly_remaining_percent": 85,
         "current_interval_status": 1, "current_weekly_status": 1},
        {"model_name": "video", ...}        ← 必须**过滤掉**
    ],
     "base_resp": {"status_code": 0, "status_msg": "success"}}

关键语义（README + ARCH §7.2 三道保险）：

- ``base_resp.status_code != 0`` → 报错（消息含 ``status_msg``）。
- 在 ``model_remains[]`` 中取 ``model_name == "general"`` 的**第一条**；没有
  → :class:`normalize.UnexpectedShape`。
- ``current_interval_remaining_percent`` = **剩余 %**（与 z.ai 相反）→ 必须
  走 :func:`normalize.from_remaining`（**翻转**）。
- ``current_weekly_remaining_percent`` 同上。
- ``end_time`` → ``five_hour.resets_at``（epoch **毫秒**）；
  ``weekly_end_time`` → ``weekly.resets_at``。
- 窗口跨度断言：5h = 18,000,000ms；weekly = 604,800,000ms。**不符 → UnexpectedShape**。
- ``current_interval_status`` / ``current_weekly_status`` 原样透传进
  ``Snapshot.raw_status``（v0.1 不解释语义）。
- **禁止** ``api.minimaxi.com``（需要 cookie，``status_code=1004``）。
- ``source = "minimax-api"``。
"""

from __future__ import annotations

from aegis import credentials
from aegis import net
from aegis import normalize
from aegis.model import Snapshot, Window, ErrorInfo


# URL 常量（README + ARCH §6 表）
URL = "https://api.minimax.io/v1/api/openplatform/coding_plan/remains"

_PROVIDER = "minimax"
_SOURCE = "minimax-api"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _error_snapshot(err_type: str, message: str) -> Snapshot:
    return Snapshot(
        provider=_PROVIDER,
        status="error",
        source=_SOURCE,
        error=ErrorInfo(type=err_type, message=message),
    )


def _to_window_from_item(item: dict, *, kind: str) -> Window:
    """从 ``model_remains`` general 条目构造 ``Window``；调用方保证字段完整。"""
    if kind == "interval":
        pct = item["current_interval_remaining_percent"]
        start = item["start_time"]
        end = item["end_time"]
        normalize.assert_minimax_interval_span(start, end)
        reset_iso = normalize.epoch_ms_to_utc_iso(end)
        window_minutes = 300
    elif kind == "weekly":
        pct = item["current_weekly_remaining_percent"]
        start = item["weekly_start_time"]
        end = item["weekly_end_time"]
        normalize.assert_minimax_weekly_span(start, end)
        reset_iso = normalize.epoch_ms_to_utc_iso(end)
        window_minutes = 10080
    else:
        raise normalize.UnexpectedShape(f"minimax: unknown window kind {kind!r}")
    used, remaining = normalize.from_remaining(pct)
    return Window(
        used_percent=used,
        remaining_percent=remaining,
        resets_at=reset_iso,
        window_minutes=window_minutes,
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def read() -> Snapshot:
    """读取 MiniMax remains，归一化为 :class:`Snapshot`。

    永不抛异常 —— 所有失败路径映射为 ``status='error'`` 快照。
    """
    # 1) 凭据
    try:
        key = credentials.get_key(_PROVIDER)
    except credentials.MissingCredential as e:
        return _error_snapshot("missing_credential", str(e))

    # 2) 网络 / 解析
    try:
        data = net.http_get_json(URL, key)
    except net.HttpError as e:
        return _error_snapshot("http_error", f"HTTP {e.status}")
    except net.NetworkError as e:
        return _error_snapshot("network_error", net._redact(str(e)))
    except net.ParseError as e:
        return _error_snapshot("parse_error", net._redact(str(e)))

    # 3) 业务字段归一化
    try:
        return _to_snapshot(data)
    except (normalize.UnexpectedShape, normalize.NormalizationError) as e:
        return _error_snapshot("unexpected_shape", str(e))


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def _to_snapshot(data: dict) -> Snapshot:
    # ---- base_resp 校验链 ----
    base_resp = data.get("base_resp")
    if not isinstance(base_resp, dict):
        raise normalize.UnexpectedShape("minimax: missing 'base_resp' object")
    status_code = base_resp.get("status_code")
    status_msg = base_resp.get("status_msg") or ""
    if status_code != 0:
        raise normalize.UnexpectedShape(
            f"minimax: base_resp.status_code={status_code!r} msg={status_msg!r}"
        )

    # ---- model_remains 校验 + 过滤 general ----
    model_remains = data.get("model_remains")
    if not isinstance(model_remains, list) or not model_remains:
        raise normalize.UnexpectedShape(
            "minimax: 'model_remains' missing or empty"
        )

    general_items = [
        item for item in model_remains
        if isinstance(item, dict) and item.get("model_name") == "general"
    ]
    if not general_items:
        raise normalize.UnexpectedShape(
            "minimax: no entry with model_name == 'general'"
        )
    item = general_items[0]  # 第一条（README 配方）

    # ---- 字段完整性 ----
    required_interval = ("current_interval_remaining_percent",
                         "start_time", "end_time")
    required_weekly = ("current_weekly_remaining_percent",
                       "weekly_start_time", "weekly_end_time")
    for f in required_interval + required_weekly:
        if item.get(f) is None:
            raise normalize.UnexpectedShape(f"minimax: general item missing field {f!r}")

    # ---- 构造两个 Window ----
    five_hour = _to_window_from_item(item, kind="interval")
    weekly = _to_window_from_item(item, kind="weekly")

    # ---- raw_status 透传（v0.1 不解释语义） ----
    # 即使上游 status 是 0（业务成功）也透传，便于 UI / 后续排查
    raw_status = {
        "current_interval_status": item.get("current_interval_status"),
        "current_weekly_status": item.get("current_weekly_status"),
    }

    return Snapshot(
        provider=_PROVIDER,
        status="ok",
        source=_SOURCE,
        five_hour=five_hour,
        weekly=weekly,
        raw_status=raw_status,
    )
