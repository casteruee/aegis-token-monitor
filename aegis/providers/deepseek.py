"""DeepSeek balance-only reader（ARCHITECTURE §4 + §6 + §13）。

直连（非 CodexBar，D-004 确定）：

    GET https://api.deepseek.com/user/balance
    Authorization: Bearer <sk-... key>

响应（``probe_raw/deepseek.json`` + ``tests/fixtures/deepseek_balance.json``）：

    {"is_available": true,
     "balance_infos": [
        {"currency": "CNY", "total_balance": "74.16",
         "granted_balance": "0.00", "topped_up_balance": "74.16"}
     ]}

硬约束：
- ``balance_infos[0]`` → ``Balance{value: float(total_balance), currency}``。
- ``five_hour = None`` / ``weekly = None``（DeepSeek 官方没有 5h / Weekly /
  Reset 概念；**禁止伪造**）。
- ``is_available == false`` → ``status='error'``，``error.type = 'SERVICE_UNAVAILABLE'``。
- ``balance_infos`` 缺失 / 为空 → :class:`normalize.UnexpectedShape`。
- ``source = "deepseek-api"``。
"""

from __future__ import annotations

from aegis import credentials
from aegis import net
from aegis import normalize
from aegis.model import Snapshot, Balance, ErrorInfo


URL = "https://api.deepseek.com/user/balance"

_PROVIDER = "deepseek"
_SOURCE = "deepseek-api"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _error_snapshot(err_type: str, message: str) -> Snapshot:
    return Snapshot(
        provider=_PROVIDER,
        status="error",
        source=_SOURCE,
        five_hour=None,    # 显式置 None（反伪装）
        weekly=None,
        balance=None,
        error=ErrorInfo(type=err_type, message=message),
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def read() -> Snapshot:
    """读取 DeepSeek 余额，归一化为 :class:`Snapshot`。

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
    except (normalize.UnexpectedShape, TypeError, ValueError) as e:
        return _error_snapshot("unexpected_shape", str(e))


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def _to_snapshot(data: dict) -> Snapshot:
    # ``is_available == false`` → error 态（SERVICE_UNAVAILABLE）
    if data.get("is_available") is False:
        return Snapshot(
            provider=_PROVIDER,
            status="error",
            source=_SOURCE,
            five_hour=None,
            weekly=None,
            balance=None,
            error=ErrorInfo(
                type="SERVICE_UNAVAILABLE",
                message="is_available=false from upstream",
            ),
        )
    # 缺 is_available 字段视为 true / 缺失容错（继续走 balance 解析；
    # 若 balance_infos 也缺，末尾的 UnexpectedShape 兜底）。

    balance_infos = data.get("balance_infos")
    if not isinstance(balance_infos, list) or not balance_infos:
        raise normalize.UnexpectedShape(
            "deepseek: 'balance_infos' missing or empty"
        )

    first = balance_infos[0]
    if not isinstance(first, dict):
        raise normalize.UnexpectedShape(
            "deepseek: balance_infos[0] is not an object"
        )

    total = first.get("total_balance")
    currency = first.get("currency")
    if total is None or currency is None:
        raise normalize.UnexpectedShape(
            "deepseek: total_balance or currency missing"
        )

    try:
        value = float(total)
    except (TypeError, ValueError) as e:
        raise normalize.UnexpectedShape(
            f"deepseek: total_balance not numeric: {total!r}"
        ) from e

    # v0.3 增量（可选分项，均为当前余额分项、非累计充值历史；缺失/坏值→None，
    # 不致命）：granted_balance=赠送余额分项，topped_up_balance=充值余额分项。
    # 仅用于 UI 金额口径核对（“含未拆分赠送余额”）。
    def _opt_float(key: str) -> Optional[float]:
        raw = first.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return Snapshot(
        provider=_PROVIDER,
        status="ok",
        source=_SOURCE,
        five_hour=None,    # 硬约束：DeepSeek 无 5h 概念
        weekly=None,       # 硬约束：DeepSeek 无 Weekly 概念
        balance=Balance(
            value=value,
            currency=currency,
            granted_balance=_opt_float("granted_balance"),
            topped_up_balance=_opt_float("topped_up_balance"),
        ),
    )
