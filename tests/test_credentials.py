"""凭据只读读取测试（ARCHITECTURE.md §6 + ROUTE_REVIEW.md M-5）。

约定：
- 全部用 ``tmp_path`` + fake 文件，**不**触碰真机 ``~/.cc-switch/``、
  ``~/.pi/agent/auth.json``、``~/AppData/Local/hermes/config.yaml``。
- 通过 ``monkeypatch.setitem`` 改 :data:`aegis.credentials._PATHS`，让
  ``get_key`` 只看 fake 文件。
- ``fake_ccs.db`` 在测试现场用 :mod:`sqlite3` 搭，不提交二进制。
- NO_SECRET_LEAK：捕获 stdout/stderr/异常消息/``to_dict()`` 输出，假 key
  子串一律不得出现。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

import aegis.credentials as creds
from aegis.credentials import MissingCredential, get_key


# ---------------------------------------------------------------------------
# 假凭据（与 fixtures 同步：fake_pi_auth.json / fake_hermes_config.yaml）
# ---------------------------------------------------------------------------

FAKE_ZAI_KEY = "sk-zai-TEST-zzzzzzzz"   # cc-switch.db: claude / Zhipu GLM en
FAKE_MINIMAX_KEY = "sk-cp-TEST-xxxxxxxx"  # pi auth.json (fixtures 一致)
FAKE_DEEPSEEK_KEY = "sk-test-yyyyyyyy"   # hermes config.yaml 正则提取后
FAKE_DEEPSEEK_FALLBACK_KEY = "sk-fallback-wwwwwwww"  # cc-switch.db: hermes / DeepSeek

# 显式「带前缀」原文（与 fake_hermes_config.yaml 一致）
DEEPSEEK_RAW_VALUE_IN_HERMES = "Bearer prefix junk sk-test-yyyyyyyy trailing"

# 用于 NO_SECRET_LEAK 断言的子串 —— 任何出现在输出里的迹象都算泄漏
SECRET_NEVER_LEAK_SUBSTRINGS = [
    "sk-zai-TEST-zzzzzzzz",
    "sk-cp-TEST-xxxxxxxx",
    "sk-test-yyyyyyyy",
    "sk-fallback-wwwwwwww",
    DEEPSEEK_RAW_VALUE_IN_HERMES,
]


# ---------------------------------------------------------------------------
# 工具：在临时目录搭一个最小可用的 cc-switch.db
# ---------------------------------------------------------------------------

def _make_ccs_db(path: Path, entries: list[tuple[str, str, dict]]) -> None:
    """``entries`` = [(app_type, name, settings_config_obj), ...]"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "  id TEXT, app_type TEXT NOT NULL, name TEXT NOT NULL,"
            "  settings_config TEXT NOT NULL,"
            "  PRIMARY KEY (id, app_type)"
            ")"
        )
        for idx, (app_type, name, obj) in enumerate(entries):
            conn.execute(
                "INSERT INTO providers (id, app_type, name, settings_config) "
                "VALUES (?, ?, ?, ?)",
                (f"id-{idx}", app_type, name, json.dumps(obj)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch):
    """搭建三个 fake 凭据源，并把 :data:`creds._PATHS` 重定向到它们。"""
    ccs_db = tmp_path / "ccs.db"
    pi_auth = tmp_path / "pi_auth.json"
    hermes_cfg = tmp_path / "hermes_config.yaml"

    # 1) cc-switch.db：放 zai（claude）、MiniMax fallback（claude）、DeepSeek fallback（hermes）
    _make_ccs_db(
        ccs_db,
        [
            ("claude", "Zhipu GLM en", {"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_ZAI_KEY}}),
            ("claude", "MiniMax en", {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-mm-fallback-yyyyyy"}}),
            ("hermes", "DeepSeek", {"api_key": FAKE_DEEPSEEK_FALLBACK_KEY}),
            # 故意放一个无关条目，确认选择器不被影响
            ("claude", "Other", {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-other-aaaaaaa"}}),
        ],
    )

    # 2) pi auth.json：放 MiniMax（首选来源）
    pi_auth.write_text(
        json.dumps({"minimax": {"key": FAKE_MINIMAX_KEY}}),
        encoding="utf-8",
    )

    # 3) hermes config.yaml：放 DeepSeek（首选来源，带前缀）
    hermes_cfg.write_text(
        "custom_providers:\n"
        "  - name: openrouter\n"
        "    api_key: sk-other-aaaaaaaa\n"
        "  - name: deepseek\n"
        '    api_key: "Bearer prefix junk sk-test-yyyyyyyy trailing"\n'
        "    api_mode: chat\n"
        "    model: deepseek-v4-pro\n"
        "  - name: trailing\n"
        "    api_key: sk-trailing-aaaaaaaa\n",
        encoding="utf-8",
    )

    monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs_db))
    monkeypatch.setitem(creds._PATHS, "pi_auth", str(pi_auth))
    monkeypatch.setitem(creds._PATHS, "hermes_config", str(hermes_cfg))

    return {
        "ccs_db": ccs_db,
        "pi_auth": pi_auth,
        "hermes_config": hermes_cfg,
    }


def _assert_no_secret_leak(
    *,
    captured_out: str,
    captured_err: str,
    exception_text: str = "",
    extra_blobs: list[str] | None = None,
):
    """集中检查：所有指定子串都不在输出/异常/补充 blob 中。"""
    blobs = [captured_out, captured_err, exception_text or ""]
    if extra_blobs:
        blobs.extend(extra_blobs)
    for substr in SECRET_NEVER_LEAK_SUBSTRINGS:
        for i, blob in enumerate(blobs):
            assert substr not in blob, (
                f"Secret substring {substr!r} leaked in blob #{i}: {blob!r}"
            )


# ---------------------------------------------------------------------------
# 三个 provider 的正向提取
# ---------------------------------------------------------------------------

class TestGetKeyHappyPaths:
    def test_zai_from_ccs_db(self, fake_paths):
        key = get_key("zai")
        assert key == FAKE_ZAI_KEY

    def test_minimax_from_pi_auth_preferred(self, fake_paths):
        # pi auth.json 是首选，即使 cc-switch 也有 mm-fallback，也不该用它
        key = get_key("minimax")
        assert key == FAKE_MINIMAX_KEY

    def test_deepseek_from_hermes_config_with_prefix_stripped(self, fake_paths):
        key = get_key("deepseek")
        # hermes 是首选，且只取 sk- 段
        assert key == FAKE_DEEPSEEK_KEY
        # 显式确认没有把前缀带进去
        assert "Bearer" not in key
        assert "prefix" not in key
        assert " " not in key

    def test_provider_name_case_insensitive(self, fake_paths):
        assert get_key("ZAI") == FAKE_ZAI_KEY
        assert get_key(" DeepSeek ") == FAKE_DEEPSEEK_KEY


# ---------------------------------------------------------------------------
# 备选来源（pi auth 缺失 → cc-switch）
# ---------------------------------------------------------------------------

class TestMiniMaxFallback:
    def test_minimax_falls_back_to_ccs_db(self, tmp_path, monkeypatch):
        # pi auth.json 不存在，cc-switch 里有 MiniMax en
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [
                ("claude", "MiniMax en", {"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_MINIMAX_KEY}}),
            ],
        )
        missing_pi = tmp_path / "does_not_exist.json"
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(missing_pi))

        assert get_key("minimax") == FAKE_MINIMAX_KEY

    def test_minimax_falls_back_when_pi_auth_has_no_minimax(self, tmp_path, monkeypatch):
        # pi auth.json 存在但不含 minimax.key
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [("claude", "MiniMax en", {"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_MINIMAX_KEY}})],
        )
        pi = tmp_path / "pi.json"
        pi.write_text(json.dumps({"other": {"key": "sk-other-zzzz"}}), encoding="utf-8")
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(pi))

        assert get_key("minimax") == FAKE_MINIMAX_KEY

    def test_minimax_falls_back_when_pi_auth_value_empty(self, tmp_path, monkeypatch):
        # pi auth.json 含 minimax.key 但值为空字符串
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [("claude", "MiniMax en", {"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_MINIMAX_KEY}})],
        )
        pi = tmp_path / "pi.json"
        pi.write_text(json.dumps({"minimax": {"key": ""}}), encoding="utf-8")
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(pi))

        assert get_key("minimax") == FAKE_MINIMAX_KEY


# ---------------------------------------------------------------------------
# 备选来源（hermes 缺失 → cc-switch）
# ---------------------------------------------------------------------------

class TestDeepSeekFallback:
    def test_deepseek_falls_back_to_ccs_db_when_no_hermes(self, tmp_path, monkeypatch):
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [("hermes", "DeepSeek", {"api_key": FAKE_DEEPSEEK_FALLBACK_KEY})],
        )
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "hermes_config", str(tmp_path / "no_such.yaml"))

        assert get_key("deepseek") == FAKE_DEEPSEEK_FALLBACK_KEY

    def test_deepseek_falls_back_when_hermes_no_deepseek_block(self, tmp_path, monkeypatch):
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [("hermes", "DeepSeek", {"api_key": FAKE_DEEPSEEK_FALLBACK_KEY})],
        )
        cfg = tmp_path / "hermes.yaml"
        # custom_providers 不含 deepseek
        cfg.write_text(
            "custom_providers:\n"
            "  - name: openrouter\n"
            "    api_key: sk-other-aaaaaaaa\n",
            encoding="utf-8",
        )
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "hermes_config", str(cfg))

        assert get_key("deepseek") == FAKE_DEEPSEEK_FALLBACK_KEY

    def test_deepseek_falls_back_when_hermes_value_lacks_sk_segment(self, tmp_path, monkeypatch):
        # hermes 块存在但值里没有「合法的 sk- 段」→ 应回落到 cc-switch。
        # 用「纯文本不包含 sk-」作为更明确的失败信号（避免误匹配偶然子串）。
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [("hermes", "DeepSeek", {"api_key": FAKE_DEEPSEEK_FALLBACK_KEY})],
        )
        cfg = tmp_path / "hermes.yaml"
        cfg.write_text(
            "custom_providers:\n"
            "  - name: deepseek\n"
            '    api_key: "no-sk-prefix-here"\n'
            "    api_mode: chat\n",
            encoding="utf-8",
        )
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "hermes_config", str(cfg))

        assert get_key("deepseek") == FAKE_DEEPSEEK_FALLBACK_KEY


# ---------------------------------------------------------------------------
# Codex 显式不提供
# ---------------------------------------------------------------------------

class TestCodex:
    def test_codex_raises_with_directional_message(self, fake_paths, capsys):
        with pytest.raises(MissingCredential) as excinfo:
            get_key("codex")
        msg = str(excinfo.value)
        # 消息必须指路、不含凭据
        assert "CodexBar" in msg or "codex" in msg.lower()
        for s in SECRET_NEVER_LEAK_SUBSTRINGS:
            assert s not in msg
        # 没有 stdout/stderr 泄漏
        cap = capsys.readouterr()
        _assert_no_secret_leak(
            captured_out=cap.out,
            captured_err=cap.err,
            exception_text=msg,
        )


# ---------------------------------------------------------------------------
# 错误路径：每条消息都必须指路 + 不含凭据
# ---------------------------------------------------------------------------

class TestMissingCredentialPaths:
    def test_zai_missing_db(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(tmp_path / "absent.db"))
        try:
            get_key("zai")
        except MissingCredential as e:
            msg = str(e)
        else:
            pytest.fail("expected MissingCredential")
        # 必须指路
        assert "cc-switch.db" in msg
        # 不含凭据
        for s in SECRET_NEVER_LEAK_SUBSTRINGS:
            assert s not in msg
        cap = capsys.readouterr()
        _assert_no_secret_leak(
            captured_out=cap.out, captured_err=cap.err, exception_text=msg,
        )

    def test_zai_provider_not_in_db(self, tmp_path, monkeypatch, capsys):
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(ccs, [("claude", "DifferentName", {"env": {"ANTHROPIC_AUTH_TOKEN": "x"}})])
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        try:
            get_key("zai")
        except MissingCredential as e:
            msg = str(e)
        else:
            pytest.fail("expected MissingCredential")
        assert "Zhipu GLM en" in msg  # 指路：哪个 name 没找到
        for s in SECRET_NEVER_LEAK_SUBSTRINGS:
            assert s not in msg

    def test_zai_field_missing(self, tmp_path, monkeypatch, capsys):
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(ccs, [("claude", "Zhipu GLM en", {"env": {"ANTHROPIC_BASE_URL": "x"}})])
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        try:
            get_key("zai")
        except MissingCredential as e:
            msg = str(e)
        else:
            pytest.fail("expected MissingCredential")
        assert "ANTHROPIC_AUTH_TOKEN" in msg  # 指路：哪个字段缺失

    def test_zai_settings_config_malformed_json(self, tmp_path, monkeypatch):
        ccs = tmp_path / "ccs.db"
        conn = sqlite3.connect(str(ccs))
        conn.execute(
            "CREATE TABLE providers ("
            "  id TEXT, app_type TEXT NOT NULL, name TEXT NOT NULL,"
            "  settings_config TEXT NOT NULL, PRIMARY KEY (id, app_type))"
        )
        conn.execute(
            "INSERT INTO providers VALUES ('i', 'claude', 'Zhipu GLM en', 'this is not json')"
        )
        conn.commit()
        conn.close()
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        with pytest.raises(MissingCredential):
            get_key("zai")

    def test_minimax_both_sources_missing(self, tmp_path, monkeypatch, capsys):
        # 实际行为：pi_auth 不存在 → 直接跳过 → 走 cc-switch 报缺库。
        # 因此错误消息提及的是 cc-switch.db（这是「第二道防线」）。
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(tmp_path / "no_pi.json"))
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(tmp_path / "no_ccs.db"))
        try:
            get_key("minimax")
        except MissingCredential as e:
            msg = str(e)
        else:
            pytest.fail("expected MissingCredential")
        # 必须指路：哪条路径失败
        assert "cc-switch.db" in msg
        for s in SECRET_NEVER_LEAK_SUBSTRINGS:
            assert s not in msg
        cap = capsys.readouterr()
        _assert_no_secret_leak(
            captured_out=cap.out, captured_err=cap.err, exception_text=msg,
        )

    def test_minimax_pi_auth_malformed_json(self, tmp_path, monkeypatch):
        # pi 存在但 JSON 损坏 → 静默回退到 cc-switch；只要 cc-switch 里有
        # 合法值就拿到 key；只有 cc-switch 也拿不到时才报错。
        pi = tmp_path / "pi.json"
        pi.write_text("{not json", encoding="utf-8")
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(
            ccs,
            [("claude", "MiniMax en", {"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_MINIMAX_KEY}})],
        )
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(pi))
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        # JSON 损坏的 pi 不应阻塞整体流程
        assert get_key("minimax") == FAKE_MINIMAX_KEY

    def test_minimax_pi_auth_malformed_json_no_fallback(self, tmp_path, monkeypatch):
        # pi JSON 损坏 + cc-switch 也缺库 → MissingCredential 且消息指路
        pi = tmp_path / "pi.json"
        pi.write_text("{not json", encoding="utf-8")
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(pi))
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(tmp_path / "no_ccs.db"))
        with pytest.raises(MissingCredential):
            get_key("minimax")

    def test_deepseek_no_block_no_db(self, tmp_path, monkeypatch):
        # hermes 不存在 → 直接跳过 → 走 cc-switch 报缺库。
        # 因此错误消息提及 cc-switch.db（第二道防线）。
        monkeypatch.setitem(creds._PATHS, "hermes_config", str(tmp_path / "n.yaml"))
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(tmp_path / "n.db"))
        with pytest.raises(MissingCredential) as e:
            get_key("deepseek")
        # 错误消息必须指路
        assert "cc-switch.db" in str(e.value)

    def test_deepseek_ccs_api_key_no_sk_segment(self, tmp_path, monkeypatch):
        # cc-switch.api_key 含「偶然出现的 -sk- 子串」但不是合法 key
        # → 正则的负向回顾应拒绝匹配 → 报错
        ccs = tmp_path / "ccs.db"
        _make_ccs_db(ccs, [("hermes", "DeepSeek", {"api_key": "no-sk-segment-here"})])
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(ccs))
        monkeypatch.setitem(creds._PATHS, "hermes_config", str(tmp_path / "n.yaml"))
        with pytest.raises(MissingCredential) as e:
            get_key("deepseek")
        msg = str(e.value)
        assert "sk-" in msg  # 指路：正则没匹配到

    def test_unknown_provider(self):
        with pytest.raises(MissingCredential) as e:
            get_key("not-a-provider")
        assert "unknown" in str(e.value).lower()

    def test_non_string_provider(self):
        with pytest.raises(MissingCredential):
            get_key(123)


# ---------------------------------------------------------------------------
# NO_SECRET_LEAK：全流程端到端断言
# ---------------------------------------------------------------------------

class TestNoSecretLeak:
    def test_full_flow_no_leak(self, fake_paths, capsys):
        """捕获成功 + 失败两条路径下的所有输出 / 异常。"""
        cap = capsys.readouterr()  # 清空

        # 成功路径：三个 provider 都应返回 key，但 key 不能进入 stdout/stderr
        zai = get_key("zai")
        mm = get_key("minimax")
        ds = get_key("deepseek")
        assert zai == FAKE_ZAI_KEY
        assert mm == FAKE_MINIMAX_KEY
        assert ds == FAKE_DEEPSEEK_KEY

        cap_after_success = capsys.readouterr()

        # 触发错误路径：所有源都指向不存在
        for k in ("ccs_db", "pi_auth", "hermes_config"):
            creds._PATHS[k] = str(fake_paths["ccs_db"].parent / f"absent_{k}")

        error_blobs: list[str] = []
        for prov in ("zai", "minimax", "deepseek"):
            try:
                get_key(prov)
            except MissingCredential as e:
                error_blobs.append(str(e))

        cap_after_errors = capsys.readouterr()

        # 函数返回值本身在测试断言里使用 —— 它们就是 fake key，不能放进
        # extra_blobs（自己跟自己比必然命中）。所以这里只断言所有「外部输出」
        # 不泄漏凭据。
        _assert_no_secret_leak(
            captured_out=cap_after_success.out + cap_after_errors.out,
            captured_err=cap_after_success.err + cap_after_errors.err,
            exception_text="\n".join(error_blobs),
        )

    def test_exception_message_lists_paths_only(self, tmp_path, monkeypatch, capsys):
        """错误消息只能提及路径 / 字段 / 原因，不能出现凭据子串。"""
        monkeypatch.setitem(creds._PATHS, "ccs_db", str(tmp_path / "no.db"))
        monkeypatch.setitem(creds._PATHS, "pi_auth", str(tmp_path / "no.json"))
        monkeypatch.setitem(creds._PATHS, "hermes_config", str(tmp_path / "no.yaml"))

        for prov in ("zai", "minimax", "deepseek"):
            try:
                get_key(prov)
            except MissingCredential as e:
                msg = str(e)
                # 消息必须提及路径或字段名（指路）
                assert any(
                    token in msg
                    for token in (".db", ".json", ".yaml", "ANTHROPIC_AUTH_TOKEN",
                                  "deepseek", "api_key", "custom_providers")
                ), f"non-directional message for {prov}: {msg}"
                # 不含假 key 子串
                for s in SECRET_NEVER_LEAK_SUBSTRINGS:
                    assert s not in msg

        cap = capsys.readouterr()
        _assert_no_secret_leak(captured_out=cap.out, captured_err=cap.err)
