"""Tests for the agent-claude-proxy plugin.

The proxy is stdlib-only (no aiohttp), so we can spin it up locally
against a fake upstream HTTP server and verify the full request/response
flow + audit log + auth gating.
"""

from __future__ import annotations

import json
import os
import signal
import socket as _socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

# Manifest + bundling tests are cheap; do them first.


def test_manifest_loads_and_is_first_party() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(Path(__file__).resolve().parents[2] / "plugins"
                       / "agent-claude-proxy")
    assert m.name == "agent-claude-proxy"
    assert m.tier == "first-party"
    assert m.kind == "host_helper"
    assert "agent-claude" in m.depends_on
    whens = sorted(h.when for h in m.hooks)
    assert whens == ["post_session", "pre_session"]


def test_proxy_plugin_bundled_in_setup() -> None:
    from botainer.plugins.builtin import BUILTIN_PLUGIN_NAMES
    assert "agent-claude-proxy" in BUILTIN_PLUGIN_NAMES


def test_proxy_plugin_in_trust_lock() -> None:
    from botainer.plugins import trust
    lock = trust.read_trust_lock(trust.lock_path_for_install())
    assert "agent-claude-proxy" in lock


# ────────── proxy.py runtime tests ──────────


PROXY_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "agent-claude-proxy" / "proxy.py"
)


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    """Records every request it receives; replies with a small JSON body."""

    requests_log: list[dict] = []  # class-level, reset per test

    def log_message(self, format: str, *args: object) -> None:
        return

    def _record_and_reply(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(content_length) if content_length else b""
        self.requests_log.append({
            "method": self.command,
            "path": self.path,
            "x_api_key": self.headers.get("x-api-key"),
            "authorization": self.headers.get("Authorization"),
            "body": body.decode("utf-8", errors="replace"),
        })
        reply = json.dumps({"ok": True, "echo": body.decode(errors="replace")}).encode()
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
    server = HTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    port = server.server_port
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (port, _FakeUpstreamHandler.requests_log)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy_setup(tmp_path: Path, fake_upstream):
    """Spawn the proxy as a subprocess; tear it down after."""
    port, upstream_log = fake_upstream
    socket_path = tmp_path / "proxy.sock"
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps({"api_key": "real-secret-key-abcdef"}))
    # Per CREDENTIAL-PROXY-INVESTIGATION.md finding #1, the proxy now
    # refuses to read credential files broader than mode 0600.
    os.chmod(creds_path, 0o600)
    audit_log = tmp_path / "audit.jsonl"
    ephemeral = "ephem-token-12345"

    env = {
        **os.environ,
        "BOTAINER_PROXY_SOCKET_PATH": str(socket_path),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": ephemeral,
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds_path),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit_log),
        # 127.0.0.1 is on the proxy's upstream allowlist (loopback for testing).
        "BOTAINER_PROXY_UPSTREAM": f"http://127.0.0.1:{port}",
        "BOTAINER_PROXY_MAX_REQUEST_BYTES": "1024",
        "BOTAINER_PROXY_RATE_LIMIT_RPS": "10",
    }
    proc = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Probe-connect (not socket.exists()) — same race as the
    # wolfram-sidecar fixture used to have: bind() returns + file
    # appears, but listen() may not be ready when the next test
    # connect() fires. Use an actual probe connect with a 5s deadline.
    deadline = time.monotonic() + 5.0
    bound = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(f"proxy exited early: {stderr}")
        if socket_path.exists():
            try:
                probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                probe.settimeout(0.2)
                probe.connect(str(socket_path))
                probe.close()
                bound = True
                break
            except (ConnectionRefusedError, OSError):
                pass
        time.sleep(0.05)
    if not bound:
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"proxy did not accept connections: {stderr}")
    yield {
        "socket_path": socket_path,
        "ephemeral": ephemeral,
        "creds_path": creds_path,
        "audit_log": audit_log,
        "upstream_log": upstream_log,
        "proxy_proc": proc,
    }
    # Teardown: SIGTERM + wait.
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _http_over_unix(socket_path: Path, method: str, path: str,
                    *, headers: dict | None = None, body: bytes = b"") -> tuple[int, dict, bytes]:
    """Minimal HTTP/1.1 client over a unix socket. Returns (status, headers, body)."""
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(socket_path))
    req_headers = {"Host": "proxy", "Content-Length": str(len(body))}
    if headers:
        req_headers.update(headers)
    request_line = f"{method} {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in req_headers.items())
    sock.sendall((request_line + header_lines + "\r\n").encode() + body)
    raw = b""
    while True:
        chunk = sock.recv(8192)
        if not chunk:
            break
        raw += chunk
        # Stop after content length received or peer closes.
        if b"\r\n\r\n" in raw:
            head, _, rest = raw.partition(b"\r\n\r\n")
            # Parse Content-Length from head.
            for line in head.split(b"\r\n")[1:]:
                k, _, v = line.partition(b":")
                if k.strip().lower() == b"content-length":
                    expected = int(v.strip())
                    if len(rest) >= expected:
                        sock.close()
                        return _parse_response(raw)
    sock.close()
    return _parse_response(raw)


def _parse_response(raw: bytes) -> tuple[int, dict, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode()
    status = int(status_line.split(" ")[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        if k:
            headers[k.decode().lower()] = v.strip().decode()
    return status, headers, body


def test_proxy_forwards_with_correct_token(proxy_setup) -> None:
    """A request with the right ephemeral token gets forwarded and the
    real API key is substituted in upstream."""
    s = proxy_setup
    status, headers, body = _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={
            "Authorization": f"Bearer {s['ephemeral']}",
            "Content-Type": "application/json",
        },
        body=b'{"model":"test"}',
    )
    assert status == 200
    assert b"ok" in body
    # Upstream saw the request.
    log = s["upstream_log"]
    assert len(log) == 1
    assert log[0]["path"] == "/v1/messages"
    # Real API key was substituted.
    assert log[0]["x_api_key"] == "real-secret-key-abcdef"
    # Ephemeral token did NOT reach upstream.
    assert s["ephemeral"] not in (log[0]["x_api_key"] or "")
    assert s["ephemeral"] not in (log[0]["authorization"] or "")


def _load_proxy_module():
    """Import proxy.py as a module so we can test pure helpers in-process."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("anthropic_proxy_test", PROXY_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_real_credential_parses_plain_api_key(tmp_path, monkeypatch) -> None:
    """Plain-string credential file → kind='api_key', value=stripped contents."""
    proxy = _load_proxy_module()
    creds = tmp_path / "creds"
    creds.write_text("sk-ant-direct-key\n")
    os.chmod(creds, 0o600)
    monkeypatch.setenv("BOTAINER_PROXY_SOCKET_PATH", str(tmp_path / "p.sock"))
    monkeypatch.setenv("BOTAINER_PROXY_EPHEMERAL_TOKEN", "e")
    monkeypatch.setenv("BOTAINER_PROXY_REAL_CREDS_PATH", str(creds))
    monkeypatch.setenv("BOTAINER_PROXY_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    cfg = proxy.ProxyConfig()
    kind, value = cfg.load_real_credential()
    assert kind == "api_key"
    assert value == "sk-ant-direct-key"


def test_load_real_credential_parses_api_key_json(tmp_path, monkeypatch) -> None:
    proxy = _load_proxy_module()
    creds = tmp_path / "creds"
    creds.write_text(json.dumps({"api_key": "sk-ant-from-json"}))
    os.chmod(creds, 0o600)
    monkeypatch.setenv("BOTAINER_PROXY_SOCKET_PATH", str(tmp_path / "p.sock"))
    monkeypatch.setenv("BOTAINER_PROXY_EPHEMERAL_TOKEN", "e")
    monkeypatch.setenv("BOTAINER_PROXY_REAL_CREDS_PATH", str(creds))
    monkeypatch.setenv("BOTAINER_PROXY_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    cfg = proxy.ProxyConfig()
    kind, value = cfg.load_real_credential()
    assert kind == "api_key"
    assert value == "sk-ant-from-json"


def test_load_real_credential_parses_oauth_shape(tmp_path, monkeypatch) -> None:
    """Proxy OAuth READ half (PROXY-REFRESH-INVESTIGATION.md Gap 2).

    Claude Code's `claude /login` writes credentials in the shape:
        {"claudeAiOauth": {"accessToken": "oat-...", "refreshToken": "...",
                             "expiresAt": <epoch_ms>, ...}}
    The proxy must extract accessToken and signal kind='oauth_access_token'
    so the handler uses `Authorization: Bearer` instead of `x-api-key`.
    """
    proxy = _load_proxy_module()
    creds = tmp_path / "creds"
    creds.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "oat-abc-123",
            "refreshToken": "ort-xyz-456",
            "expiresAt": 1_899_999_999_000,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }))
    os.chmod(creds, 0o600)
    monkeypatch.setenv("BOTAINER_PROXY_SOCKET_PATH", str(tmp_path / "p.sock"))
    monkeypatch.setenv("BOTAINER_PROXY_EPHEMERAL_TOKEN", "e")
    monkeypatch.setenv("BOTAINER_PROXY_REAL_CREDS_PATH", str(creds))
    monkeypatch.setenv("BOTAINER_PROXY_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    cfg = proxy.ProxyConfig()
    kind, value = cfg.load_real_credential()
    assert kind == "oauth_access_token"
    assert value == "oat-abc-123"


def test_load_real_credential_refuses_unknown_json(tmp_path, monkeypatch) -> None:
    """JSON we don't recognize is refused, not silently forwarded as the
    full blob (the pre-fix bug from PROXY-REFRESH-INVESTIGATION Gap 2)."""
    proxy = _load_proxy_module()
    creds = tmp_path / "creds"
    creds.write_text(json.dumps({"some_future_field": "oat-future"}))
    os.chmod(creds, 0o600)
    monkeypatch.setenv("BOTAINER_PROXY_SOCKET_PATH", str(tmp_path / "p.sock"))
    monkeypatch.setenv("BOTAINER_PROXY_EPHEMERAL_TOKEN", "e")
    monkeypatch.setenv("BOTAINER_PROXY_REAL_CREDS_PATH", str(creds))
    monkeypatch.setenv("BOTAINER_PROXY_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    cfg = proxy.ProxyConfig()
    with pytest.raises(RuntimeError, match="neither an `api_key`"):
        cfg.load_real_credential()


@pytest.fixture
def oauth_proxy_setup(tmp_path: Path, fake_upstream):
    """Like proxy_setup but the credential file is OAuth-shaped."""
    port, upstream_log = fake_upstream
    socket_path = tmp_path / "proxy.sock"
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "oat-from-real-login",
            "refreshToken": "ort-irrelevant-here",
            "expiresAt": 1_899_999_999_000,
        }
    }))
    os.chmod(creds_path, 0o600)
    audit_log = tmp_path / "audit.jsonl"
    ephemeral = "ephem-token-oauth-flow"
    env = {
        **os.environ,
        "BOTAINER_PROXY_SOCKET_PATH": str(socket_path),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": ephemeral,
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds_path),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit_log),
        "BOTAINER_PROXY_UPSTREAM": f"http://127.0.0.1:{port}",
        "BOTAINER_PROXY_MAX_REQUEST_BYTES": "1024",
        "BOTAINER_PROXY_RATE_LIMIT_RPS": "10",
    }
    proc = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    # Probe-connect with 5s deadline (see proxy_setup fixture for
    # rationale — bind() returning isn't the same as accepting).
    deadline = time.monotonic() + 5.0
    bound = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if socket_path.exists():
            try:
                probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                probe.settimeout(0.2)
                probe.connect(str(socket_path))
                probe.close()
                bound = True
                break
            except (ConnectionRefusedError, OSError):
                pass
        time.sleep(0.05)
    if not bound:
        proc.terminate()
        pytest.fail("proxy didn't accept connections in 5s")
    try:
        yield {
            "socket_path": socket_path,
            "ephemeral": ephemeral,
            "upstream_log": upstream_log,
            "audit_log": audit_log,
            "proc": proc,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_proxy_oauth_uses_bearer_header_upstream(oauth_proxy_setup) -> None:
    """End-to-end: with an OAuth credential file the proxy sends
    Authorization: Bearer <access_token> upstream, not x-api-key."""
    s = oauth_proxy_setup
    status, _, _ = _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"Authorization": f"Bearer {s['ephemeral']}",
                 "Content-Type": "application/json"},
        body=b'{"model":"test"}',
    )
    assert status == 200
    log = s["upstream_log"]
    assert len(log) == 1
    # Upstream got Bearer <real-access-token>, NOT x-api-key.
    assert log[0]["authorization"] == "Bearer oat-from-real-login"
    assert log[0]["x_api_key"] is None
    # Ephemeral did not leak.
    assert s["ephemeral"] not in (log[0]["authorization"] or "")


def test_proxy_refuses_wrong_token(proxy_setup) -> None:
    s = proxy_setup
    status, _, body = _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"Authorization": "Bearer wrong-token"},
        body=b"{}",
    )
    assert status == 403
    assert b"invalid session token" in body
    # Upstream was not contacted.
    assert s["upstream_log"] == []


def test_proxy_refuses_missing_token(proxy_setup) -> None:
    s = proxy_setup
    status, _, _ = _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        body=b"{}",
    )
    assert status == 403


def test_proxy_accepts_x_api_key_header(proxy_setup) -> None:
    """Anthropic SDK also uses x-api-key header (not just Bearer)."""
    s = proxy_setup
    status, _, _ = _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"x-api-key": s["ephemeral"]},
        body=b"{}",
    )
    assert status == 200


def test_proxy_refuses_body_too_large(proxy_setup) -> None:
    """Configured max is 1024 bytes."""
    s = proxy_setup
    status, _, body = _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"Authorization": f"Bearer {s['ephemeral']}"},
        body=b"x" * 2048,
    )
    assert status == 413
    assert b"body exceeds" in body


def test_proxy_audits_forwarded_request(proxy_setup) -> None:
    s = proxy_setup
    _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"Authorization": f"Bearer {s['ephemeral']}"},
        body=b'{"hello":"world"}',
    )
    time.sleep(0.1)  # let audit flush
    content = s["audit_log"].read_text()
    lines = [json.loads(line) for line in content.strip().split("\n") if line]
    # Filter to forwarded events (skip the proxy_started startup event).
    forwarded = [e for e in lines if e.get("kind") == "forwarded"]
    assert len(forwarded) == 1
    assert forwarded[0]["path"] == "/v1/messages"
    assert forwarded[0]["status"] == 200
    assert forwarded[0]["request_bytes"] == len(b'{"hello":"world"}')
    # Task #300: proxy no longer records 4 KiB of every request body in
    # the audit log (user prompts can contain credentials). Default is
    # sha256 prefix only. The verbatim preview is opt-in via
    # BOTAINER_PROXY_AUDIT_BODIES=1.
    import hashlib as _h
    expected_sha8 = _h.sha256(b'{"hello":"world"}').hexdigest()[:8]
    assert forwarded[0].get("request_body_sha8") == expected_sha8


def test_proxy_audits_rejected_auth(proxy_setup) -> None:
    s = proxy_setup
    _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"Authorization": "Bearer wrong"},
        body=b"{}",
    )
    time.sleep(0.1)
    lines = [json.loads(line) for line in s["audit_log"].read_text().strip().split("\n") if line]
    rejected = [e for e in lines if e.get("kind") == "auth_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["status"] == -1


def test_proxy_socket_is_mode_0600(proxy_setup) -> None:
    """The unix socket is restricted to owner."""
    s = proxy_setup
    mode = s["socket_path"].stat().st_mode & 0o777
    assert mode == 0o600


def test_proxy_audit_log_mode_0600(proxy_setup) -> None:
    """The audit log starts at mode 0600 even when umask is loose."""
    s = proxy_setup
    # Trigger first write.
    _http_over_unix(
        s["socket_path"], "POST", "/v1/messages",
        headers={"Authorization": f"Bearer {s['ephemeral']}"},
        body=b"{}",
    )
    time.sleep(0.1)
    mode = s["audit_log"].stat().st_mode & 0o777
    assert mode == 0o600


# ────────── start_proxy.py hook tests ──────────


def test_start_proxy_hook_refuses_missing_credentials(tmp_path: Path) -> None:
    """If the per-project credentials file is missing, hook refuses."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="uuid",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))

    hook = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "agent-claude-proxy" / "hooks" / "start_proxy.py"
    )
    proc = subprocess.run(
        [sys.executable, str(hook)],
        env={
            **os.environ,
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            # Bypass the T0-3 not-functional guard so we exercise the
            # missing-credentials refusal (the guard would fire first
            # otherwise). See start_proxy.py::main().
            "BOTAINER_PROXY_EXPERIMENTAL": "1",
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "credentials file not found" in proc.stderr


def test_start_proxy_hook_with_override_path(tmp_path: Path) -> None:
    """Use BOTAINER_PROXY_CREDS_PATH_OVERRIDE for testing."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)

    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="uuid",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))

    # Use the same env-overrides approach as in proxy_setup.
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"api_key": "override-key"}))

    hook = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "agent-claude-proxy" / "hooks" / "start_proxy.py"
    )
    # Use an upstream-pointing-nowhere URL: the hook only spawns the
    # proxy, doesn't make a request to it. So the proxy should start
    # cleanly with whatever upstream we configure.
    proc = subprocess.run(
        [sys.executable, str(hook)],
        env={
            **os.environ,
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            # BOTAINER_TESTING=1 gates the creds-path override (security fix:
            # otherwise a stale env var could redirect the credential read).
            "BOTAINER_TESTING": "1",
            "BOTAINER_PROXY_CREDS_PATH_OVERRIDE": str(creds),
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    assert contribution["kind"] == "pre_session"
    assert contribution["env"]["ANTHROPIC_API_KEY"]
    assert contribution["binds"][0]["target"] == "/run/anthropic-proxy.sock"
    pid = contribution["proxy_pid"]
    assert isinstance(pid, int)

    # Cleanup: stop the spawned proxy.
    import signal as _sig
    try:
        os.kill(pid, _sig.SIGTERM)
    except ProcessLookupError:
        pass


def test_proxy_watchdog_shuts_down_when_launcher_pid_disappears(tmp_path: Path) -> None:
    """#108: proxy must exit when its declared launcher PID is gone.

    Without this watchdog, a SIGKILL on the launcher (or OOM, or
    reboot) leaves the proxy orphaned — start_new_session=True
    detaches it from the launcher's process group, and stop_proxy.py
    (post_session) never runs. Next session then fails with "address
    in use" on the same UNIX socket path.

    Test shape:
      1. Spawn a short-lived "fake launcher" process (sleep 1).
      2. Spawn the real proxy with BOTAINER_LAUNCHER_PID=<fake>.
      3. Wait long enough for the fake launcher to exit AND for
         the watchdog's 30s poll to fire.

    Polling-loop runtime makes this slow (~30-60s). Marked
    integration-flavor but kept here so the test sits next to the
    code it pins. Test is opt-in via BOTAINER_PROXY_LONG_TESTS=1
    to keep `pytest tests/unit/` under a minute by default.
    """
    if os.environ.get("BOTAINER_PROXY_LONG_TESTS") != "1":
        import pytest as _pytest
        _pytest.skip("set BOTAINER_PROXY_LONG_TESTS=1 to enable")

    proxy_script = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "agent-claude-proxy" / "proxy.py"
    )
    # 1. Fake launcher: a sleep 1 that exits quickly.
    fake_launcher = subprocess.Popen(["sleep", "1"])
    # 2. Minimal creds file + socket env.
    creds_path = tmp_path / "creds"
    creds_path.write_text("fake-key-for-watchdog-test")
    sock_path = tmp_path / "watchdog.sock"
    proxy_env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "BOTAINER_PROXY_SOCKET_PATH": str(sock_path),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "test-token",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds_path),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "BOTAINER_PROXY_UPSTREAM": "https://api.anthropic.com",
        "BOTAINER_PROXY_MAX_REQUEST_BYTES": "1048576",
        "BOTAINER_PROXY_RATE_LIMIT_RPS": "60",
        "BOTAINER_PROXY_REDACT_BODIES": "0",
        "BOTAINER_LAUNCHER_PID": str(fake_launcher.pid),
    }
    proxy = subprocess.Popen(
        [sys.executable, str(proxy_script)],
        env=proxy_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        fake_launcher.wait(timeout=5)  # exits in ~1s.
        # 3. Watchdog polls every 30s; wait up to 60s for it to fire.
        proxy.wait(timeout=60)
        assert proxy.returncode is not None, (
            "proxy did NOT exit after launcher disappeared — watchdog broken"
        )
    finally:
        if proxy.poll() is None:
            proxy.kill()
        if fake_launcher.poll() is None:
            fake_launcher.kill()


def test_stop_proxy_hook_kills_recorded_pid(tmp_path: Path) -> None:
    """post_session reads runtime_handle.proxy.pid and sends SIGTERM.

    #205 — THIS TEST WAS LOAD-FLAKY AND BLOCKED A COMMIT. It passed 3/3 alone
    and failed when a second pytest run was going. The cause was not the
    behaviour: the timeouts below exist to stop a HANG from wedging the suite,
    and at 10s/5s they had quietly become assertions about SPEED. Spawning a
    Python process that imports botainer, on a loaded box, can exceed ten
    seconds; `subprocess.run` then raises TimeoutExpired and the test errors
    while the code under test is fine.

    So the guards are generous now, and the ASSERTIONS got stricter rather than
    looser — the fix for a flaky test must not be to ask it less:

      was: `sleeper.returncode != 0`   — passes if ANYTHING killed it
      now: `== -signal.SIGTERM`        — the hook sent SIGTERM, specifically

    A timeout that fires is now reported as "the hook did not finish", which is
    a real failure worth seeing, not an opaque TimeoutExpired traceback.
    """
    # A long-lived process we can SIGTERM. Killed in the `finally` regardless of
    # outcome — the old version leaked a `sleep 60` on every failure.
    sleeper = subprocess.Popen(["sleep", "60"])
    session_dir = tmp_path / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)

    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(tmp_path),
        runtime="docker",
        image="img",
    )
    rec = sr.from_spec(spec)
    sr.write(session_dir, rec)
    # Now manually attach the proxy info to the record.
    record_path = session_dir / sr.RECORD_FILENAME
    record = json.loads(record_path.read_text())
    record.setdefault("runtime_handle", {})["proxy"] = {
        "pid": sleeper.pid,
        "socket_path": str(session_dir / "x.sock"),
    }
    record_path.write_text(json.dumps(record))

    hook = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "agent-claude-proxy" / "hooks" / "stop_proxy.py"
    )
    try:
        try:
            proc = subprocess.run(
                [sys.executable, str(hook)],
                env={
                    **os.environ,
                    "BOTAINER_SESSION_RECORD_PATH": str(record_path),
                },
                capture_output=True,
                text=True,
                # HANG GUARD, not a speed assertion. See the docstring.
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "stop_proxy.py did not finish within 120s — that is a hang, "
                "not slowness") from None
        assert proc.returncode == 0, proc.stderr
        try:
            sleeper.wait(timeout=60)      # hang guard, not a speed assertion
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"the hook exited 0 but pid {sleeper.pid} is still alive after "
                f"60s — it did not signal the recorded pid") from None
        assert sleeper.returncode == -signal.SIGTERM, (
            f"expected SIGTERM (-{int(signal.SIGTERM)}), got "
            f"{sleeper.returncode} — something killed it, but not this hook")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=10)
