"""Aegis 统一数据模型（ARCHITECTURE.md §7.1）。

所有 dataclass 的 ``to_dict()`` 输出必须是 :func:`json.dumps` 可序列化的纯
Python 类型。窗口字段在 provider 没有对应概念时（例如 DeepSeek）必须是
``None``，而不是空字典 —— 参见 ARCHITECTURE §7.1 与本任务硬约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Window:
    """5 小时或周窗口。

    P5 D-1：``resets_at`` 允许为 ``None``（上游未提供，例如 z.ai 5h 窗口刚重
    置时整段字段被省略）。UI 层面对 ``None`` 显示 ``—``。**禁止**任何 reader
    把本地时间或上一轮缓存值塞进 ``resets_at``（反伪装硬约束，详见 ARCH §8）。
    """

    used_percent: float
    remaining_percent: float
    resets_at: Optional[str]  # UTC ISO 8601（如 "2026-09-11T07:51:57Z"）；上游未提供则为 None
    window_minutes: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "resets_at": self.resets_at,
            "window_minutes": self.window_minutes,
        }


@dataclass
class Balance:
    """DeepSeek 余额。

    v0.3 增量字段（可选，默认 None，向后兼容）：
    ``granted_balance`` / ``topped_up_balance`` —— 上游 balance_infos 的原始
    分项（赠送余额 / 充值余额分项，均为**当前余额分项**，不是累计充值历史）。
    仅用于 UI 的金额口径核对（如“含未拆分赠送余额”），不改变 value/currency
    的语义；为 None 时不输出到 dict（保持旧 JSON 形状）。
    """

    value: float
    currency: str
    granted_balance: Optional[float] = None
    topped_up_balance: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"value": self.value, "currency": self.currency}
        if self.granted_balance is not None:
            d["granted_balance"] = self.granted_balance
        if self.topped_up_balance is not None:
            d["topped_up_balance"] = self.topped_up_balance
        return d



@dataclass
class ExtraWindow:
    """CodexBar 透传的附加窗口（``usage.extra_rate_windows[]``）。"""

    id: str
    title: str
    used_percent: float
    resets_at: Optional[str] = None
    reset_description: Optional[str] = None
    is_informational: bool = False
    window_minutes: Optional[int] = None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "title": self.title,
            "used_percent": self.used_percent,
            "is_informational": self.is_informational,
        }
        # 仅在有值时输出对应字段，避免与 probe_raw 真实样例对不上
        d["resets_at"] = self.resets_at
        if self.reset_description is not None:
            d["reset_description"] = self.reset_description
        if self.window_minutes is not None:
            d["window_minutes"] = self.window_minutes
        return d


@dataclass
class ErrorInfo:
    """provider 失败时的结构化错误（消息已脱敏）。"""

    type: str
    message: str

    def to_dict(self) -> dict:
        return {"type": self.type, "message": self.message}


@dataclass
class Snapshot:
    """单个 provider 的归一化快照。"""

    provider: str
    status: str  # "ok" | "error"
    five_hour: Optional[Window] = None
    weekly: Optional[Window] = None
    balance: Optional[Balance] = None
    extras: List[ExtraWindow] = field(default_factory=list)
    updated_at: Optional[str] = None  # 数据源时刻（不是渲染时刻）
    source: str = ""
    reliability: str = "live"  # "live" | "cached" | "stale"
    error: Optional[ErrorInfo] = None
    # MiniMax status 位（v0.1 仅透传，不解释）
    raw_status: Optional[dict] = None

    def to_dict(self) -> dict:
        d: dict = {
            "provider": self.provider,
            "status": self.status,
            # 关键：null 语义，绝不输出空字典
            "five_hour": self.five_hour.to_dict() if self.five_hour is not None else None,
            "weekly": self.weekly.to_dict() if self.weekly is not None else None,
            "balance": self.balance.to_dict() if self.balance is not None else None,
            "extras": [e.to_dict() for e in self.extras],
            "updated_at": self.updated_at,
            "source": self.source,
            "reliability": self.reliability,
            "error": self.error.to_dict() if self.error is not None else None,
        }
        if self.raw_status is not None:
            d["raw_status"] = self.raw_status
        return d


@dataclass
class Report:
    """整轮抓取的统一报告。"""

    generated_at: str  # UTC ISO 8601
    providers: List[Snapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "providers": [p.to_dict() for p in self.providers],
        }
