"""Provider readers registry（ARCHITECTURE §0.1 + §6 + §12 / P2 + P3）。

约束：
- :data:`READERS` 是**普通 dict**（``{"zai": read, ...}``），**不**做插件
  系统、不做抽象工厂、不做 ``BaseReader`` 继承树。
- 每个 reader 形如 ``read() -> aegis.model.Snapshot``（零参、内部自取凭据）。
- P3 把 CodexBar 子进程的 codex reader 也加入（ARCH §5 复用边界）。

注册顺序对应面板 / 缓存 / 验收命令展示顺序（Codex 在第一位：唯一子进程
特例 + 凭据由 CodexBar 自理）。
"""

from aegis.providers.codex import read as read_codex
from aegis.providers.deepseek import read as read_deepseek
from aegis.providers.minimax import read as read_minimax
from aegis.providers.zai import read as read_zai


READERS = {
    "codex": read_codex,
    "zai": read_zai,
    "minimax": read_minimax,
    "deepseek": read_deepseek,
}


__all__ = ["READERS"]
__version__ = "0.1.0"