"""Tests for botainer/broker/ — the host-side credential broker.

No real network, no real credential: the upstream is a local fake HTTP
server, the token endpoint is an injected in-process callable, and every
credential is a fabricated temp file.

Covers:
- header strip + inject (client sentinel/x-api-key/cookie stripped; the real
  Bearer injected host-side; anthropic-beta/-version pass through)
- SSRF guarantees: pinned https upstream, no redirect following, path
  allowlist, absolute-URL refusal, body cap + framing hardening
- refresh-on-expiry + refresh-token rotation + write-back to the botainer
  credential file (0600, atomic, other fields preserved)
- re-read-per-call picks up an externally-refreshed token
- fail-closed when no valid token and no refresh configured
- credential path conventions match the plugin pre_session hooks
- the daemon_main entry point end-to-end over a real unix socket
"""

from __future__ import annotations

import json
import os
import socket as _socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from botainer.broker.credential_source import (
    BotainerCredentialBroker,
    resolve_credential_path,
)
from botainer.broker.daemon import (
    MAX_BODY_BYTES,
    BrokerError,
    UnixHTTPConnection,
    handle_request,
    serve_tcp,
    serve_unix,
)
from botainer.broker.oauth_refresh import OAuthRefreshError, RefreshingOAuthKeystore
from botainer.core.broker_sentinel import is_sentinel, make_sentinel
from botainer.core.refusal import Refused, RefusalCategory

REPO_ROOT = Path(__file__).resolve().parents[2]

# Fabricated, shape-conformant tokens (>=64 chars past the prefix, matching
# the sk-ant-oat*/sk-ant-ort* validation in the shared pre_session hook).
FAKE_ACCESS = "sk-ant-oat01-" + "a" * 80
FAKE_ACCESS_2 = "sk-ant-oat01-" + "b" * 80
FAKE_REFRESH = "sk-ant-ort01-" + "r" * 80
FAKE_REFRESH_2 = "sk-ant-ort01-" + "s" * 80


def _now_ms() -> int:
    return int(time.time() * 1000)


def write_creds(
    path: Path,
    *,
    access: str = FAKE_ACCESS,
    refresh: str | None = FAKE_REFRESH,
    expires_at_ms: int | None = None,
    mode: int = 0o600,
) -> None:
    oauth: dict = {
        "accessToken": access,
        "expiresAt": _now_ms() + 3_600_000 if expires_at_ms is None else expires_at_ms,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
    if refresh is not None:
        oauth["refreshToken"] = refresh
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    os.chmod(path, mode)


# ────────── fake upstream (records everything it receives) ──────────


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    requests_log: list[dict] = []  # reset per fixture
    redirect_to: str | None = None  # when set, reply 302 → this URL

    def log_message(self, format: str, *args: object) -> None:
        return

    def _record_and_reply(self) -> None:
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).requests_log.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        if type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        reply = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = _record_and_reply
    do_POST = _record_and_reply


@pytest.fixture
def fake_upstream():
    _FakeUpstreamHandler.requests_log = []
    _FakeUpstreamHandler.redirect_to = None
    server = HTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}",
            _FakeUpstreamHandler.requests_log,
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def short_tmp():
    """A SHORT temp dir for AF_UNIX socket paths (108-byte sun_path limit)."""
    d = tempfile.mkdtemp(prefix="bkr", dir="/tmp")
    yield Path(d)
    import shutil

    shutil.rmtree(d, ignore_errors=True)


def _serve(keystore, upstream: str, sock: Path):
    server = serve_unix(str(sock), keystore, upstream, allow_insecure_upstream=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _serve_tcp_helper(keystore, upstream: str, token: str):
    server = serve_tcp("127.0.0.1", 0, keystore, upstream,
                       required_client_token=token, allow_insecure_upstream=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _tcp_post(port: int, path: str, headers: dict, body: bytes = b""):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


# ────────── TCP transport (Docker Desktop) + sentinel access gate ──────────


def test_tcp_transport_requires_the_sentinel_token(fake_upstream, tmp_path: Path) -> None:
    """The loopback TCP port has no uid boundary, so every request MUST present
    the sentinel as its bearer. Without it → 403 and the upstream is untouched;
    with it → forwarded with the real injected token."""
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    token = make_sentinel("t-tcp", "deadbeef")
    server, port = _serve_tcp_helper(BotainerCredentialBroker(creds), upstream_url, token)
    try:
        # No token → refused, nothing forwarded.
        status, _ = _tcp_post(port, "/v1/messages", {"Content-Type": "application/json"})
        assert status == 403
        assert log == []
        # Wrong token → refused.
        status, _ = _tcp_post(port, "/v1/messages",
                              {"Authorization": "Bearer nope"})
        assert status == 403
        assert log == []
        # Correct sentinel → forwarded with the REAL injected Bearer.
        status, _ = _tcp_post(port, "/v1/messages",
                              {"Authorization": f"Bearer {token}"})
        assert status == 200
        assert len(log) == 1
        # real token injected host-side; sentinel never forwarded
        assert log[0]["headers"]["authorization"] == f"Bearer {FAKE_ACCESS}"
        assert log[0]["headers"].get("anthropic-beta") == "oauth-2025-04-20"
        assert token not in json.dumps(log[0])
    finally:
        server.shutdown()
        server.server_close()


# ────────── credential path conventions (mirror the plugin hooks) ──────────


def test_shared_path_matches_agent_claude_shared_hook(tmp_path: Path) -> None:
    # plugins/agent-claude-shared/hooks/pre_session.py:
    #   shared_dir = state_root / "shared-auth" / "agent-claude"
    #   shared_file = shared_dir / ".credentials.json"
    p = resolve_credential_path(state_root=tmp_path, mode="shared")
    assert p == tmp_path / "shared-auth" / "agent-claude" / ".credentials.json"


def test_isolated_path_matches_agent_claude_hook(tmp_path: Path) -> None:
    # plugins/agent-claude/hooks/pre_session.py:
    #   creds_dir = state_root / "state" / uid / "data" / "agent-claude"
    #               / "profiles" / profile
    p = resolve_credential_path(
        state_root=tmp_path, mode="isolated", project_uuid="u-123", profile="work"
    )
    assert p == (
        tmp_path / "state" / "u-123" / "data" / "agent-claude"
        / "profiles" / "work" / ".credentials.json"
    )


def test_isolated_path_requires_uuid(tmp_path: Path) -> None:
    with pytest.raises(Refused) as exc:
        resolve_credential_path(state_root=tmp_path, mode="isolated")
    assert exc.value.category == RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE


def test_unknown_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused):
        resolve_credential_path(state_root=tmp_path, mode="mounted")


# ────────── BotainerCredentialBroker (file-backed keystore) ──────────


def test_outbound_is_bearer_of_file_access_token(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    ks = BotainerCredentialBroker(creds)
    assert ks.outbound_authorization() == f"Bearer {FAKE_ACCESS}"


def test_reread_picks_up_externally_refreshed_token(tmp_path: Path) -> None:
    """A token refreshed by ANOTHER writer (shared-mode session, another
    broker) must be served without any refresh call of our own."""
    creds = tmp_path / ".credentials.json"
    write_creds(creds, access=FAKE_ACCESS)
    ks = BotainerCredentialBroker(creds)
    assert ks.outbound_authorization() == f"Bearer {FAKE_ACCESS}"
    write_creds(creds, access=FAKE_ACCESS_2)  # external refresh
    assert ks.outbound_authorization() == f"Bearer {FAKE_ACCESS_2}"


def test_real_secret_value_is_refresh_token(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    ks = BotainerCredentialBroker(creds)
    assert ks.real_secret_value() == FAKE_REFRESH


def test_fail_closed_when_file_missing(tmp_path: Path) -> None:
    ks = BotainerCredentialBroker(tmp_path / "nope.json")
    with pytest.raises(Refused) as exc:
        ks.outbound_authorization()
    assert exc.value.category == RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE


def test_fail_closed_expired_and_no_refresh_configured(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds, expires_at_ms=_now_ms() - 10_000)
    ks = BotainerCredentialBroker(creds)  # no token_endpoint/client_id
    with pytest.raises(Refused) as exc:
        ks.outbound_authorization()
    assert exc.value.category == RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE


def test_refuses_world_readable_credential_file(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds, mode=0o644)
    ks = BotainerCredentialBroker(creds)
    with pytest.raises(Refused) as exc:
        ks.outbound_authorization()
    assert exc.value.category == RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE


def test_refuses_sentinel_in_host_store(tmp_path: Path) -> None:
    """A sentinel placeholder is never a servable credential."""
    creds = tmp_path / ".credentials.json"
    write_creds(creds, access=make_sentinel("t1", "abc123"))
    ks = BotainerCredentialBroker(creds)
    with pytest.raises(Refused):
        ks.outbound_authorization()


def test_refresh_on_expiry_rotates_and_writes_back(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds, expires_at_ms=_now_ms() - 10_000)
    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, form: dict) -> tuple[int, dict]:
        calls.append((url, dict(form)))
        return 200, {
            "access_token": FAKE_ACCESS_2,
            "refresh_token": FAKE_REFRESH_2,  # rotation
            "expires_in": 3600,
        }

    ks = BotainerCredentialBroker(
        creds,
        token_endpoint="https://token.example/oauth/token",
        client_id="client-abc",
        http_post=fake_post,
    )
    assert ks.outbound_authorization() == f"Bearer {FAKE_ACCESS_2}"

    # The refresh went to the configured endpoint with the OLD refresh token.
    assert calls == [
        (
            "https://token.example/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": FAKE_REFRESH,
                "client_id": "client-abc",
            },
        )
    ]

    # Rotated tokens written BACK to the botainer file (ms expiry, 0600).
    on_disk = json.loads(creds.read_text())["claudeAiOauth"]
    assert on_disk["accessToken"] == FAKE_ACCESS_2
    assert on_disk["refreshToken"] == FAKE_REFRESH_2
    assert on_disk["expiresAt"] > _now_ms() + 1_000_000  # ~1h out, in ms
    assert (creds.stat().st_mode & 0o777) == 0o600
    # Unrelated fields preserved.
    assert on_disk["scopes"] == ["user:inference"]
    assert on_disk["subscriptionType"] == "max"

    # Next call serves from the (now fresh) file — no second refresh.
    assert ks.outbound_authorization() == f"Bearer {FAKE_ACCESS_2}"
    assert len(calls) == 1
    assert ks.real_secret_value() == FAKE_REFRESH_2


def test_refresh_failure_is_fail_closed(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds, expires_at_ms=_now_ms() - 10_000)

    def failing_post(url: str, form: dict) -> tuple[int, dict]:
        return 400, {"error": "invalid_grant"}

    ks = BotainerCredentialBroker(
        creds,
        token_endpoint="https://token.example/oauth/token",
        client_id="client-abc",
        http_post=failing_post,
    )
    with pytest.raises(Refused) as exc:
        ks.outbound_authorization()
    assert exc.value.category == RefusalCategory.BROKER_REFRESH_FAILED
    # The stale file is untouched (no partial write).
    assert json.loads(creds.read_text())["claudeAiOauth"]["accessToken"] == FAKE_ACCESS


# ────────── RefreshingOAuthKeystore (ported refresh core) ──────────


def test_refreshing_keystore_requires_endpoint_and_client_id() -> None:
    with pytest.raises(ValueError):
        RefreshingOAuthKeystore(FAKE_REFRESH, token_endpoint="", client_id="x")
    with pytest.raises(ValueError):
        RefreshingOAuthKeystore(FAKE_REFRESH, token_endpoint="https://t", client_id="")
    with pytest.raises(ValueError):
        RefreshingOAuthKeystore("", token_endpoint="https://t", client_id="x")


def test_refreshing_keystore_skew_refresh_and_rotation() -> None:
    clock = [1000.0]
    responses = [
        (200, {"access_token": "acc-1", "refresh_token": "ref-2", "expires_in": 300}),
        (200, {"access_token": "acc-2", "expires_in": 300}),
    ]

    def post(url: str, form: dict) -> tuple[int, dict]:
        return responses.pop(0)

    ks = RefreshingOAuthKeystore(
        "ref-1",
        token_endpoint="https://t/token",
        client_id="cid",
        http_post=post,
        skew_seconds=60,
        now=lambda: clock[0],
    )
    assert ks.outbound_authorization() == "Bearer acc-1"
    assert ks.refresh_token == "ref-2"  # rotation adopted
    clock[0] = 1000.0 + 300 - 30  # inside the skew window → refresh again
    assert ks.outbound_authorization() == "Bearer acc-2"
    assert ks.refresh_token == "ref-2"  # no rotation in 2nd response → kept
    assert responses == []


def test_refreshing_keystore_fail_closed_on_bad_response() -> None:
    ks = RefreshingOAuthKeystore(
        "ref-1",
        token_endpoint="https://t/token",
        client_id="cid",
        http_post=lambda u, f: (200, {"unexpected": "shape"}),
    )
    with pytest.raises(OAuthRefreshError):
        ks.outbound_authorization()


# ────────── handle_request (pure-function guarantees) ──────────


class _StaticKeystore:
    def __init__(self, value: str | None = f"Bearer {FAKE_ACCESS}") -> None:
        self._value = value

    def outbound_authorization(self) -> str | None:
        return self._value


def test_handle_request_refuses_non_https_upstream() -> None:
    with pytest.raises(BrokerError) as exc:
        handle_request(
            path="/v1/messages",
            headers={},
            body=b"{}",
            keystore=_StaticKeystore(),
            pinned_upstream="http://127.0.0.1:9",  # http, no insecure flag
        )
    assert exc.value.status == 500


def test_handle_request_refuses_absolute_url_and_off_allowlist() -> None:
    ks = _StaticKeystore()
    with pytest.raises(BrokerError) as exc:
        handle_request(
            path="https://evil.example/v1/messages",
            headers={},
            body=b"",
            keystore=ks,
            pinned_upstream="https://api.anthropic.com",
        )
    assert exc.value.status == 400
    with pytest.raises(BrokerError) as exc:
        handle_request(
            path="//evil.example/v1/messages",  # protocol-relative
            headers={},
            body=b"",
            keystore=ks,
            pinned_upstream="https://api.anthropic.com",
        )
    assert exc.value.status == 400
    with pytest.raises(BrokerError) as exc:
        handle_request(
            path="/admin/keys",
            headers={},
            body=b"",
            keystore=ks,
            pinned_upstream="https://api.anthropic.com",
        )
    assert exc.value.status == 403


def test_handle_request_refuses_odd_methods() -> None:
    with pytest.raises(BrokerError) as exc:
        handle_request(
            path="/v1/messages",
            headers={},
            body=b"",
            keystore=_StaticKeystore(),
            pinned_upstream="https://api.anthropic.com",
            method="TRACE",
        )
    assert exc.value.status == 405


def test_handle_request_refuses_double_encoded_separators() -> None:
    """audit LOW: `%252e`/`%252f` must be rejected too, so a backend
    that decodes twice can't be reached past the codex path fence."""
    for bad in ("/v1/%252e%252e/admin", "/v1/x%252fy"):
        with pytest.raises(BrokerError) as exc:
            handle_request(path=bad, headers={}, body=b"",
                           keystore=_StaticKeystore(),
                           pinned_upstream="https://api.anthropic.com")
        assert exc.value.status == 400


def test_connection_listed_is_case_insensitive() -> None:
    from botainer.broker.daemon import _connection_listed
    listed = _connection_listed({"CONNECTION": "X-Secret, keep-alive"})
    assert "x-secret" in listed and "keep-alive" in listed


def test_read_body_bounded_slowloris_times_out() -> None:
    import time as _t

    from botainer.broker.daemon import _read_body_bounded

    class _Trickle:
        def read1(self, k: int) -> bytes:
            _t.sleep(0.02)
            return b"x"  # one byte per call, forever

    with pytest.raises(TimeoutError):
        _read_body_bounded(_Trickle(), 10_000, deadline=_t.monotonic() + 0.2)

    class _Fast:
        def read1(self, k: int) -> bytes:
            return b"y" * k

    assert _read_body_bounded(_Fast(), 64, deadline=_t.monotonic() + 5) == b"y" * 64


def test_connection_cap_closes_over_limit(monkeypatch) -> None:
    """The cap mixin closes a connection WITHOUT spawning a thread once
    MAX_CONNECTIONS handler threads are live (audit MEDIUM)."""
    import botainer.broker.daemon as D

    class _Server(D._CapMixin):
        def __init__(self) -> None:
            self.closed = 0
            self.spawned = 0
            super().__init__()

        def shutdown_request(self, request):  # noqa: D102
            self.closed += 1

        def process_request_thread(self, request, addr):  # noqa: D102
            self.spawned += 1

    monkeypatch.setattr(D, "MAX_CONNECTIONS", 1)
    s = _Server()
    s._conn_sem.acquire()  # simulate one slot already taken (at the cap)
    s.process_request(object(), ("x", 0))  # over cap → must close, not spawn
    assert s.closed == 1 and s.spawned == 0


# ────────── daemon over a real unix socket ──────────


def test_daemon_strips_client_credentials_and_injects_real(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), upstream_url, sock)
    try:
        sentinel = make_sentinel("proj1", "deadbeef")
        status, _hdrs, body = UnixHTTPConnection(str(sock)).post(
            "/v1/messages",
            headers={
                # everything credential-shaped the client could send:
                "Authorization": f"Bearer {sentinel}",
                "x-api-key": "sk-ant-should-never-arrive",
                "Cookie": "session=steal-me",
                "Proxy-Authorization": "Basic gggg",
                # Connection-listed smuggling attempt:
                "Connection": "X-Smuggled",
                "X-Smuggled": "leak-me",
                # what Claude Code legitimately sends — must pass through:
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            body=b'{"model": "claude"}',
        )
        assert status == 200
        assert json.loads(body) == {"ok": True}
        assert len(log) == 1
        seen = log[0]["headers"]
        # The REAL Bearer was injected host-side…
        assert seen["authorization"] == f"Bearer {FAKE_ACCESS}"
        assert not is_sentinel(seen["authorization"].removeprefix("Bearer ").strip())
        # …and every client credential/hop-by-hop header was stripped.
        assert "x-api-key" not in seen
        assert "cookie" not in seen
        assert "proxy-authorization" not in seen
        assert "x-smuggled" not in seen
        # The sentinel value appears NOWHERE in what the upstream received.
        assert sentinel not in json.dumps(log[0])
        # Claude Code's own OAuth headers passed through untouched.
        assert seen["anthropic-beta"] == "oauth-2025-04-20"
        assert seen["anthropic-version"] == "2023-06-01"
        assert log[0]["body"] == '{"model": "claude"}'
        assert log[0]["method"] == "POST"
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_case_variant_headers_cannot_override_injected(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    """sharp-edges HIGH: a client must not be able to OVERRIDE a
    host-injected header (or strip a forced one) by sending a case-variant
    duplicate — urllib capitalizes both to the same wire name, last-write-wins.
    The daemon lowercases header keys so only the host injection survives."""
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), upstream_url, sock)
    try:
        sentinel = make_sentinel("proj1", "deadbeef")
        status, _h, _b = UnixHTTPConnection(str(sock)).post(
            "/v1/messages",
            headers={
                "authorization": f"Bearer {sentinel}",
                # case-variant duplicates trying to override the injected Bearer:
                "Authorization": "Bearer ATTACKER-OVERRIDE",
                "AUTHORIZATION": "Bearer ATTACKER-OVERRIDE-2",
                "Content-Type": "application/json",
            },
            body=b"{}",
        )
        assert status == 200
        seen = log[0]["headers"]
        # exactly the injected Bearer reached the upstream — no attacker variant
        assert seen["authorization"] == f"Bearer {FAKE_ACCESS}"
        assert "ATTACKER" not in json.dumps(log[0])
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_never_follows_redirects(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    upstream_url, log = fake_upstream

    # A second origin that must NEVER see a request.
    class _EvilHandler(BaseHTTPRequestHandler):
        hits: list[str] = []

        def log_message(self, *a: object) -> None:
            return

        def do_POST(self) -> None:
            type(self).hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    _EvilHandler.hits = []
    evil = HTTPServer(("127.0.0.1", 0), _EvilHandler)
    threading.Thread(target=evil.serve_forever, daemon=True).start()
    _FakeUpstreamHandler.redirect_to = f"http://127.0.0.1:{evil.server_port}/v1/steal"

    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), upstream_url, sock)
    try:
        status, hdrs, _body = UnixHTTPConnection(str(sock)).post(
            "/v1/messages", headers={"Content-Type": "application/json"}, body=b"{}"
        )
        # The 3xx is surfaced UNFOLLOWED; the evil origin never got the token.
        assert status == 302
        assert _EvilHandler.hits == []
        assert len(log) == 1  # only the pinned upstream was contacted
        # Sanitized response: no Location to chase, no auth material echoed.
        assert "location" not in hdrs
        assert "authorization" not in hdrs
        assert FAKE_ACCESS not in json.dumps(dict(hdrs))
    finally:
        server.shutdown()
        server.server_close()
        evil.shutdown()
        evil.server_close()


def test_daemon_path_allowlist_and_no_secret_in_refusal(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), upstream_url, sock)
    try:
        conn = UnixHTTPConnection(str(sock))
        status, _h, body = conn.post("/admin/export", body=b"")
        assert status == 403
        assert FAKE_ACCESS.encode() not in body
        assert FAKE_REFRESH.encode() not in body
        # Protocol-relative "//host/..." form: on Python >= 3.14 the stdlib
        # server itself collapses the leading "//" before our handler runs,
        # so it arrives off-allowlist (403); on older Pythons it reaches
        # handle_request's own "//" check (400). Either way it is REFUSED and
        # never forwarded — that is the invariant. (The strict 400 branch is
        # unit-tested directly in
        # test_handle_request_refuses_absolute_url_and_off_allowlist.)
        resp = _raw_unix_http(
            str(sock),
            b"POST //evil.example/v1/x HTTP/1.1\r\nHost: b\r\n"
            b"Content-Length: 0\r\n\r\n",
        )
        status_line = resp.split(b"\r\n", 1)[0]
        assert b"400" in status_line or b"403" in status_line
        assert log == []  # nothing was forwarded
    finally:
        server.shutdown()
        server.server_close()


def _raw_unix_http(sock_path: str, raw: bytes) -> bytes:
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall(raw)
    s.settimeout(5)
    chunks = []
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                break
    finally:
        s.close()
    return b"".join(chunks)


def test_daemon_rejects_oversize_and_chunked_framing(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), upstream_url, sock)
    try:
        resp = _raw_unix_http(
            str(sock),
            (
                f"POST /v1/messages HTTP/1.1\r\nHost: b\r\n"
                f"Content-Length: {MAX_BODY_BYTES + 1}\r\n\r\n"
            ).encode(),
        )
        assert b"413" in resp.split(b"\r\n", 1)[0]
        resp = _raw_unix_http(
            str(sock),
            b"POST /v1/messages HTTP/1.1\r\nHost: b\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        )
        assert b"400" in resp.split(b"\r\n", 1)[0]
        assert log == []
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_fail_closed_502_when_credential_unavailable(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    """Expired token, no refresh configured → 502, nothing forwarded, and no
    secret material in the refusal the container sees."""
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds, expires_at_ms=_now_ms() - 10_000)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), upstream_url, sock)
    try:
        status, _h, body = UnixHTTPConnection(str(sock)).post("/v1/messages", body=b"{}")
        assert status == 502
        assert log == []
        assert FAKE_ACCESS.encode() not in body
        assert FAKE_REFRESH.encode() not in body
        assert str(creds).encode() not in body  # host paths stay host-side
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_refreshes_mid_serving_and_writes_back(
    fake_upstream, short_tmp: Path, tmp_path: Path
) -> None:
    """End-to-end: expired file token + configured (mock) token endpoint →
    the request goes out with the NEW Bearer and the rotation lands on disk."""
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds, expires_at_ms=_now_ms() - 10_000)

    def fake_post(url: str, form: dict) -> tuple[int, dict]:
        return 200, {
            "access_token": FAKE_ACCESS_2,
            "refresh_token": FAKE_REFRESH_2,
            "expires_in": 3600,
        }

    ks = BotainerCredentialBroker(
        creds,
        token_endpoint="https://token.example/oauth/token",
        client_id="cid",
        http_post=fake_post,
    )
    sock = short_tmp / "s"
    server = _serve(ks, upstream_url, sock)
    try:
        status, _h, _b = UnixHTTPConnection(str(sock)).post("/v1/messages", body=b"{}")
        assert status == 200
        assert log[0]["headers"]["authorization"] == f"Bearer {FAKE_ACCESS_2}"
        on_disk = json.loads(creds.read_text())["claudeAiOauth"]
        assert on_disk["refreshToken"] == FAKE_REFRESH_2
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_socket_is_owner_only(short_tmp: Path, tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    server = _serve(BotainerCredentialBroker(creds), "https://api.anthropic.com", sock)
    try:
        assert (sock.stat().st_mode & 0o777) == 0o600
    finally:
        server.shutdown()
        server.server_close()


# ────────── daemon_main entry point (subprocess, detached-style) ──────────


def test_daemon_main_end_to_end(fake_upstream, short_tmp: Path, tmp_path: Path) -> None:
    upstream_url, log = fake_upstream
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    sock = short_tmp / "s"
    env = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")},
        "BOTAINER_BROKER_SOCKET": str(sock),
        "BOTAINER_BROKER_CREDENTIAL_FILE": str(creds),
        "BOTAINER_BROKER_UPSTREAM": upstream_url,
        "BOTAINER_BROKER_ALLOW_INSECURE": "1",
        "BOTAINER_TESTING": "1",
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "botainer.broker.daemon_main"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )
    try:
        assert proc.stdout is not None
        ready = proc.stdout.readline().decode()
        assert "BOTAINER-BROKER-READY" in ready, (
            ready,
            proc.stderr.read().decode() if proc.poll() is not None else "",
        )
        status, _h, body = UnixHTTPConnection(str(sock)).post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {make_sentinel('t', 'ff')}"},
            body=b"{}",
        )
        assert status == 200
        assert json.loads(body) == {"ok": True}
        assert log[0]["headers"]["authorization"] == f"Bearer {FAKE_ACCESS}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_daemon_main_refuses_to_start_without_valid_credential(
    short_tmp: Path, tmp_path: Path
) -> None:
    """Fail-closed startup probe: expired token + no refresh config → exit 1
    before ever binding the socket."""
    creds = tmp_path / ".credentials.json"
    write_creds(creds, expires_at_ms=_now_ms() - 10_000)
    sock = short_tmp / "s"
    proc = subprocess.run(
        [sys.executable, "-m", "botainer.broker.daemon_main",
         "--socket", str(sock), "--credential-file", str(creds)],
        env={
            **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")},
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode == 1
    assert b"refusing to start" in proc.stderr
    assert not sock.exists()


def test_daemon_main_insecure_upstream_needs_testing_gate(
    short_tmp: Path, tmp_path: Path
) -> None:
    """--allow-insecure-upstream without BOTAINER_TESTING=1 is refused, so a
    stale shell export can't downgrade a real session to plaintext."""
    creds = tmp_path / ".credentials.json"
    write_creds(creds)
    proc = subprocess.run(
        [sys.executable, "-m", "botainer.broker.daemon_main",
         "--socket", str(short_tmp / "s"), "--credential-file", str(creds),
         "--allow-insecure-upstream"],
        env={
            **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")},
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode == 1
    assert b"test-only" in proc.stderr
