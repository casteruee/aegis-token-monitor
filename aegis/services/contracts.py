"""服务层数据契约（UI_NEXT_VERSION_DESIGN §8 / §13 contracts.py）。

三个设计维度（不改动 v0.1 Snapshot 的 status/reliability 字段）：

- ``freshness``：LIVE / CACHED / STALE / NONE —— 数据来自本轮、有效缓存、
  过期缓存（附带失败）、还是从未成功。
- ``completeness``：FULL / PARTIAL —— 支持字段是否齐全（按能力矩阵）。
- ``availability``：OK / ERROR / UNAVAILABLE —— 数据面是否可用。

时间信封（TimeEnvelope）：source_updated_at / last_success_fetched_at /
last_attempt_at 三个时刻**分开保存**；源时刻未知绝不填本地时刻（D-007）。

安全错误映射：只传递**固定错误码 + 白名单参数（HTTP 状态码数字）**，
绝不把 exception 原文 / stderr / headers / raw JSON 传给 UI 或历史库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 设计状态枚举（字符串常量，避免 enum 序列化负担）
# ---------------------------------------------------------------------------

FRESH_LIVE = "LIVE"
FRESH_CACHED = "CACHED"
FRESH_STALE = "STALE"
FRESH_NONE = "NONE"

COMPLETE_FULL = "FULL"
COMPLETE_PARTIAL = "PARTIAL"

AVAIL_OK = "OK"
AVAIL_ERROR = "ERROR"
AVAIL_UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# 能力矩阵（UI_NEXT_VERSION_DESIGN §3；reader 解析契约，不保证上游永远提供）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderCapability:
    """单个 provider 的展示能力声明。"""

    display_name: str
    supports_five_hour: bool
    supports_weekly: bool
    supports_balance: bool
    # Reset 是否为 reader 的必要字段（False = 上游可能合法缺失，如 z.ai）
    reset_required: bool


CAPABILITIES: dict[str, ProviderCapability] = {
    "codex": ProviderCapability(
        display_name="Codex",
        supports_five_hour=True,
        supports_weekly=True,
        supports_balance=False,
        reset_required=True,
    ),
    "zai": ProviderCapability(
        display_name="z.ai / GLM",
        supports_five_hour=True,
        supports_weekly=True,
        supports_balance=False,
        reset_required=False,  # D-006：窗口刚重置时上游省略 nextResetTime
    ),
    "minimax": ProviderCapability(
        display_name="MiniMax",
        supports_five_hour=True,
        supports_weekly=True,
        supports_balance=False,
        reset_required=True,
    ),
    "deepseek": ProviderCapability(
        display_name="DeepSeek",
        supports_five_hour=False,   # 官方无窗口概念；禁止伪造
        supports_weekly=False,
        supports_balance=True,
        reset_required=False,
    ),
}

PROVIDER_ORDER = ["codex", "zai", "minimax", "deepseek"]


# ---------------------------------------------------------------------------
# 固定错误码 → 中文文案（安全映射的唯一来源）
# ---------------------------------------------------------------------------

SAFE_ERROR_TEXT: dict[str, str] = {
    "missing_credential": "凭据来源不可读（只读位置缺失）",
    "http_error": "上游返回错误",
    "http_rate_limited": "上游限流，稍后自动重试",
    "network_error": "网络连接失败",
    "parse_error": "响应格式无法解析",
    "unexpected_shape": "上游数据结构变化",
    "codex_source_unavailable": "CodexBar 数据源不可用",
    "SERVICE_UNAVAILABLE": "服务端标记暂不可用",
    "timeout": "采集超时",
    "internal_error": "内部错误",
}


def safe_error_text(code: str, *, http_status: Optional[int] = None) -> str:
    """错误码 → 面板中文文案。

    只允许 ``http_status``（整数）作为插值参数；任何其它上游文本都不得进入。
    未知错误码落回 ``internal_error`` 文案，**绝不**透传原文。
    """
    if code == "http_error" and isinstance(http_status, int):
        return f"{SAFE_ERROR_TEXT['http_error']}（HTTP {http_status}）"
    return SAFE_ERROR_TEXT.get(code, SAFE_ERROR_TEXT["internal_error"])


# ---------------------------------------------------------------------------
# 时间信封
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimeEnvelope:
    """三个时刻分开保存；均为 UTC ISO 字符串或 None（未知绝不伪造）。"""

    source_updated_at: Optional[str] = None      # 源声明的数据时刻
    last_success_fetched_at: Optional[str] = None  # 本应用最近一次成功采集
    last_attempt_at: Optional[str] = None          # 最近一次尝试（含失败）


# ---------------------------------------------------------------------------
# 快照信封（服务 → 适配器 → ViewModel 的唯一数据形状）
# ---------------------------------------------------------------------------

@dataclass
class SnapshotEnvelope:
    """单个 provider 的完整展示输入。"""

    provider: str
    snapshot: object = None               # aegis.model.Snapshot（已验证）或 None
    freshness: str = FRESH_NONE
    completeness: str = COMPLETE_FULL
    availability: str = AVAIL_UNAVAILABLE
    time: TimeEnvelope = field(default_factory=TimeEnvelope)
    # 安全错误：固定码 + 可选 HTTP 状态数字；无原文
    error_code: Optional[str] = None
    error_http_status: Optional[int] = None
    loading: bool = False                  # 在途请求叠加态，独立于上面三维度

    def error_display_text(self) -> str:
        if self.error_code is None:
            return ""
        return safe_error_text(self.error_code, http_status=self.error_http_status)


# ---------------------------------------------------------------------------
# 完整性判定（按能力矩阵；纯函数）
# ---------------------------------------------------------------------------

def compute_completeness(provider: str, snapshot) -> str:
    """ok 快照按能力矩阵判 FULL / PARTIAL。

    PARTIAL 条件（设计 §8）：**支持的字段**缺失，或支持 Reset 的窗口缺 Reset。
    不支持的字段缺失（DeepSeek 无窗口）不算 PARTIAL —— 那是 UNSUPPORTED 字段层。
    """
    cap = CAPABILITIES[provider]
    if snapshot is None or getattr(snapshot, "status", "error") != "ok":
        return COMPLETE_PARTIAL

    def _win_missing(win, need_reset: bool) -> bool:
        if win is None:
            return True
        if need_reset and getattr(win, "resets_at", None) is None:
            return True
        return False

    partial = False
    if cap.supports_five_hour and _win_missing(snapshot.five_hour, cap.reset_required):
        partial = True
    if cap.supports_weekly and _win_missing(snapshot.weekly, cap.reset_required):
        partial = True
    if cap.supports_balance and snapshot.balance is None:
        partial = True
    return COMPLETE_PARTIAL if partial else COMPLETE_FULL


def http_status_from_snapshot(snapshot) -> Optional[int]:
    """从 snapshot.error.message 里提取白名单参数：HTTP 状态码数字。

    v0.1 reader 的 ``http_error`` message 形如 ``HTTP 401``（zai/deepseek/
    minimax 统一格式 ``f"HTTP {e.status}"``）。这里只提取**纯数字**，其余
    一概忽略。codex 的 stderr 摘要不会被传入（固定码 + rc 不解析）。
    """
    err = getattr(snapshot, "error", None)
    if err is None:
        return None
    msg = getattr(err, "message", "") or ""
    import re
    m = re.search(r"HTTP (\d{3})", msg)
    if m:
        return int(m.group(1))
    return None
