"""只读凭据读取（ARCHITECTURE.md §6 + ROUTE_REVIEW.md M-5）。

约定（硬）：
- 全部以 :py:func:`get_key` 单一入口；不允许在别处直接读 ``~/.codex/auth.json``、
  CC Switch、Hermes、pi auth.json 等。
- cc-switch.db 用 ``sqlite3.connect("file:<path>?mode=ro", uri=True)`` 只读打开；
  禁止 ``mode=rw``。
- DeepSeek hermes config.yaml 的 ``api_key`` 可能带前缀文本，**必须**用正则
  ``sk-[A-Za-z0-9_\\-]+`` 抽取 sk- 段（实测带前缀直发 401）。
- :class:`MissingCredential` 的消息只能指路（文件 / 字段 / 原因），不得包含
  凭据明文或前缀片段。
- 不打印、不写盘、不进日志 / 异常文本 / ``to_dict()`` 的输出。

测试便利性：所有路径集中在模块级 :data:`_PATHS` 字典中，pytest 用
``monkeypatch.setitem`` 注入临时文件即可，不依赖真机环境。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Optional


class MissingCredential(Exception):
    """凭据缺失或不可读。消息必须不含凭据内容。"""


# ---------------------------------------------------------------------------
# 路径表（默认值在导入时计算；测试可整体或逐项 monkeypatch）
# ---------------------------------------------------------------------------

_PATHS = {
    "ccs_db": os.path.expanduser("~/.cc-switch/cc-switch.db"),
    "pi_auth": os.path.expanduser("~/.pi/agent/auth.json"),
    "hermes_config": os.path.expanduser("~/AppData/Local/hermes/config.yaml"),
}

# DeepSeek hermes config / cc-switch.db 路径下提取 sk- 段。
# 负向回顾：只匹配「不在 [A-Za-z0-9-] 之后」的 sk-。避免把
# "no-sk-segment-here" 这类值里偶然出现的 "sk-..." 子串当成 key
# （带前缀直发 401 的实测失败模式：前缀是 "Bearer ..." 或乱码，
# 而不是「合法 key 中段偶然含 -sk-」）。
_DEEPSEEK_KEY_RE = re.compile(r"(?<![A-Za-z0-9\-])sk-[A-Za-z0-9_\-]+")


# ---------------------------------------------------------------------------
# 低层 cc-switch.db / pi auth.json / hermes config 读取
# ---------------------------------------------------------------------------

def _open_ccs_db(path: str) -> sqlite3.Connection:
    """以 ``mode=ro`` URI 只读打开 SQLite；失败抛指路型 :class:`MissingCredential`。"""
    if not path or not os.path.isfile(path):
        raise MissingCredential(
            f"cc-switch.db not found at {path!r}. "
            "Verify CC Switch is installed and the DB path is correct."
        )
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise MissingCredential(
            f"failed to open cc-switch.db read-only at {path!r}: {type(e).__name__}"
        )


def _ccs_query(
    path: str,
    app_type: str,
    name: str,
    *,
    field_kind: str,
) -> str:
    """按 ``(app_type, name)`` 取 ``settings_config`` 中的指定字段。

    ``field_kind='ANTHROPIC_AUTH_TOKEN'`` → ``settings_config.env.ANTHROPIC_AUTH_TOKEN``
    （Claude 系 provider）；``field_kind='api_key'`` → ``settings_config.api_key``
    （Hermes 系 provider）。
    """
    conn = _open_ccs_db(path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT settings_config FROM providers "
            "WHERE app_type=? AND name=? LIMIT 1",
            (app_type, name),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise MissingCredential(
            f"cc-switch.db ({path!r}) has no provider with "
            f"app_type={app_type!r} name={name!r}. "
            "Check the exact 'name' field in CC Switch UI."
        )

    try:
        obj = json.loads(row[0])
    except json.JSONDecodeError as e:
        raise MissingCredential(
            f"cc-switch.db ({path!r}) settings_config is not valid JSON "
            f"for app_type={app_type!r} name={name!r}: {type(e).__name__}"
        )

    if field_kind == "ANTHROPIC_AUTH_TOKEN":
        env = obj.get("env")
        if not isinstance(env, dict) or "ANTHROPIC_AUTH_TOKEN" not in env:
            raise MissingCredential(
                f"cc-switch.db ({path!r}) settings_config.env.ANTHROPIC_AUTH_TOKEN "
                f"missing for app_type={app_type!r} name={name!r}."
            )
        token = env["ANTHROPIC_AUTH_TOKEN"]
    elif field_kind == "api_key":
        if "api_key" not in obj:
            raise MissingCredential(
                f"cc-switch.db ({path!r}) settings_config.api_key "
                f"missing for app_type={app_type!r} name={name!r}."
            )
        token = obj["api_key"]
    else:
        raise MissingCredential(
            f"unsupported field_kind={field_kind!r}"
        )

    if not isinstance(token, str) or not token:
        raise MissingCredential(
            f"cc-switch.db ({path!r}) {field_kind} is empty or non-string "
            f"for app_type={app_type!r} name={name!r}."
        )
    return token


def _read_pi_auth(path: str) -> dict:
    """读取并解析 pi auth.json，失败抛指路型 :class:`MissingCredential`。"""
    if not path or not os.path.isfile(path):
        raise MissingCredential(
            f"pi auth.json not found at {path!r}. "
            "Set up MiniMax in ~/.pi/agent/auth.json or use CC Switch fallback."
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise MissingCredential(
            f"failed to read pi auth.json at {path!r}: {type(e).__name__}"
        )
    except json.JSONDecodeError as e:
        raise MissingCredential(
            f"pi auth.json at {path!r} is not valid JSON: {type(e).__name__}"
        )
    if not isinstance(data, dict):
        raise MissingCredential(
            f"pi auth.json at {path!r} root must be a JSON object"
        )
    return data


def _read_hermes_config(path: str) -> str:
    """读取 hermes config.yaml 全文，失败抛指路型 :class:`MissingCredential`。"""
    if not path or not os.path.isfile(path):
        raise MissingCredential(
            f"hermes config.yaml not found at {path!r}. "
            "Verify the Hermes installation path."
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise MissingCredential(
            f"failed to read hermes config at {path!r}: {type(e).__name__}"
        )


# ---------------------------------------------------------------------------
# hermes config.yaml 中的 deepseek 块定位（两段式正则，避免引入 PyYAML）
# ---------------------------------------------------------------------------

def _extract_deepseek_block(text: str, target_name: str = "deepseek") -> str:
    """定位 ``custom_providers`` 下 ``name: <target_name>`` 的 YAML 块文本。"""
    lines = text.splitlines()

    # 1) 顶层 `custom_providers:` 锚点
    cp_idx: Optional[int] = None
    cp_indent: Optional[int] = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("custom_providers"):
            cp_idx = i
            cp_indent = len(ln) - len(ln.lstrip())
            break

    if cp_idx is None or cp_indent is None:
        raise MissingCredential(
            "hermes config.yaml has no 'custom_providers:' section."
        )

    # 2) 紧跟其后，找缩进为 cp_indent+2 的列表项 `- name: <target_name>`
    list_indent = cp_indent + 2
    item_start: Optional[int] = None
    for j in range(cp_idx + 1, len(lines)):
        ln = lines[j]
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        cur_indent = len(ln) - len(ln.lstrip())
        # 退出 custom_providers 块
        if cur_indent <= cp_indent:
            break
        if cur_indent == list_indent and s.startswith("- "):
            content_after_dash = s[2:].strip()
            if content_after_dash == f"name: {target_name}" or content_after_dash.startswith(
                f"name: {target_name} "
            ):
                item_start = j
                break

    if item_start is None:
        raise MissingCredential(
            f"hermes config.yaml custom_providers has no entry with "
            f"name={target_name!r}."
        )

    # 3) 向下收集块，直到下一项 `-` 或退出 custom_providers
    end = len(lines)
    for k in range(item_start + 1, len(lines)):
        ln = lines[k]
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        cur_indent = len(ln) - len(ln.lstrip())
        if cur_indent <= cp_indent:
            end = k
            break
        if cur_indent == list_indent and s.startswith("- "):
            end = k
            break

    return "\n".join(lines[item_start:end])


def _extract_api_key_value(block: str) -> str:
    """从 deepseek YAML 块中抽出 ``api_key:`` 右侧的值（去引号与尾随注释）。"""
    pattern = re.compile(
        r"^[ \t]*api_key[ \t]*:[ \t]*(?P<val>.+?)\s*(?:#.*)?$",
        re.MULTILINE,
    )
    m = pattern.search(block)
    if m is None:
        raise MissingCredential(
            "hermes config.yaml deepseek block has no 'api_key' field."
        )
    raw = m.group("val").strip()
    # 去掉外层对称引号
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    return raw


# ---------------------------------------------------------------------------
# 三个 provider 的私有提取（M-5 优先级表）
# ---------------------------------------------------------------------------

def _zai_key() -> str:
    """z.ai / GLM 凭据：cc-switch.db → ``app_type='claude'`` 且
    ``name='Zhipu GLM en'`` → ``settings_config.env.ANTHROPIC_AUTH_TOKEN``。"""
    return _ccs_query(
        _PATHS["ccs_db"],
        app_type="claude",
        name="Zhipu GLM en",
        field_kind="ANTHROPIC_AUTH_TOKEN",
    )


def _minimax_key() -> str:
    """MiniMax 凭据（M-5 优先级）：
    1. ``~/.pi/agent/auth.json`` → ``minimax.key``
    2. 回退 cc-switch.db → ``app_type='claude'`` 且 ``name='MiniMax en'`` →
       ``settings_config.env.ANTHROPIC_AUTH_TOKEN``
    """
    pi_path = _PATHS["pi_auth"]
    if os.path.isfile(pi_path):
        try:
            data = _read_pi_auth(pi_path)
            minimax = data.get("minimax")
            if isinstance(minimax, dict):
                key = minimax.get("key")
                if isinstance(key, str) and key:
                    return key
        except MissingCredential:
            # pi auth.json 存在但解析失败，仍可尝试 cc-switch
            pass

    # 备选：cc-switch.db
    return _ccs_query(
        _PATHS["ccs_db"],
        app_type="claude",
        name="MiniMax en",
        field_kind="ANTHROPIC_AUTH_TOKEN",
    )


def _deepseek_key() -> str:
    """DeepSeek 凭据（M-5 优先级）：
    1. hermes ``config.yaml`` → ``custom_providers[name=deepseek].api_key``
       → 正则 ``sk-[A-Za-z0-9_\\-]+`` 截取 sk- 段（实测带前缀直发 401）
    2. 回退 cc-switch.db → ``app_type='hermes'`` 且 ``name='DeepSeek'`` →
       ``settings_config.api_key`` → 同正则
    """
    cfg_path = _PATHS["hermes_config"]
    if os.path.isfile(cfg_path):
        try:
            text = _read_hermes_config(cfg_path)
            block = _extract_deepseek_block(text, "deepseek")
            raw_value = _extract_api_key_value(block)
            m = _DEEPSEEK_KEY_RE.search(raw_value)
            if m:
                return m.group(0)
            # 块结构正确但值里没有 sk- 段 —— 仍可回落到 cc-switch
        except MissingCredential:
            # 解析失败，回落到 cc-switch
            pass

    # 备选：cc-switch.db（hermes app）
    raw = _ccs_query(
        _PATHS["ccs_db"],
        app_type="hermes",
        name="DeepSeek",
        field_kind="api_key",
    )
    m = _DEEPSEEK_KEY_RE.search(raw)
    if not m:
        raise MissingCredential(
            f"cc-switch.db ({_PATHS['ccs_db']!r}) DeepSeek api_key has no "
            f"sk-[A-Za-z0-9_-]+ substring."
        )
    return m.group(0)


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

_SUPPORTED = {"zai", "minimax", "deepseek"}


def get_key(provider: str) -> str:
    """只读读取 ``provider`` 的凭据，返回原始字符串。

    :raises MissingCredential: 凭据不可读 / 字段缺失 / 格式异常。
    """
    if not isinstance(provider, str):
        raise MissingCredential(
            f"provider must be a string, got {type(provider).__name__}"
        )
    p = provider.lower().strip()

    if p == "codex":
        # Codex 凭据由 CodexBar 自行管理；本项目不读取 ~/.codex/auth.json。
        raise MissingCredential(
            "Codex credentials are managed by CodexBar (~/.codex/auth.json); "
            "this project does not provide get_key('codex'). "
            "Use codexbar-cli.exe usage -p codex --json instead."
        )

    if p == "zai":
        return _zai_key()
    if p == "minimax":
        return _minimax_key()
    if p == "deepseek":
        return _deepseek_key()

    raise MissingCredential(
        f"unknown provider {provider!r}. "
        f"Supported: {sorted(_SUPPORTED)} (plus 'codex' which raises)."
    )
