"""Tests for the proxy hardening (sharp-edges HIGH 4 + HIGH 5)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROXY_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "agent-claude-proxy" / "proxy.py"
)
START_HOOK = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "agent-claude-proxy" / "hooks" / "start_proxy.py"
)


def _spawn_proxy(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Spawn the proxy directly and capture its early-exit error."""
    base_env = {
        "BOTAINER_PROXY_SOCKET_PATH": "/tmp/test-proxy.sock",
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "eph",
        "BOTAINER_PROXY_REAL_CREDS_PATH": "/tmp/test-creds.json",
        "BOTAINER_PROXY_AUDIT_LOG": "/tmp/test-audit.jsonl",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    base_env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(PROXY_SCRIPT)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=5,
    )


# ────────── HIGH 5: upstream hostname allowlist ──────────


def test_proxy_refuses_arbitrary_upstream_host() -> None:
    """An attacker-controlled upstream URL is refused at proxy startup."""
    result = _spawn_proxy({
        "BOTAINER_PROXY_UPSTREAM": "https://attacker.example.com",
    })
    assert result.returncode != 0
    assert "allowlist" in result.stderr.lower()
    assert "attacker.example.com" in result.stderr


def test_proxy_refuses_metadata_service_upstream() -> None:
    """AWS instance metadata service URL is refused."""
    result = _spawn_proxy({
        "BOTAINER_PROXY_UPSTREAM": "http://169.254.169.254/latest/meta-data/",
    })
    assert result.returncode != 0


def test_proxy_refuses_internal_network() -> None:
    """RFC1918 / internal hosts refused."""
    result = _spawn_proxy({
        "BOTAINER_PROXY_UPSTREAM": "http://internal.corp/admin",
    })
    assert result.returncode != 0


def test_proxy_refuses_plain_http_to_anthropic() -> None:
    """http:// (vs https://) is refused for the production API."""
    result = _spawn_proxy({
        "BOTAINER_PROXY_UPSTREAM": "http://api.anthropic.com",
    })
    assert result.returncode != 0
    assert "https" in result.stderr.lower() or "loopback" in result.stderr.lower()


def _spawn_proxy_alive(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.Popen[bytes]:
    """Spawn the proxy in background. Caller must terminate it."""
    base_env = {
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "proxy.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "eph",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(tmp_path / "creds.json"),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    base_env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)],
        env=base_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_proxy(proc: subprocess.Popen[bytes]) -> None:
    """Robust proxy teardown: SIGTERM then SIGKILL after short wait."""
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def test_proxy_accepts_https_anthropic(tmp_path: Path) -> None:
    """Default upstream (https://api.anthropic.com) passes validation.
    Proxy starts and runs; we kill it to verify it actually started."""
    proc = _spawn_proxy_alive(
        {"BOTAINER_PROXY_UPSTREAM": "https://api.anthropic.com"},
        tmp_path,
    )
    import time
    for _ in range(20):
        if (tmp_path / "proxy.sock").exists():
            break
        time.sleep(0.05)
    try:
        assert (tmp_path / "proxy.sock").exists(), "proxy didn't start listening"
    finally:
        _stop_proxy(proc)


def test_proxy_accepts_http_loopback(tmp_path: Path) -> None:
    """http://127.0.0.1 is allowed for testing."""
    proc = _spawn_proxy_alive(
        {"BOTAINER_PROXY_UPSTREAM": "http://127.0.0.1:9999"},
        tmp_path,
    )
    import time
    for _ in range(20):
        if (tmp_path / "proxy.sock").exists():
            break
        time.sleep(0.05)
    try:
        assert (tmp_path / "proxy.sock").exists()
    finally:
        _stop_proxy(proc)


# ────────── HIGH 4: start_proxy.py doesn't pass through stale shell vars ──────────


def test_start_proxy_does_not_inherit_stale_upstream_env(
    tmp_path: Path,
) -> None:
    """If the user has BOTAINER_PROXY_UPSTREAM set in their shell, the
    hook ignores it and uses the plugin config's value.

    Without this defense, a malicious wrapper could redirect upstream
    by setting an env var before invoking botainer.
    """
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))

    creds = tmp_path / "creds.json"
    creds.write_text('{"api_key": "test"}')
    (tmp_path / "proj").mkdir()

    # Set a malicious BOTAINER_PROXY_UPSTREAM in the env we pass.
    # The hook should ignore it (build env from scratch).
    result = subprocess.run(
        [sys.executable, str(START_HOOK)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            "BOTAINER_TESTING": "1",
            "BOTAINER_PROXY_CREDS_PATH_OVERRIDE": str(creds),
            # ATTACK: stale shell var trying to redirect upstream.
            "BOTAINER_PROXY_UPSTREAM": "https://attacker.example.com",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    # If the hook passed BOTAINER_PROXY_UPSTREAM through, the proxy would
    # have started successfully with the malicious upstream. Now the hook
    # builds env from scratch and the proxy starts with the default
    # (https://api.anthropic.com). The proxy itself accepts the default,
    # so the hook succeeds (rc=0).
    assert result.returncode == 0, result.stderr
    # Stop the spawned proxy.
    import json as _json
    import signal as _sig
    payload = _json.loads(result.stdout)
    try:
        os.kill(payload["proxy_pid"], _sig.SIGTERM)
    except ProcessLookupError:
        pass


def test_start_proxy_refuses_creds_override_without_testing_flag(
    tmp_path: Path,
) -> None:
    """BOTAINER_PROXY_CREDS_PATH_OVERRIDE only takes effect with
    BOTAINER_TESTING=1 (defense against stale env redirecting creds)."""
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))

    bogus_creds = tmp_path / "bogus.json"
    bogus_creds.write_text('{"api_key": "bogus"}')
    (tmp_path / "proj").mkdir()

    # No BOTAINER_TESTING=1 → override ignored → real creds path used →
    # which doesn't exist → hook refuses with "credentials file not found".
    # BOTAINER_PROXY_EXPERIMENTAL=1 bypasses the T0-3 not-functional guard so
    # this test still exercises the creds-override gating (the guard fires
    # first otherwise, and we'd be asserting the wrong refusal).
    result = subprocess.run(
        [sys.executable, str(START_HOOK)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            "BOTAINER_PROXY_CREDS_PATH_OVERRIDE": str(bogus_creds),
            "BOTAINER_PROXY_EXPERIMENTAL": "1",  # bypass the T0-3 guard only
            # NOT setting BOTAINER_TESTING=1
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "credentials file not found" in result.stderr


def test_start_proxy_refuses_as_not_functional_by_default(tmp_path: Path) -> None:
    """T0-3 / #59: without an escape hatch, the proxy hook fails fast with an
    explicit 'NOT FUNCTIONAL at v0.1.0' refusal BEFORE spawning anything.

    Rationale: the proxy's ephemeral ANTHROPIC_API_KEY is refused by the
    launcher's credential-leak guard, so a proxy session can't start on any
    runtime. The hook must refuse legibly here rather than spawn the proxy and
    die later at composition.py's leak check with a confusing message.
    """
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))
    (tmp_path / "proj").mkdir()

    result = subprocess.run(
        [sys.executable, str(START_HOOK)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            # NEITHER BOTAINER_TESTING nor BOTAINER_PROXY_EXPERIMENTAL set.
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "NOT FUNCTIONAL" in result.stderr
    # Fails fast BEFORE emitting any contribution (no proxy spawned, so no
    # proxy_pid to leak or clean up).
    assert result.stdout.strip() == ""


# ────────── CREDENTIAL-PROXY-INVESTIGATION findings ──────────


def test_proxy_refuses_world_readable_credential_file(tmp_path: Path) -> None:
    """Investigation finding #1: file mode 0644 must be refused.

    A buggy write left the file world-readable; previously the proxy
    silently read it and forwarded the key. Now: refuse loudly.
    """
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    # Reload the proxy module to pick up our changes.
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "bad-mode.json"
    creds.write_text('{"api_key": "x"}')
    os.chmod(creds, 0o644)

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    })
    cfg = proxy_mod.ProxyConfig()
    with pytest.raises(RuntimeError, match="mode|broader than 0600"):
        cfg.load_real_credential()


def test_proxy_accepts_mode_0600_credential_file(tmp_path: Path) -> None:
    """Investigation finding #1: mode 0600 (and 0400) must be accepted."""
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "good-mode.json"
    creds.write_text('{"api_key": "secret-xyz"}')
    os.chmod(creds, 0o600)

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    })
    cfg = proxy_mod.ProxyConfig()
    # load_real_credential returns (kind, value) tuple after the
    # OAuth READ refactor (PROXY-REFRESH-INVESTIGATION.md Gap 2).
    assert cfg.load_real_credential() == ("api_key", "secret-xyz")


def test_audit_log_hash_chain_verifies_clean(tmp_path: Path) -> None:
    """Investigation finding #2: hash chain detects tampering."""
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "creds.json"
    creds.write_text('{"api_key": "x"}')
    os.chmod(creds, 0o600)
    audit = tmp_path / "audit.jsonl"

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit),
    })
    cfg = proxy_mod.ProxyConfig()
    # Write three audit entries.
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=429)
    ok, detail = proxy_mod.verify_audit_chain(audit)
    assert ok, detail


def test_audit_log_hash_chain_detects_truncation(tmp_path: Path) -> None:
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "creds.json"
    creds.write_text('{"api_key": "x"}')
    os.chmod(creds, 0o600)
    audit = tmp_path / "audit.jsonl"

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit),
    })
    cfg = proxy_mod.ProxyConfig()
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    # Attacker truncates the first entry.
    lines = audit.read_bytes().splitlines(keepends=True)
    audit.write_bytes(lines[1])  # only the second line
    ok, detail = proxy_mod.verify_audit_chain(audit)
    assert not ok
    assert "prev_hash mismatch" in detail


def test_audit_log_verify_empty_log_ok(tmp_path: Path) -> None:
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    audit = tmp_path / "audit.jsonl"
    ok, detail = proxy_mod.verify_audit_chain(audit)
    assert ok
