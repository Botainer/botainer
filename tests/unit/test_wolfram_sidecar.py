"""Tests for the wolfram-sidecar plugin.

The proxy is stdlib-only Python with a unix-socket transport; we can
spin it up against a fake wolframscript on a temp socket and verify
the full request/response flow, string filter, token auth, and
-file refusal.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "wolfram-sidecar"
PROXY_SCRIPT = PLUGIN_DIR / "proxy.py"
CLIENT_SCRIPT = PLUGIN_DIR / "client.py"
SANDBOX_PROFILE = PLUGIN_DIR / "sandbox" / "wolfram-sandbox.sb"
SANDBOX_INIT = PLUGIN_DIR / "sandbox" / "wolfram-sandbox-init.wl"


# ────────── manifest + bundle ──────────


def test_manifest_loads_and_is_first_party() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(PLUGIN_DIR)
    assert m.name == "wolfram-sidecar"
    assert m.tier == "first-party"
    assert m.kind == "host_helper"
    whens = sorted(h.when for h in m.hooks)
    assert whens == ["post_session", "pre_session"]


def test_wolfram_sidecar_in_builtin_names() -> None:
    from botainer.plugins.builtin import BUILTIN_PLUGIN_NAMES
    assert "wolfram-sidecar" in BUILTIN_PLUGIN_NAMES


def test_wolfram_sidecar_in_trust_lock() -> None:
    from botainer.plugins import trust
    lock = trust.read_trust_lock(trust.lock_path_for_install())
    assert "wolfram-sidecar" in lock


def test_manifest_envelope_covers_all_contributed_targets() -> None:
    """Every target the pre_session hook contributes must be inside
    the manifest's mount_target_prefixes (composition's strict
    envelope check refuses otherwise)."""
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(PLUGIN_DIR)
    prefixes = m.contributes.mount_target_prefixes
    for target in (
        "/run/wolfram-proxy.sock",
        "/run/wolfram-token",
        "/usr/local/bin/wolframscript",
    ):
        assert any(
            target == p or target.startswith(p.rstrip("/") + "/")
            for p in prefixes
        ), f"target {target!r} not covered by envelope {prefixes}"


def test_sandbox_files_are_bundled() -> None:
    """The two sandbox files (sb profile + init.wl) must ship with the
    plugin so the proxy can load them at startup."""
    assert SANDBOX_PROFILE.is_file()
    assert SANDBOX_INIT.is_file()
    # Sandbox files mention the expected directives so a typo-edit
    # at copy time would surface.
    assert "deny file-write" in SANDBOX_PROFILE.read_text(encoding="utf-8")
    assert "General::sandbox" in SANDBOX_INIT.read_text(encoding="utf-8")


# ────────── proxy.py module-level helpers ──────────


def _load_proxy_module():
    """Import proxy.py in-process for pure-Python testing."""
    if "wolfram_proxy_test" in sys.modules:
        del sys.modules["wolfram_proxy_test"]
    spec = importlib.util.spec_from_file_location("wolfram_proxy_test", PROXY_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wolfram_proxy_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_blocked_patterns_includes_dangerous_wolfram_calls() -> None:
    """Layer-1 string filter must cover the canonical bypass attempts."""
    proxy = _load_proxy_module()
    expected_subset = {
        "Run[", "Run @", "Run [",          # @-prefix + space-form bypass
        "RunProcess[", "StartProcess[",
        "Install[", "LibraryFunctionLoad[",
        "Get[", "Get @", "Get [",
        "ToExpression[",                    # dynamic-eval bypass vector
        "FromCharacterCode[", "StringJoin[",
        "URLFetch[", "URLRead[",
        'Import["http', 'Import["https',
        "Export[", "DeleteFile[", "OpenWrite[",
        "ExternalEvaluate[", "JLink",
    }
    actual = set(proxy.BLOCKED_PATTERNS)
    missing = expected_subset - actual
    assert not missing, f"missing patterns: {sorted(missing)}"


# ────────── end-to-end proxy spawn over unix socket ──────────


@pytest.fixture
def fake_wolframscript(tmp_path: Path):
    """A stub wolframscript that prints stdout/stderr summaries of its
    argv. Returncode 0 unless argv contains 'FAIL'."""
    fake = tmp_path / "fake-wolframscript.sh"
    fake.write_text(
        "#!/bin/bash\n"
        'echo "stdout-from-fake: $*"\n'
        'echo "stderr-from-fake: $*" 1>&2\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "FAIL" ]; then exit 7; fi\n'
        'done\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


@pytest.fixture
def proxy_setup(tmp_path: Path, fake_wolframscript: Path):
    """Spawn the proxy as a subprocess on a unix socket; teardown after."""
    socket_path = tmp_path / "wolfram.sock"
    token_file = tmp_path / "wolfram-token"
    audit_log = tmp_path / "audit.jsonl"
    token = "test-token-abc123"
    token_file.write_text(token)
    os.chmod(token_file, 0o600)

    env = {
        **os.environ,
        "BOTAINER_PROXY_SOCKET_PATH": str(socket_path),
        "BOTAINER_PROXY_TOKEN_FILE": str(token_file),
        "BOTAINER_PROXY_WOLFRAMSCRIPT": str(fake_wolframscript),
        "BOTAINER_PROXY_TIMEOUT": "10",
        "BOTAINER_PROXY_MAX_REQUEST_BYTES": "8192",
        # Linux dev container: no sandbox-exec; opt out to start.
        "BOTAINER_PROXY_REQUIRE_SANDBOX": "0",
        "BOTAINER_PROXY_SANDBOX_PROFILE": str(SANDBOX_PROFILE),
        "BOTAINER_PROXY_SANDBOX_INIT": str(SANDBOX_INIT),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit_log),
    }
    proc = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Wait up to 5s for the proxy to (a) create the socket file AND
    # (b) start accepting connections. Just checking `socket_path.exists()`
    # raced: under load the socket file appeared (bind() returned) but
    # listen() hadn't completed → next connect() got ECONNREFUSED.
    # Now: actually attempt a probe connect with a tight timeout.
    deadline = time.monotonic() + 5.0
    bound = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            proc.wait(timeout=1)
            raise RuntimeError(f"proxy exited early: rc={proc.returncode}\n{err}")
        if socket_path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
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
            "token": token,
            "token_file": token_file,
            "audit_log": audit_log,
            "proc": proc,
            "fake_wolframscript": fake_wolframscript,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _send_request(socket_path: Path, body: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(socket_path))
    sock.sendall(json.dumps(body).encode("utf-8"))
    sock.shutdown(socket.SHUT_WR)
    chunks: list[bytes] = []
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.append(data)
    sock.close()
    return json.loads(b"".join(chunks))


def test_proxy_forwards_with_valid_token(proxy_setup) -> None:
    s = proxy_setup
    resp = _send_request(s["socket_path"], {
        "args": ["-code", "Integrate[x^2, x]"],
        "auth": s["token"],
    })
    assert resp["returncode"] == 0
    assert "stdout-from-fake" in resp["stdout"]
    # The kernel-init code is prepended to user -code; the fake just
    # echoes its argv so we'll see both.
    assert "Integrate[x^2, x]" in resp["stdout"]


def test_proxy_refuses_wrong_token(proxy_setup) -> None:
    s = proxy_setup
    resp = _send_request(s["socket_path"], {
        "args": ["-code", "1+1"],
        "auth": "wrong-token",
    })
    assert resp["returncode"] == 1
    assert "Authentication required" in resp["stderr"]


def test_proxy_refuses_missing_token(proxy_setup) -> None:
    s = proxy_setup
    resp = _send_request(s["socket_path"], {
        "args": ["-code", "1+1"],
    })
    assert resp["returncode"] == 1
    assert "Authentication" in resp["stderr"]


def test_proxy_refuses_dash_file(proxy_setup) -> None:
    """v0.0.12 B2 ported: -file is unbounded path input; refuse at the
    proxy layer."""
    s = proxy_setup
    resp = _send_request(s["socket_path"], {
        "args": ["-file", "/etc/passwd"],
        "auth": s["token"],
    })
    assert resp["returncode"] == 1
    assert "-file is refused" in resp["stderr"]


def test_proxy_refuses_blocked_pattern(proxy_setup) -> None:
    s = proxy_setup
    resp = _send_request(s["socket_path"], {
        "args": ["-code", 'Run["id"]'],
        "auth": s["token"],
    })
    assert resp["returncode"] == 1
    assert "Blocked:" in resp["stderr"]
    assert "Run[" in resp["stderr"]


def test_proxy_refuses_bypass_attempt_dollar_at_prefix(proxy_setup) -> None:
    """`Run @ "id"` is the canonical Wolfram metaprogramming bypass.
    The pattern list explicitly includes `Run @` to catch it."""
    s = proxy_setup
    resp = _send_request(s["socket_path"], {
        "args": ["-code", 'Run @ "id"'],
        "auth": s["token"],
    })
    assert resp["returncode"] == 1
    assert "Run @" in resp["stderr"]


def test_proxy_refuses_body_too_large(proxy_setup) -> None:
    """max_request_bytes=8192 in the fixture; send 16 KiB."""
    s = proxy_setup
    big = "x" * 16384
    resp = _send_request(s["socket_path"], {
        "args": ["-code", big],
        "auth": s["token"],
    })
    assert resp["returncode"] == 1
    assert "exceeds" in resp["stderr"]


def test_proxy_refuses_malformed_json(proxy_setup) -> None:
    s = proxy_setup
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(s["socket_path"]))
    sock.sendall(b"this is not json")
    sock.shutdown(socket.SHUT_WR)
    data = sock.recv(65536)
    sock.close()
    resp = json.loads(data)
    assert resp["returncode"] == 1
    assert "Malformed" in resp["stderr"]


def test_proxy_audit_log_records_events(proxy_setup) -> None:
    s = proxy_setup
    # one allowed request + one refused.
    _send_request(s["socket_path"], {"args": ["-code", "1+1"], "auth": s["token"]})
    _send_request(s["socket_path"], {"args": ["-code", "Run[\"id\"]"], "auth": s["token"]})
    # Audit log should have proxy_started + 2 request events.
    time.sleep(0.1)   # let writes flush
    log = (s["audit_log"]).read_text(encoding="utf-8")
    lines = [ln for ln in log.splitlines() if ln.strip()]
    kinds = [json.loads(ln).get("kind") for ln in lines]
    assert "proxy_started" in kinds
    assert "request_forwarded" in kinds or "request_refused" in kinds
    # The refused one definitely has reason=string_filter.
    refused_reasons = [
        json.loads(ln).get("reason") for ln in lines
        if json.loads(ln).get("kind") == "request_refused"
    ]
    assert "string_filter" in refused_reasons


def test_proxy_per_request_timeout_capped_by_global(proxy_setup) -> None:
    """request.timeout=100 is capped to global TIMEOUT=10 from env."""
    s = proxy_setup
    # The fake wolframscript completes instantly, so the actual cap
    # isn't exercised, but we can verify the proxy accepts the field.
    resp = _send_request(s["socket_path"], {
        "args": ["-code", "1+1"],
        "auth": s["token"],
        "timeout": 100,
    })
    assert resp["returncode"] == 0


# ────────── client.py ──────────


def test_client_sends_token_and_strips_proxy_timeout(tmp_path: Path) -> None:
    """The in-container client reads /run/wolfram-token, sends `auth`
    on the request, and strips --proxy-timeout from argv."""
    socket_path = tmp_path / "fake.sock"
    token_file = tmp_path / "tok"
    token_file.write_text("client-test-token")

    # Stand up a one-shot server that captures the request payload.
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    import threading
    received: dict = {}

    def _accept():
        conn, _ = server.accept()
        chunks: list[bytes] = []
        while True:
            d = conn.recv(65536)
            if not d:
                break
            chunks.append(d)
        received["body"] = json.loads(b"".join(chunks))
        # Reply with a known response.
        conn.sendall(json.dumps({
            "returncode": 0, "stdout": "hello\n", "stderr": "",
        }).encode("utf-8"))
        conn.close()

    t = threading.Thread(target=_accept, daemon=True)
    t.start()

    env = {
        **os.environ,
        "WOLFRAM_PROXY_SOCKET": str(socket_path),
        "WOLFRAM_PROXY_TOKEN_FILE": str(token_file),
    }
    proc = subprocess.run(
        [sys.executable, str(CLIENT_SCRIPT),
         "--proxy-timeout", "30",
         "-code", "Integrate[x, x]"],
        env=env, capture_output=True, text=True, timeout=5,
    )
    t.join(timeout=2)

    try:
        server.close()
    finally:
        try:
            os.unlink(socket_path)
        except OSError:
            pass

    assert proc.returncode == 0
    assert proc.stdout == "hello\n"
    # Verify what the proxy actually saw.
    body = received["body"]
    assert body["auth"] == "client-test-token"
    assert body["timeout"] == 30
    # --proxy-timeout was stripped from args; -code + expression remain.
    assert "--proxy-timeout" not in body["args"]
    assert body["args"] == ["-code", "Integrate[x, x]"]


def test_client_clear_error_when_socket_unreachable(tmp_path: Path) -> None:
    """The client prints an actionable message if the proxy socket
    doesn't exist (proxy isn't running)."""
    env = {
        **os.environ,
        "WOLFRAM_PROXY_SOCKET": str(tmp_path / "no-such.sock"),
    }
    proc = subprocess.run(
        [sys.executable, str(CLIENT_SCRIPT), "-code", "1+1"],
        env=env, capture_output=True, text=True, timeout=5,
    )
    assert proc.returncode == 1
    assert "not reachable" in proc.stderr
    assert "wolfram-sidecar" in proc.stderr  # actionable hint
