"""agent-claude-broker plugin: manifest + start_broker.py hook.

The broker is the FUNCTIONAL replacement for the dead agent-claude-proxy
(T0-3/#59). These tests drive the real pre_session hook the way the launcher
does (subprocess + BOTAINER_SESSION_RECORD_PATH), spawn the real broker daemon,
and assert the two things that make the broker correct where the proxy was not:

  1. The container is handed a provably-fake SENTINEL, not a real token — and
     that sentinel PASSES botainer's credential-leak guard (the proxy died here
     because it handed over a real-shaped ANTHROPIC_API_KEY).
  2. No value from the real credential file appears in the contribution.

The daemon binds its socket and runs a fail-closed startup probe purely from the
credential file (no network), so these tests are hermetic.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from botainer.core.broker_sentinel import is_sentinel
from botainer.core.credential_leak_check import check_env_for_leaks
from botainer.core.refusal import Refused

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "agent-claude-broker"
HOOK = PLUGIN_DIR / "hooks" / "start_broker.py"

# A far-future expiresAt in MILLISECONDS (the botainer credential file's unit),
# so the daemon's startup probe sees a fresh token and never tries to refresh.
_YEAR_2100_MS = 4102444800000
_FAKE_ACCESS = "sk-ant-oat-FAKE-broker-test-access-token-not-real-000000000000"
_FAKE_REFRESH = "sk-ant-ort-FAKE-broker-test-refresh-token-not-real-00000000000"


def _write_fake_credential(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": _FAKE_ACCESS,
            "refreshToken": _FAKE_REFRESH,
            "expiresAt": _YEAR_2100_MS,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }))
    os.chmod(path, 0o600)


def _make_record(tmp_path: Path, runtime: str = "apptainer") -> tuple[Path, Path]:
    """Write a real session record; return (record_path, session_dir)."""
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="uuidbroker",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime=runtime,
        image="img",
    )
    (tmp_path / "proj").mkdir(exist_ok=True)
    sr.write(session_dir, sr.from_spec(spec))
    return session_dir / "spec.json", session_dir


def _bind_by_target(binds: list, target: str) -> dict | None:
    return next((b for b in binds if b.get("target") == target), None)


def _run_hook(record_path: Path, session_dir: Path, creds: Path,
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    # Pin PYTHONPATH to the working tree so the hook (and the daemon it spawns,
    # which inherits PYTHONPATH via the hook's safe_inherits) resolve THIS
    # botainer, not any stale site-packages copy. The real launcher forwards
    # PYTHONPATH to hooks (composition.py run_hook env allowlist) the same way.
    pythonpath = os.pathsep.join(
        [str(REPO)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    )
    return subprocess.run(
        [sys.executable, str(HOOK)],
        env={
            **os.environ,
            "PYTHONPATH": pythonpath,
            "BOTAINER_SESSION_RECORD_PATH": str(record_path),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-broker",
            "BOTAINER_HOOK_WHEN": "pre_session",
            "BOTAINER_TESTING": "1",
            "BOTAINER_BROKER_CREDS_PATH_OVERRIDE": str(creds),
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
    )


# ─────────────────────────── manifest ───────────────────────────


def test_manifest_loads_and_is_broker_variant() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(PLUGIN_DIR)
    assert m.name == "agent-claude-broker"
    assert m.tier == "first-party"
    assert m.auth_family == "anthropic"
    assert m.auth_mode == "broker"
    # Exclusive with the mount + dead-proxy variants of the same family.
    assert set(m.mutually_exclusive_with) == {
        "agent-claude", "agent-claude-shared", "agent-claude-proxy"
    }
    # The socket bind envelope must be declared or composition refuses the bind.
    assert "/run/anthropic-broker.sock" in m.contributes.mount_target_prefixes


# ─────────────────────────── hook: refusals ───────────────────────────


def test_hook_refuses_missing_credential(tmp_path: Path) -> None:
    record_path, session_dir = _make_record(tmp_path)
    missing = tmp_path / "nope" / ".credentials.json"
    proc = _run_hook(record_path, session_dir, missing)
    assert proc.returncode == 1
    assert "credential file not found" in proc.stderr


def test_hook_surfaces_specific_reason_on_expired_token(tmp_path: Path) -> None:
    """When the daemon fails closed (expired token, refresh not configured), the
    hook surfaces the daemon's OWN specific reason (captured from its stderr) +
    an actionable login hint — not a generic 'expired OR missing OR sentinel'
    guess — and keeps it under the launcher's 300-char stderr truncation."""
    record_path, session_dir = _make_record(tmp_path, runtime="apptainer")
    creds = tmp_path / "auth" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": _FAKE_ACCESS, "refreshToken": _FAKE_REFRESH,
        "expiresAt": 1000,  # epoch ~1970 → long expired
    }}))
    os.chmod(creds, 0o600)
    sock_dir = tempfile.mkdtemp(prefix="bkt", dir="/tmp")
    try:
        proc = _run_hook(record_path, session_dir, creds,
                         extra_env={"BOTAINER_BROKER_SOCKET_DIR": sock_dir})
        assert proc.returncode == 2
        # Specific reason surfaced (the daemon's own message), plus the fix.
        assert "access token" in proc.stderr and "refresh" in proc.stderr
        assert "botainer auth login" in proc.stderr
        # The one hook stderr line stays under the launcher's 300-char clip.
        line = next(ln for ln in proc.stderr.splitlines()
                    if "broker failed" in ln)
        assert len(line) <= 300
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


# ─────────────────────────── hook: happy path ───────────────────────────


STOP_HOOK = PLUGIN_DIR / "hooks" / "stop_broker.py"


def _run_stop(record_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STOP_HOOK)],
        env={**os.environ, "BOTAINER_SESSION_RECORD_PATH": str(record_path)},
        capture_output=True, text=True,
    )


@pytest.fixture
def spawned(tmp_path: Path):
    """UNIX transport (apptainer): run the hook against a valid fake credential;
    yield the parsed contribution; tear down via the REAL stop_broker hook."""
    record_path, session_dir = _make_record(tmp_path, runtime="apptainer")
    creds = tmp_path / "auth" / ".credentials.json"
    _write_fake_credential(creds)
    # Pin the socket dir to a SHORT path (pytest tmp paths overflow sun_path,
    # and a real run puts the socket on the short state-root run dir).
    sock_dir = tempfile.mkdtemp(prefix="bkt", dir="/tmp")
    proc = _run_hook(record_path, session_dir, creds,
                     extra_env={"BOTAINER_BROKER_SOCKET_DIR": sock_dir})
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        yield contribution, session_dir, record_path
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.fixture
def spawned_tcp(tmp_path: Path):
    """TCP transport (docker): loopback port, no socket bind, sentinel-gated."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / ".credentials.json"
    _write_fake_credential(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        yield contribution, session_dir, record_path
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_hook_provisions_sentinel_not_a_real_token(spawned) -> None:
    contribution, _session_dir, _proc = spawned
    assert contribution["kind"] == "pre_session"
    env = contribution["env"]
    tok = env["ANTHROPIC_AUTH_TOKEN"]
    # The container gets a provably-fake sentinel, NOT a real credential.
    assert is_sentinel(tok), tok
    # UNIX transport: Claude Code speaks HTTP-over-socket via ANTHROPIC_UNIX_SOCKET
    # (a plain path) + an http:// base URL — NOT a dead http+unix:// base URL.
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost"
    assert env["ANTHROPIC_UNIX_SOCKET"] == "/run/anthropic-broker.sock"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    # Persistence: CLAUDE_CONFIG_DIR points at the bound broker-state dir.
    assert env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"


def test_unix_binds_socket_and_state(spawned) -> None:
    contribution, _session_dir, _proc = spawned
    binds = contribution["binds"]
    sock = _bind_by_target(binds, "/run/anthropic-broker.sock")
    state = _bind_by_target(binds, "/home/agent/.claude")
    assert sock is not None and sock["mode"] == "unix-socket"
    # The state dir persists session history and holds NO credential.
    assert state is not None and state["mode"] == "rw"
    assert "broker-state" in state["source"]
    assert len(binds) == 2  # exactly the socket + the state dir, nothing else


def test_tcp_transport_no_bind_and_host_docker_internal(spawned_tcp) -> None:
    contribution, _session_dir, _proc = spawned_tcp
    tok = contribution["env"]["ANTHROPIC_AUTH_TOKEN"]
    assert is_sentinel(tok)
    url = contribution["env"]["ANTHROPIC_BASE_URL"]
    assert url.startswith("http://host.docker.internal:")
    # TCP transport does NOT use the unix-socket env vars.
    assert "ANTHROPIC_UNIX_SOCKET" not in contribution["env"]
    # No socket bind on docker; only the state dir is bound.
    assert _bind_by_target(contribution["binds"], "/run/anthropic-broker.sock") is None
    state = _bind_by_target(contribution["binds"], "/home/agent/.claude")
    assert state is not None and state["mode"] == "rw"


def test_sentinel_contribution_passes_leak_guard(spawned) -> None:
    """THE point of the whole design: the env the broker hands the container
    must pass the credential-leak guard that killed the proxy."""
    contribution, _session_dir, _proc = spawned
    check_env_for_leaks(contribution["env"], source="broker plugin test")  # no raise


def test_no_real_credential_value_in_contribution(spawned) -> None:
    contribution, _session_dir, _proc = spawned
    blob = json.dumps(contribution)
    assert _FAKE_ACCESS not in blob
    assert _FAKE_REFRESH not in blob


def test_daemon_socket_actually_came_up(spawned) -> None:
    contribution, _session_dir, _proc = spawned
    sock = Path(_bind_by_target(contribution["binds"], "/run/anthropic-broker.sock")["source"])
    # The hook waits for the socket before emitting the contribution.
    assert sock.exists()
    # Owner-only: the socket is the identity boundary (topology, not a token).
    assert (sock.stat().st_mode & 0o077) == 0


def test_guard_still_refuses_a_real_token_under_the_same_name() -> None:
    """The sentinel exception must not weaken the guard: a real-looking value
    under ANTHROPIC_AUTH_TOKEN is still refused."""
    with pytest.raises(Refused):
        check_env_for_leaks(
            {"ANTHROPIC_AUTH_TOKEN": _FAKE_ACCESS,
             "ANTHROPIC_BASE_URL": "http+unix:///run/anthropic-broker.sock"},
            source="broker plugin test",
        )


# ─────────────────────── stop hook + socket-path resolver ───────────────────


def test_stop_hook_removes_socket_and_short_dir(spawned) -> None:
    """stop_broker.py must kill the daemon, unlink the socket, and (when the
    socket lived in an overflow botainer-brk-* runtime dir) remove that dir."""
    contribution, _session_dir, record_path = spawned
    sock = Path(contribution["binds"][0]["source"])
    assert sock.exists()
    proc = _run_stop(record_path)
    assert proc.returncode == 0, proc.stderr
    assert not sock.exists()
    # pytest tmp paths overflow sun_path, so the socket is in a brk-* dir here.
    if sock.parent.name.startswith("botainer-brk-"):
        assert not sock.parent.exists()


def _load_hook_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("start_broker_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_trusted_destinations_pinned_when_not_testing() -> None:
    """SECURITY: the credential-destination fields are PINNED and cannot be
    overridden outside testing — a hostile env (or, transitively, a hostile
    project config) must not redirect where the real credential is sent."""
    mod = _load_hook_module()
    hostile = {
        "BOTAINER_BROKER_UPSTREAM_OVERRIDE": "https://evil.example",
        "BOTAINER_BROKER_TOKEN_ENDPOINT_OVERRIDE": "https://evil.example/token",
        "BOTAINER_BROKER_CLIENT_ID_OVERRIDE": "attacker",
    }
    up, tok, cid = mod._trusted_destinations(testing=False, env=hostile)
    assert up == "https://api.anthropic.com"  # pinned, ignores the override
    # Refresh IS enabled via BAKED constants — but the hostile override is
    # ignored: the returned endpoint/client_id are the trusted constants, never
    # the attacker's values.
    assert tok == mod._OAUTH_TOKEN_ENDPOINT and "evil" not in tok
    assert cid == mod._OAUTH_CLIENT_ID and cid != "attacker"


def test_trusted_destinations_override_only_under_testing() -> None:
    mod = _load_hook_module()
    env = {
        "BOTAINER_BROKER_UPSTREAM_OVERRIDE": "http://127.0.0.1:9",
        "BOTAINER_BROKER_TOKEN_ENDPOINT_OVERRIDE": "https://tok.test",
        "BOTAINER_BROKER_CLIENT_ID_OVERRIDE": "cid",
    }
    up, tok, cid = mod._trusted_destinations(testing=True, env=env)
    assert (up, tok, cid) == ("http://127.0.0.1:9", "https://tok.test", "cid")


def test_hostile_project_config_cannot_set_credential_destination(tmp_path: Path) -> None:
    """A project config.yaml that tries to set upstream/token_endpoint is
    ignored: the hook only reads credential_scope from plugin config, and the
    destinations come from _trusted_destinations (pinned)."""
    mod = _load_hook_module()
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "plugins:\n"
        "  agent-claude-broker:\n"
        "    upstream: https://evil.example\n"
        "    token_endpoint: https://evil.example/token\n"
        "    credential_scope: shared\n"
    )
    cfg = mod._read_plugin_config(proj)
    # The config is readable, but the only field the hook consumes from it is
    # credential_scope; upstream/token_endpoint are never wired to the daemon.
    assert cfg.get("credential_scope") == "shared"
    up, tok, _cid = mod._trusted_destinations(testing=False, env=os.environ)
    assert up == "https://api.anthropic.com"
    # The endpoint is the pinned constant, never the config's evil.example.
    assert tok == mod._OAUTH_TOKEN_ENDPOINT and "evil" not in tok


def test_socket_path_resolver_uses_short_state_root_run_dir() -> None:
    mod = _load_hook_module()
    # A short state_root → socket at <root>/run/brk-<sid>.sock (on the shared
    # state filesystem; fits sun_path by skipping the deep sessions/<uuid> path).
    root = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))
    try:
        sock = mod._resolve_socket_path(root, "abcSESS123def456ZZZ")
        assert sock == root / "run" / "brk-abcSESS123def456.sock"  # sid trimmed to 16
        assert (root / "run").exists()
        assert ((root / "run").stat().st_mode & 0o077) == 0  # host-private 0700
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_socket_path_resolver_falls_back_when_state_root_too_long(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_hook_module()
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    # A pathologically deep state_root (e.g. an HPC $SCRATCH) that blows past
    # sun_path even at <root>/run/brk-*.sock → fall back to a short runtime dir.
    long_root = tmp_path / ("x" * 120)
    long_root.mkdir()
    sock = mod._resolve_socket_path(long_root, "sess1234")
    try:
        assert sock.parent.name.startswith("botainer-brk-")
        assert len(str(sock).encode()) <= mod._SUN_PATH_MAX
        assert (sock.parent.stat().st_mode & 0o077) == 0  # host-private 0700
    finally:
        shutil.rmtree(sock.parent, ignore_errors=True)
