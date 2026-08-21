"""agent-codex-broker plugin: manifest + start_broker.py hook.

The OpenAI analog of test_agent_claude_broker_plugin.py. Drives the real
pre_session hook the way the launcher does, spawns the real broker daemon
(provider=openai), and asserts the two correctness properties:

  1. The container is handed a provably-fake SENTINEL as OPENAI_API_KEY (not the
     real key), and that env PASSES botainer's credential-leak guard.
  2. No value from the real credential file appears in the contribution.

TCP-only transport (codex has no unix socket), so the daemon binds a loopback
port gated by the sentinel; the startup probe reads the fake key from disk
(no network), so these tests are hermetic.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from botainer.core.broker_sentinel import is_sentinel
from botainer.core.credential_leak_check import check_env_for_leaks
from botainer.core.refusal import Refused

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "agent-codex-broker"
HOOK = PLUGIN_DIR / "hooks" / "start_broker.py"
STOP_HOOK = PLUGIN_DIR / "hooks" / "stop_broker.py"

_FAKE_KEY = "sk-FAKE-codex-broker-test-key-not-real-000000000000000000"


def _write_fake_credential(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"OPENAI_API_KEY": _FAKE_KEY}))
    os.chmod(path, 0o600)


def _make_record(tmp_path: Path, runtime: str = "docker") -> tuple[Path, Path]:
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="uuidcodexbrk",
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
            "BOTAINER_PLUGIN": "agent-codex-broker",
            "BOTAINER_HOOK_WHEN": "pre_session",
            "BOTAINER_TESTING": "1",
            "BOTAINER_BROKER_CREDS_PATH_OVERRIDE": str(creds),
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
    )


def _run_stop(record_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STOP_HOOK)],
        env={**os.environ, "BOTAINER_SESSION_RECORD_PATH": str(record_path)},
        capture_output=True, text=True,
    )


# ─────────────────────────── manifest ───────────────────────────


def test_manifest_loads_and_is_broker_variant() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(PLUGIN_DIR)
    assert m.name == "agent-codex-broker"
    assert m.tier == "first-party"
    assert m.auth_family == "openai"
    assert m.auth_mode == "broker"
    assert set(m.mutually_exclusive_with) == {"agent-codex", "agent-codex-shared"}
    # The state-dir bind envelope must be declared or composition refuses the bind.
    assert "/home/agent/.codex" in m.contributes.mount_target_prefixes


# ─────────────────────────── hook: refusals ───────────────────────────


def test_hook_refuses_missing_credential(tmp_path: Path) -> None:
    record_path, session_dir = _make_record(tmp_path)
    missing = tmp_path / "nope" / "api_key"
    proc = _run_hook(record_path, session_dir, missing)
    assert proc.returncode == 1
    assert "credential file not found" in proc.stderr


# ─────────────────────────── hook: happy path ───────────────────────────


@pytest.fixture
def spawned(tmp_path: Path):
    """Run the hook against a valid fake key; yield the parsed contribution;
    tear down via the REAL stop_broker hook."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / "auth.json"
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


def test_hook_provisions_sentinel_not_a_real_key(spawned) -> None:
    contribution, _session_dir, _rp = spawned
    assert contribution["kind"] == "pre_session"
    env = contribution["env"]
    key = env["OPENAI_API_KEY"]
    assert is_sentinel(key), key            # fake sentinel, not the real key
    # Codex base URL is TCP loopback (docker → host.docker.internal) with /v1.
    url = env["OPENAI_BASE_URL"]
    assert url.startswith("http://host.docker.internal:") and url.endswith("/v1")
    assert env["CODEX_HOME"] == "/home/agent/.codex"


def test_apptainer_base_url_is_loopback(tmp_path: Path) -> None:
    """Under apptainer (shared host netns), the reachable host is 127.0.0.1, not
    host.docker.internal."""
    record_path, session_dir = _make_record(tmp_path, runtime="apptainer")
    creds = tmp_path / "auth" / "auth.json"
    _write_fake_credential(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        url = contribution["env"]["OPENAI_BASE_URL"]
        assert url.startswith("http://127.0.0.1:") and url.endswith("/v1")
    finally:
        _run_stop(record_path)


def test_only_state_dir_is_bound(spawned) -> None:
    contribution, _session_dir, _rp = spawned
    binds = contribution["binds"]
    state = _bind_by_target(binds, "/home/agent/.codex")
    assert state is not None and state["mode"] == "rw"
    assert "broker-state" in state["source"]
    assert len(binds) == 1  # no socket bind (TCP transport)


def test_sentinel_contribution_passes_leak_guard(spawned) -> None:
    contribution, _session_dir, _rp = spawned
    check_env_for_leaks(contribution["env"], source="codex broker test")  # no raise


def test_no_real_key_value_in_contribution(spawned) -> None:
    contribution, _session_dir, _rp = spawned
    assert _FAKE_KEY not in json.dumps(contribution)


def test_guard_refuses_a_real_key_under_openai_api_key() -> None:
    """The sentinel exception must not weaken the guard: a real-looking key under
    OPENAI_API_KEY is still refused."""
    with pytest.raises(Refused):
        check_env_for_leaks(
            {"OPENAI_API_KEY": _FAKE_KEY,
             "OPENAI_BASE_URL": "http://host.docker.internal:9/v1"},
            source="codex broker test",
        )


# ─────────────────────────── trust: pinned upstream ───────────────────────────


def _load_hook_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_start_broker_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_upstream_pinned_when_not_testing() -> None:
    """A hostile env (or, transitively, a hostile project config) must not
    redirect where the real credential is sent — for EITHER mode."""
    mod = _load_hook_module()
    hostile = {"BOTAINER_BROKER_UPSTREAM_OVERRIDE": "https://evil.example"}
    assert mod._trusted_upstream("api-key", testing=False, env=hostile) \
        == "https://api.openai.com"
    assert mod._trusted_upstream("subscription", testing=False, env=hostile) \
        == "https://chatgpt.com"


def test_hostile_project_config_cannot_set_upstream(tmp_path: Path) -> None:
    mod = _load_hook_module()
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "plugins:\n"
        "  agent-codex-broker:\n"
        "    upstream: https://evil.example\n"
        "    credential_scope: shared\n"
    )
    cfg = mod._read_plugin_config(proj)
    assert cfg.get("credential_scope") == "shared"       # only field consumed
    assert mod._trusted_upstream("api-key", testing=False, env=os.environ) \
        == "https://api.openai.com"


# ─────────────────────── subscription (ChatGPT OAuth) mode ───────────────────────

import base64  # noqa: E402
import time  # noqa: E402


def _oauth_auth_json(path: Path) -> None:
    """A fresh ChatGPT-OAuth codex login (tokens block, non-expired access)."""
    def jwt(claims):
        h = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"{h}.{p}.sig"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}}),
            "access_token": jwt({"exp": int(time.time()) + 3600}),
            "refresh_token": "rt-fake-test", "account_id": "acct-1"},
        "last_refresh": "2026-07-08T00:00:00Z"}))
    os.chmod(path, 0o600)


def test_detect_mode(tmp_path: Path) -> None:
    mod = _load_hook_module()
    apik = tmp_path / "api_key"
    apik.write_text("sk-x")
    assert mod._detect_mode(apik) == "api-key"
    aj = tmp_path / "auth.json"
    aj.write_text(json.dumps({"OPENAI_API_KEY": "sk-x"}))
    assert mod._detect_mode(aj) == "api-key"
    _oauth_auth_json(tmp_path / "oauth.json")
    assert mod._detect_mode(tmp_path / "oauth.json") == "subscription"


def test_subscription_hook_uses_chatgpt_backend(tmp_path: Path) -> None:
    """A ChatGPT-OAuth login → the container is pointed at the /backend-api/codex
    base (not /v1), the daemon runs in openai-chatgpt mode, and the container
    still gets only a sentinel — no OAuth token leaks into the contribution."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / "auth.json"
    _oauth_auth_json(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        env = contribution["env"]
        assert is_sentinel(env["OPENAI_API_KEY"])
        url = env["OPENAI_BASE_URL"]
        assert url.startswith("http://host.docker.internal:")
        assert url.endswith("/backend-api/codex")   # NOT /v1
        # the record records the subscription provider
        rec = json.loads(record_path.read_text())
        assert rec["runtime_handle"]["broker"]["provider"] == "openai-chatgpt"
        # no real token from the OAuth bundle leaked
        assert "rt-fake-test" not in json.dumps(contribution)
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
