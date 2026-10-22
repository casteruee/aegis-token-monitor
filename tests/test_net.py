"""``aegis.net`` 离线单元测试（ARCHITECTURE §13 + ROUTE_REVIEW M-3 + NO_SECRET_LEAK）。

不需要真实网络，全部 monkeypatch ``aegis.net.urlopen``。

覆盖：
- HttpError / NetworkError / ParseError 三类异常分类；
- M-3：429 / 5xx 直接抛 HttpError，不重试 / 不退避；
- Authorization: Bearer 头正确发送，key 绝**不进** URL；
- 响应体里的 ``sk-...`` / ``Bearer ...`` 字段在异常消息中都被替换为
  ``<redacted>``（NO_SECRET_LEAK）。
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from aegis import net
from aegis.net import (
    HttpError,
    NetworkError,
    ParseError,
    _host,
    _preview,
    _read_body,
    _redact,
    http_get_json,
)


# ---------------- 全局常量 ----------------

FAKE_KEY = "sk-test-aaaaaaaaaaaaaaaaa"
LEAK_NEVER = [FAKE_KEY]


# ---------------- 工具：构造可读的 HTTPError ----------------

class _FakeResp:
    """模仿 ``http.client.HTTPResponse``：``read()`` 返回字节。"""
    def __init__(self, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._buf = io.BytesIO(body)
    def read(self):
        return self._buf.read()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def _make_http_error(status, body=b""):
    """构造一个能 ``.read()`` 出 body 的 :class:`urllib.error.HTTPError`。"""
    if isinstance(body, str):
        body = body.encode("utf-8")
    from urllib.error import HTTPError as URLLibHTTPError
    return URLLibHTTPError(
        "https://api.example/test", status, "Err",
        {"Content-Type": "application/json"},
        io.BytesIO(body),
    )


# ===========================================================================
# _redact / _preview
# ===========================================================================

class TestRedact:
    def test_sk_redacted(self):
        assert _redact("token=sk-abcdef1234 leaked") == "token=<redacted> leaked"

    def test_sk_cp_redacted(self):
        # mini 风格的 sk-cp-...
        assert _redact("Bearer sk-cp-TEST-xxxxxxxx") == "Bearer <redacted>"

    def test_bearer_redacted(self):
        assert _redact("Authorization: Bearer some-secret") == "Authorization: Bearer <redacted>"

    def test_bearer_case_insensitive(self):
        assert _redact("bearer sk-other") == "Bearer <redacted>"

    def test_safe_text_unchanged(self):
        s = '{"code": 200, "msg": "ok"}'
        assert _redact(s) == s

    def test_empty(self):
        assert _redact("") == ""

    def test_none(self):
        assert _redact(None) == ""


class TestPreview:
    def test_short_body_no_truncation(self):
        assert _preview("hi") == "hi"

    def test_long_body_truncates_with_ellipsis(self):
        long = "x" * 500
        out = _preview(long, n=200)
        assert len(out) == 203
        assert out.endswith("...")

    def test_redacts_then_truncates(self):
        body = "sk-test " * 200  # 1600 字符
        out = _preview(body)
        assert "sk-test" not in out
        assert out.count("<redacted>") >= 1

    def test_none_handled(self):
        assert _preview(None) == ""

    def test_non_string(self):
        assert _preview(123) == "123"


class TestHost:
    def test_extracts_host_only(self):
        # 关键：路径 / query / fragment 都不能出现
        h = _host("https://api.z.ai/api/monitor/usage/quota/limit")
        assert h == "api.z.ai"

    def test_with_port(self):
        h = _host("https://api.example.com:8443/foo")
        assert h == "api.example.com:8443"

    def test_empty(self):
        assert _host("") == ""
        assert _host("not-a-url") == ""  # 不抛

    def test_no_path_means_no_secret_leak(self):
        # 与 key 无关 —— 但确认 _host 不返回路径
        full = "https://api.example.com/some/path?query=abc"
        h = _host(full)
        assert "/some/path" not in h
        assert "query" not in h


class TestReadBody:
    def test_bytes_utf8(self):
        r = _FakeResp("中文 + 日本語")
        assert _read_body(r) == "中文 + 日本語"

    def test_bytes_latin1_fallback(self):
        r = _FakeResp(b"\xff\xfe plain ascii")
        out = _read_body(r)
        assert "plain ascii" in out  # utf-8 fail 时回落到 latin-1

    def test_none_returns_empty(self):
        class R:
            def read(self):
                return None
        assert _read_body(R()) == ""


# ===========================================================================
# Happy path
# ===========================================================================

class TestHappy:
    def test_200_returns_dict(self):
        def fake_open(req, timeout=None):
            assert timeout == 10
            return _FakeResp(json.dumps({"code": 200, "data": {"x": 1}}))
        with patch.object(net, "urlopen", side_effect=fake_open):
            out = http_get_json("https://api.example/q", FAKE_KEY)
        assert out == {"code": 200, "data": {"x": 1}}

    def test_empty_object(self):
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: _FakeResp("{}")):
            assert http_get_json("https://api.example/q", FAKE_KEY) == {}

    def test_custom_timeout(self):
        seen = []
        def fake_open(req, timeout=None):
            seen.append(timeout)
            return _FakeResp("{}")
        with patch.object(net, "urlopen", side_effect=fake_open):
            http_get_json("https://api.example/q", FAKE_KEY, timeout=3.5)
        assert seen == [3.5]


# ===========================================================================
# 头部 / 无 key 泄漏到 URL
# ===========================================================================

class TestAuthorizationHeader:
    def test_bearer_header_set(self):
        captured = {}
        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["authorization"] = req.get_header("Authorization")
            captured["accept"] = req.get_header("Accept")
            return _FakeResp("{}")
        with patch.object(net, "urlopen", side_effect=fake_open):
            http_get_json("https://api.example/q", FAKE_KEY)
        assert captured["authorization"] == f"Bearer {FAKE_KEY}"
        assert "application/json" in (captured["accept"] or "")

    def test_key_never_appears_in_url(self):
        captured = {}
        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResp("{}")
        with patch.object(net, "urlopen", side_effect=fake_open):
            http_get_json("https://api.example/q", FAKE_KEY)
        # key 不得以任何方式出现在 URL 里（即便有人手动拼 query 也会被捕获）
        assert FAKE_KEY not in captured["url"]


# ===========================================================================
# 非 200 → HttpError
# ===========================================================================

class TestHttpStatusErrors:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
    def test_non_200_raises_http_error(self, status):
        """M-3：429 / 5xx 也走 HttpError，不重试、不退避。"""
        calls = []
        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            raise _make_http_error(status, f'{{"err":"{status}"}}')
        with patch.object(net, "urlopen", side_effect=fake_open):
            with pytest.raises(HttpError) as ex:
                http_get_json("https://api.example/q", FAKE_KEY)
        assert ex.value.status == status
        assert len(calls) == 1, f"M-3 violation: status={status} was retried"

    def test_status_propagated_in_message(self):
        """消息里只允许 host + status + 异常类名 + 脱敏 body。"""
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              _make_http_error(401, "boom"))):
            with pytest.raises(HttpError) as ex:
                http_get_json("https://api.example/q", FAKE_KEY)
        msg = str(ex.value)
        assert "api.example" in msg
        assert "401" in msg
        assert "HTTPError" in msg

    def test_401_attempted_only_once(self):
        # M-3：明确只调一次 urlopen（不重试）
        calls = []
        def fake_open(req, timeout=None):
            calls.append(1)
            raise _make_http_error(401)
        with patch.object(net, "urlopen", side_effect=fake_open):
            with pytest.raises(HttpError):
                http_get_json("https://api.example/q", FAKE_KEY)
        assert len(calls) == 1


# ===========================================================================
# NetworkError
# ===========================================================================

class TestNetworkErrors:
    def test_url_error(self):
        from urllib.error import URLError
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              URLError("name resolution failed"))):
            with pytest.raises(NetworkError):
                http_get_json("https://api.example/q", FAKE_KEY)

    def test_timeout_error(self):
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              TimeoutError("timed out"))):
            with pytest.raises(NetworkError):
                http_get_json("https://api.example/q", FAKE_KEY)

    def test_connection_refused_oserror(self):
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              OSError("connection refused"))):
            with pytest.raises(NetworkError):
                http_get_json("https://api.example/q", FAKE_KEY)

    def test_network_error_message_has_host_and_class(self):
        from urllib.error import URLError
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              URLError("dns down"))):
            try:
                http_get_json("https://api.z.ai/q", FAKE_KEY)
            except NetworkError as e:
                msg = str(e)
                assert "api.z.ai" in msg
                assert "URLError" in msg


# ===========================================================================
# ParseError
# ===========================================================================

class TestParseErrors:
    @pytest.mark.parametrize("body", [
        "not-a-json",
        "{unterminated",
        "",
        "[1,2,3]",   # 不是 object
        "null",
        "123",
        '"str"',
        "true",
    ])
    def test_non_object_or_bad_json_raises_parse_error(self, body):
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: _FakeResp(body)):
            with pytest.raises(ParseError):
                http_get_json("https://api.example/q", FAKE_KEY)

    def test_parse_error_has_host_and_class(self):
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: _FakeResp("not-json")):
            try:
                http_get_json("https://api.example/q", FAKE_KEY)
            except ParseError as e:
                msg = str(e)
                assert "api.example" in msg
                assert "JSONDecodeError" in msg or "NonObjectJson" in msg


# ===========================================================================
# NO_SECRET_LEAK
# ===========================================================================

class TestNoSecretLeak:
    """异常文本中不得出现 fake key（任何形式的 key 子串）。"""

    def _assert_clean(self, message: str):
        for s in LEAK_NEVER:
            assert s not in message, f"key leaked: {s!r} in {message!r}"

    def test_http_error_body_redacted(self):
        leak_body = "auth failed: sk-test-aaaaaaaaaaaaaaaaa must rotate"
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              _make_http_error(401, leak_body))):
            with pytest.raises(HttpError) as ex:
                http_get_json("https://api.example/q", FAKE_KEY)
            self._assert_clean(str(ex.value))
            assert "body=" in str(ex.value)

    def test_parse_error_body_redacted(self):
        # 用「故意畸形 + 含 sk-」的 body，确保 (a) 触发 ParseError (b) 校验脱敏
        leak_body = "not-json-leading sk-test-aaaaaaaaaaaaaaaaa tail"
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: _FakeResp(leak_body)):
            with pytest.raises(ParseError) as ex:
                http_get_json("https://api.example/q", FAKE_KEY)
            self._assert_clean(str(ex.value))

    def test_network_error_no_key(self):
        # 即使 URLError 的 reason 里塞了 key，也要被外层重写（防止本端意外泄露）
        from urllib.error import URLError
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              URLError("could not resolve host sk-test-aaaaaaaaaaaaaaaaa"))):
            try:
                http_get_json("https://api.example/q", FAKE_KEY)
            except NetworkError as e:
                self._assert_clean(str(e))

    def test_authorization_header_value_not_in_exception(self):
        """即使 body 模拟异常触发，构造的 Authorization 也不会被泄漏到异常文本里。

        直接构造：body 里的 ``Authorization: Bearer sk-...`` 该被 redact。
        """
        leak = "Request: GET / Authorization: Bearer sk-test-aaaaaaaaaaaaaaaaa HTTP/1.1"
        with patch.object(net, "urlopen",
                          lambda req, timeout=None: (_ for _ in ()).throw(
                              _make_http_error(403, leak))):
            with pytest.raises(HttpError) as ex:
                http_get_json("https://api.example/q", FAKE_KEY)
            self._assert_clean(str(ex.value))
            # 应被 ``Bearer <redacted>`` 替代
            assert "Bearer <redacted>" in str(ex.value)


# ===========================================================================
# 输入合法性
# ===========================================================================

class TestInputValidation:
    def test_empty_url_raises_network_error(self):
        with pytest.raises(NetworkError):
            http_get_json("", FAKE_KEY)

    def test_empty_key_raises_network_error(self):
        with pytest.raises(NetworkError):
            http_get_json("https://api.example/q", "")

    def test_non_string_url(self):
        with pytest.raises(NetworkError):
            http_get_json(None, FAKE_KEY)

    def test_non_string_key(self):
        with pytest.raises(NetworkError):
            http_get_json("https://api.example/q", None)
