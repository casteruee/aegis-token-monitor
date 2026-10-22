"""z.ai / GLM 配额 reader（ARCHITECTURE §2 + §6 + §13 + P5 D-1）。

直连官方配额接口（**不**复用 CodexBar ``-p zai``，理由见 ARCH §2）：

    GET https://api.z.ai/api/monitor/usage/quota/limit
    Authorization: Bearer <key>

响应结构（``probe_raw/zai.json`` + ``tests/fixtures/zai_quota.json``）：

    {"code": 200, "data": {"level": "lite", "limits": [
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
         "usage": 2000, "currentValue": 8, "remaining": 1991,
         "percentage": 1, "nextResetTime": 1789112389135},
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
         "usage": 10000, "currentValue": 9590, "remaining": 409,
         "percentage": 95, "nextResetTime": 1789193655997}
    ]}}

- ``unit == 3``  → 5 小时窗口；``unit == 6`` → weekly。
- ``percentage`` = **已用 %** → 必须 ``normalize.from_used``（P2 §1 硬约束）。
- ``nextResetTime`` = epoch **毫秒** → ``normalize.epoch_ms_to_utc_iso``。
- **P5 D-1（Hermes 真机验收发现，2026-09-11 15:56）**：5h 窗口刚重置、用量
  为 0 时上游会**整体省略** ``percentage`` 与 ``nextResetTime`` 两个键（不是
  给 0）。此时视为正常"窗口已重置"，**禁止**报 ``unexpected_shape``：
    * ``percentage`` 缺失 → 若 ``usage`` 存在且 ``> 0``，则按
      ``currentValue / usage * 100`` 计算已用百分比；否则取 ``0.0``。计算后
      仍走 ``normalize.from_used``（**保持唯一翻转入口**）。
    * ``nextResetTime`` 缺失 → 该窗口 ``resets_at = None``（表示上游未提
      供，UI 显示 ``—``）。**禁止**用本地时间或上一轮缓存值伪造。
- 缺 ``unit=3`` **或** ``unit=6`` 单个条目（P5 D-1 放宽）：该窗口为 ``None``，
  另一窗口照常解析，整体仍 ``status='ok'``。**两者都缺** 才是结构性错误
  → ``UnexpectedShape``（不静默补 null 的硬约束仍然有效）。
- 备用域 ``https://open.bigmodel.cn/...``：**仅**在
  :class:`aegis.net.NetworkError` 时切换重试一次；:class:`aegis.net.HttpError`
  （401 / 403 ...）**不得**切换 —— 换域名解决不了认证问题。
- ``source = "zai-api"``；``data.level`` 通过 ``Snapshot.raw_status`` 透传
  （arch §7.1 + P2 §3 —— ``model.py`` 已锁定，不新增顶层字段）。
"""

from __future__ import annotations

from typing import Optional

from aegis import credentials
from aegis import net
from aegis import normalize
from aegis.model import Snapshot, Window, ErrorInfo


# URL 常量（测试断言依赖这些值，所以必须 module-level 且不可变）
PRIMARY_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
BACKUP_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"

# z.ai 业务常量
_PROVIDER = "zai"
_SOURCE = "zai-api"
_TYPE_CREDIT = "CREDIT_LIMIT"
_UNIT_5H = 3
_UNIT_WEEKLY = 6


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _error_snapshot(err_type: str, message: str) -> Snapshot:
    """构造一个 status=error 的快照；message 必须已经脱敏。"""
    return Snapshot(
        provider=_PROVIDER,
        status="error",
        source=_SOURCE,
        error=ErrorInfo(type=err_type, message=message),
    )


def _fetch_with_fallback(key: str):
    """主域失败时按 M-3 规则切换备用域一次。

    :class:`aegis.net.HttpError` **不**触发备用域（M-3 + ROUTE_REVIEW M-3）。
    """
    try:
        return net.http_get_json(PRIMARY_URL, key)
    except net.NetworkError as primary_err:
        # 仅连接层失败才换域
        try:
            return net.http_get_json(BACKUP_URL, key)
        except net.NetworkError as backup_err:
            raise net.NetworkError(
                f"primary+backup NetworkError: primary={primary_err}, backup={backup_err}"
            ) from None


def _compute_used_pct_when_missing(item: dict) -> float:
    """``percentage`` 缺失时（P5 D-1），按 ``currentValue / usage * 100`` 算出已用 %。

    规则：
    - ``usage`` 存在且 ``> 0`` → ``currentValue / usage * 100``（保留为 float）。
    - ``usage`` 缺失 / 不可转为数值 / ``<= 0`` → ``0.0``（窗口刚重置 / 用量为 0）。
    - ``currentValue`` 缺失 / 不可转为数值 → ``0.0``（上游没报，保守取 0）。
    - 计算后**仍**交给 :func:`normalize.from_used` 走唯一翻转入口；本函数
      只负责"拿到已用 %"，不直接构造 ``Window``。
    - 数值健壮性：``currentValue = 0`` / ``remaining = 0`` 是合法值，**禁止**
      用 ``or`` / falsy 判断把它们变成其它值。
    """
    raw_usage = item.get("usage")
    raw_current = item.get("currentValue")

    # 任一不可转为数值 → 直接 0.0（保守）
    try:
        usage_f = float(raw_usage) if raw_usage is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    try:
        current_f = float(raw_current) if raw_current is not None else 0.0
    except (TypeError, ValueError):
        return 0.0

    if usage_f <= 0:
        # 上游未给出 usage / usage 为 0 → 不能除；窗口刚重置取 0
        return 0.0
    return current_f / usage_f * 100.0


def _to_window_from_limit(item: dict) -> Window:
    """从一条 CREDIT_LIMIT 条目构造 ``Window``（P5 D-1 容错版本）。

    容错（Hermes 真机验收发现，2026-09-11 15:56）：
    - ``percentage`` 缺失 → 由 :func:`_compute_used_pct_when_missing` 算出
      已用 %；仍走 :func:`normalize.from_used`（**唯一翻转入口**硬约束）。
    - ``nextResetTime`` 缺失 → ``resets_at = None``（上游未提供；UI 显示
      ``—``）。**禁止**用本地时间或上一轮缓存值伪造。
    """
    raw_pct = item.get("percentage")
    if raw_pct is None:
        used_pct = _compute_used_pct_when_missing(item)
    else:
        used_pct = raw_pct
    used, remaining = normalize.from_used(used_pct)

    raw_reset = item.get("nextResetTime")
    resets_at: Optional[str] = None
    if raw_reset is not None:
        resets_at = normalize.epoch_ms_to_utc_iso(raw_reset)

    return Window(
        used_percent=used,
        remaining_percent=remaining,
        resets_at=resets_at,
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def read() -> Snapshot:
    """读取 z.ai 配额，归一化为 :class:`Snapshot`。

    永远不会抛异常（**所有**失败路径都映射为 ``status='error'`` 快照；
    见 ARCH §7.1 与 P2 测试要求）。
    """
    # 1) 凭据（缺凭据 → error 快照，不抛）
    try:
        key = credentials.get_key(_PROVIDER)
    except credentials.MissingCredential as e:
        return _error_snapshot("missing_credential", str(e))

    # 2) 网络 / 解析错误
    try:
        data = _fetch_with_fallback(key)
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
# 归一化（与 model.py §7.1 一致）
# ---------------------------------------------------------------------------

def _to_snapshot(data: dict) -> Snapshot:
    # 顶层 ``code`` 校验（z.ai 用 200 表示业务成功，但 HTTP 已经是 200）
    if data.get("code") != 200:
        raise normalize.UnexpectedShape(
            f"zai: top-level code != 200, got {data.get('code')!r}"
        )

    body = data.get("data")
    if not isinstance(body, dict):
        raise normalize.UnexpectedShape("zai: missing 'data' object")

    level = body.get("level")

    limits = body.get("limits")
    if not isinstance(limits, list):
        raise normalize.UnexpectedShape("zai: 'data.limits' missing or not a list")

    # 仅 ``type == 'CREDIT_LIMIT'`` 的条目参与归一化（README 配方）
    credit_limits = [
        item for item in limits
        if isinstance(item, dict) and item.get("type") == _TYPE_CREDIT
    ]

    five_hour = None
    weekly = None
    for item in credit_limits:
        unit = item.get("unit")
        if unit == _UNIT_5H and five_hour is None:
            win = _to_window_from_limit(item)
            five_hour = Window(
                used_percent=win.used_percent,
                remaining_percent=win.remaining_percent,
                resets_at=win.resets_at,
                window_minutes=300,
            )
        elif unit == _UNIT_WEEKLY and weekly is None:
            win = _to_window_from_limit(item)
            weekly = Window(
                used_percent=win.used_percent,
                remaining_percent=win.remaining_percent,
                resets_at=win.resets_at,
                window_minutes=10080,
            )
        # 其他 unit（如 number != 5/1 的）忽略

    # P5 D-1：单个 unit 缺失 → 该窗口为 None，另一窗口照常解析，**整体
    # 仍 status='ok'**。仅当 unit=3 与 unit=6 **同时**缺失才报 unexpected_shape
    # （"不静默补 null"硬约束仍然有效 —— 两者都缺意味着上游没给出任何窗口
    # 信息，不是真机常见的"窗口刚重置"）。
    if five_hour is None and weekly is None:
        raise normalize.UnexpectedShape(
            f"zai: no CREDIT_LIMIT entries with unit={_UNIT_5H} (5h) "
            f"or unit={_UNIT_WEEKLY} (weekly)"
        )

    # level 字段透传进 snapshot（不解释语义；model.py 已锁，无顶层字段可用）
    raw_status = {"level": level} if level is not None else None

    return Snapshot(
        provider=_PROVIDER,
        status="ok",
        source=_SOURCE,
        five_hour=five_hour,
        weekly=weekly,
        raw_status=raw_status,
    )