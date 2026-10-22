"""Aegis Token Monitor v0.1 —— 纯逻辑层（P1）。

模块：
    aegis.model        统一数据模型（dataclass + to_dict）。
    aegis.normalize    纯函数：used/remaining 语义转换、时间归一化、窗口跨度断言。
    aegis.credentials  只读读取 z.ai / MiniMax / DeepSeek 凭据。

约束（README + ARCHITECTURE + ROUTE_REVIEW 锁定）：
- 零第三方依赖；只允许使用 Python 标准库。
- 凭据只在内存中流转，禁止打印 / 写入 / 编码进任何对外输出。
- 时间一律 UTC ISO 8601；naive datetime 必须报错。
- `100 - x` 翻转只允许发生在 ``aegis.normalize``，其它模块禁出现。
"""

__all__ = ["model", "normalize", "credentials"]
__version__ = "0.1.0"
