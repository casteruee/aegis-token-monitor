"""可选的累计充值手动登记（v3 方案：DeepSeek 金额卡数据来源之一）。

**背景（方案原文）**：余额接口不等于充值历史；DeepSeek 官方 balance 接口
只提供当前余额（total / granted / topped_up 分项），**没有**累计充值字段。
因此“累计充值”允许由用户据自己的充值记录手动登记，默认留空（“待登记”），
不猜测、不从余额/历史最大余额推断。

文件位置：``<v0.3 根>/config/topup.json``（默认不存在 = 未登记）：
    {"deepseek": {"total_topup": 100.00, "currency": "CNY", "note": "2026-09 手动登记"}}
由用户手动编辑；程序只读，绝不写入该文件。加载失败一律按“未登记”处理。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_CONFIG_FILE = _CONFIG_DIR / "topup.json"


@dataclass(frozen=True)
class TopupConfig:
    """手动登记的累计充值（用户据充值记录填入；默认无 = 未登记）。"""

    total: Optional[float] = None
    currency: str = "CNY"


def load_topup(provider: str, path: Optional[Path] = None) -> Optional[TopupConfig]:
    """读取手动登记的累计充值；任何缺失/坏值 → None（未登记，绝不猜数）。"""
    file = Path(path) if path else _CONFIG_FILE
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("topup: config unreadable (%s)", type(e).__name__)
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(provider)
    if not isinstance(entry, dict):
        return None
    total = entry.get("total_topup")
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total < 0:
        return None
    currency = entry.get("currency")
    if not isinstance(currency, str) or not currency:
        currency = "CNY"
    return TopupConfig(total=float(total), currency=currency)
