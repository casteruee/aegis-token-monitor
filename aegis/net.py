"""HTTP wrapper for the self-written readers (ARCHITECTURE §2 / §3 / §4 / §13).

唯一对外接口：``http_get_json(url, key, timeout=10) -> dict``。

设计约束（README + ARCHITECTURE + ROUTE_REVIEW 锁定）：

- 只发 ``Authorization: Bearer <key>`` 头。**key 绝不**进 URL、日志、异常
  文本、``to_dict()`` 输出。
- 异常分类（ARCH §13 ERROR_HANDLING）：
  * 非 200 → :class:`HttpError`（含 4xx / 5xx）；
  * 连接 / DNS / 超时 → :class:`NetworkError`（URLError 子树，但不含 HTTP
    状态码错误）；
  * 响应体不是合法 JSON object → :class:`ParseError`。
- **M-3**：*不对* 429 / 5xx 做任何重试或退避 —— 直接抛 :class:`HttpError`。
- 异常消息只允许包含：``host + status + 异常类名 + 响应体前 200 字符的脱敏副本``
  （含 ``sk-`` / ``Bearer`` 模式的必须替换为 ``<redacted>``）。
- 返回值必须是 ``dict``；数组、标量、``null`` 一律视为结构漂移（``ParseError``）。
"""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError as _URLLibHTTPError
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen


# ---------------------------------------------------------------------------
# 异常类型（与 ARCHITECTURE §13 错误分类一致）
# ---------------------------------------------------------------------------

class HttpError(Exception):
    """非 200 响应；``status`` 属性必须可读。

    M-3：429 / 5xx 也走这条路径，不再重试、也不退避。
    """

    def __init__(self, status, message=""):
        if not isinstance(status, int) or status < 100 or status >= 600:
            raise ValueError(
                f"HttpError status must be in [100, 600), got {status!r}"
            )
        self.status = status
        super().__init__(message or f"HTTP {status}")


class NetworkError(Exception):
    """连接层失败：DNS / 连接被拒 / 读超时 / 协议错误。

    不包含 4xx / 5xx（那些走 :class:`HttpError`）。
    """


class ParseError(Exception):
    """响应体不是合法的 JSON object（数组 / null / 标量 / 畸形 JSON）。"""


# ---------------------------------------------------------------------------
# 响应体脱敏（与 ``aegis.credentials._DEEPSEEK_KEY_RE`` 语义保持一致）
# ---------------------------------------------------------------------------

# 吞掉完整 sk- 段（含 sk-cp- 、sk-cp-TEST-x 等）。
_REDACT_SK = re.compile(r"sk-[A-Za-z0-9_\-]+")
# ``Bearer xxx`` 整段（含 ``Authorization: Bearer abc`` 与裸 ``Bearer abc``）。
_REDACT_BEARER = re.compile(r"(?i)\bBearer\b[ \t]+\S+")


def _redact(body: str) -> str:
    """把响应体里偶然出现的 sk- 段 / Bearer token 替换为 ``<redacted>``。"""
    if not body:
        return ""
    out = _REDACT_SK.sub("<redacted>", body)
    out = _REDACT_BEARER.sub("Bearer <redacted>", out)
    return out


def _preview(body, n: int = 200) -> str:
    """响应体截断为前 ``n`` 字符的脱敏副本；尾部补 ``...`` 提示截断。"""
    if body is None:
        return ""
    s = body if isinstance(body, str) else str(body)
    s = _redact(s)
    if len(s) > n:
        return s[:n] + "..."
    return s


def _host(url: str) -> str:
    """仅取 ``scheme://host[:port]``，绝不包含路径 / query / fragment / key。"""
    try:
        netloc = urlparse(url).netloc
    except Exception:
        return ""
    return netloc or ""


def _read_body(resp) -> str:
    """读取响应体并解码为 ``str``（bytes 时按 utf-8 / utf-8-sig / latin-1 顺序尝试）。"""
    data = resp.read()
    if data is None:
        return ""
    if isinstance(data, bytes):
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    return str(data)


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def http_get_json(url: str, key: str, timeout: float = 10):
    """``GET <url>`` 带 Bearer 头，解析为 JSON object 并返回。

    :raises NetworkError: 连接层失败。
    :raises HttpError: 非 200 状态码（M-3：不重试 / 不退避）。
    :raises ParseError: 响应体不是合法 JSON object。
    """
    # 输入校验：空 url / 空 key 都是「凭据 / 配置层」故障，对外表现为 NetworkError。
    if not isinstance(url, str) or not url:
        raise NetworkError("http_get_json: empty url")
    if not isinstance(key, str) or not key:
        raise NetworkError("http_get_json: empty key")

    host = _host(url)

    # 仅在这一个位置构造请求头；key 只在这里出现。
    req = Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "aegis-token-monitor/0.1 (stdlib urllib)")

    # ---------- 打开连接 ----------
    try:
        ctx = urlopen(req, timeout=timeout)  # noqa: S310 - 调用方传入 URL，可信
    except _URLLibHTTPError as e:
        # urllib 把非 200 状态码映射为 HTTPError；fp 可能含 body。
        body = ""
        try:
            body = _read_body(e)
        except Exception:
            # 读取 body 失败也要继续 —— status 才是分类关键。
            pass
        status = int(getattr(e, "code", 0) or 0)
        msg = (
            f"host={host} status={status} err={type(e).__name__} "
            f"body={_preview(body)!r}"
        )
        raise HttpError(status=status, message=msg) from None
    except URLError as e:
        # ``e.reason`` 可能是任何字符串（含 ``sk-...`` 子串），必须先脱敏。
        reason = getattr(e, "reason", "")
        reason_str = _redact(str(reason) if reason is not None else "")
        raise NetworkError(
            f"host={host} err={type(e).__name__} reason={reason_str!r}"
        ) from None
    except (TimeoutError, OSError) as e:
        # TimeoutError / ConnectionRefusedError / OSError(Connection reset) ...
        # ``str(e)`` 可能含 key —— 一并脱敏。
        msg = _redact(str(e))
        raise NetworkError(
            f"host={host} err={type(e).__name__} detail={msg!r}"
        ) from None

    # ---------- 读 body ----------
    try:
        with ctx as resp:
            body_str = _read_body(resp)
    except Exception as e:
        raise NetworkError(
            f"host={host} err=BodyReadFailed cls={type(e).__name__}"
        ) from None

    # ---------- 解析 JSON ----------
    try:
        parsed = json.loads(body_str)
    except json.JSONDecodeError as e:
        raise ParseError(
            f"host={host} err=JSONDecodeError body={_preview(body_str)!r}"
        ) from None
    except (TypeError, ValueError) as e:
        # json.loads 在遇到 None / 非字符串 body 时抛这些。
        raise ParseError(
            f"host={host} err={type(e).__name__} body={_preview(body_str)!r}"
        ) from None

    if not isinstance(parsed, dict):
        raise ParseError(
            f"host={host} err=NonObjectJson body={_preview(body_str)!r}"
        )
    return parsed
