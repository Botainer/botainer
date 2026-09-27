"""agent-codex-broker plugin: manifest + start_broker.py hook.

The OpenAI analog of test_agent_claude_broker_plugin.py. Drives the real
pre_session hook directly, spawns the real broker daemon
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
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; provided by the dev extra.
    import tomli as tomllib

import pytest

from botainer.core.broker_sentinel import is_sentinel
from botainer.core.credential_leak_check import check_env_for_leaks
from botainer.core.refusal import Refused
from tests.unit._broker_install import checked_hook_python

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "agent-codex-broker"
HOOK = PLUGIN_DIR / "hooks" / "start_broker.py"
STOP_HOOK = PLUGIN_DIR / "hooks" / "stop_broker.py"

_FAKE_KEY = "sk-FAKE-codex-broker-test-key-not-real-000000000000000000"


def _write_fake_credential(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"OPENAI_API_KEY": _FAKE_KEY}))
    os.chmod(path, 0o600)


def _make_record(tmp_path: Path, runtime: str = "docker", *,
                 project_uuid: str = "uuidcodexbrk") -> tuple[Path, Path]:
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid=project_uuid,
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
    interpreter = checked_hook_python(REPO)
    return subprocess.run(
        [interpreter, "-I", "-B", str(HOOK)],
        env={
            **os.environ,
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
        [checked_hook_python(REPO), "-I", "-B", str(STOP_HOOK)],
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


def test_a_non_ascii_project_uuid_still_yields_a_RECOGNISED_sentinel(
        tmp_path: Path) -> None:
    """#216, through the REAL hook rather than through make_sentinel directly.

    The tenant half of the sentinel comes from the project uuid, and SessionSpec
    accepts a non-ASCII one — so this path is reachable, not hypothetical. The
    hooks used to filter it with `c.isalnum()`, which is Unicode-aware, while
    `is_sentinel`'s regex is ASCII-only; the hook then emitted a value that
    every downstream consumer refused to recognise.

    Both consequences are asserted here because they point OPPOSITE ways:

      * `check_env_for_leaks` allows a recognised sentinel. Unrecognised, it
        refuses the launch for leaking a value that carries no secret at all.
      * The broker's credential sources refuse to forward a recognised
        sentinel upstream. Unrecognised, that guard silently stops guarding.

    Asserting only the first would have passed on a half-fix.
    """
    record_path, session_dir = _make_record(
        tmp_path, runtime="docker", project_uuid="uuidⅧ٣ｆ-x")
    creds = tmp_path / "auth" / "auth.json"
    _write_fake_credential(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        key = contribution["env"]["OPENAI_API_KEY"]
        assert is_sentinel(key), (
            f"the hook built a sentinel nothing downstream recognises: {key!r}")
        check_env_for_leaks(contribution["env"], source="codex broker test")
        # And the value the container holds is still ASCII, so it survives every
        # env-var transport between here and the container unchanged.
        key.encode("ascii")
    finally:
        _run_stop(record_path)


def _planned_broker_state_dir(session_dir: Path) -> Path:
    """Where the hook WILL bind /home/agent/.codex from — needed before the hook
    runs, so a hostile path can be planted there. Every use below cross-checks
    it against the bind the hook actually emitted (_broker_state_dir), so a
    layout change fails loudly instead of quietly planting somewhere harmless
    and passing."""
    return (session_dir.parent.parent / "data" / "agent-codex"
            / "broker-state" / "default")


def test_the_agent_cannot_brick_broker_mode_with_a_directory(
        tmp_path: Path) -> None:
    """#216. broker-state is bound rw at /home/agent/.codex, so a caged agent
    can `mkdir /home/agent/.codex/auth.json`. The stale-stub cleanup used a bare
    unlink(), which raises IsADirectoryError on that — the hook returned 2 and
    broker mode for the project stayed dead until a human diagnosed it, while
    the error told them to delete a "file" that was not one.

    A denial of service the container can inflict on its OWN future sessions.
    Observed here, not reasoned about: the directory is really created, the real
    hook is really run, and the directory is really gone afterwards."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / "auth.json"
    _write_fake_credential(creds)

    hostile = _planned_broker_state_dir(session_dir) / "auth.json"
    hostile.mkdir(parents=True)
    (hostile / "planted").write_text("the agent put this here")
    assert hostile.is_dir()                      # the precondition really holds

    proc = _run_hook(record_path, session_dir, creds)
    try:
        assert proc.returncode == 0, (
            f"a directory at {hostile} still bricks the broker: {proc.stderr}")
        contribution = json.loads(proc.stdout)
        assert _broker_state_dir(contribution) == hostile.parent, (
            "the test planted its directory somewhere the hook does not bind; "
            "this test would pass without checking anything")
        assert not hostile.exists(), "the hostile directory survived"
        # And the session it produced is a real one, not a degraded fallback.
        assert is_sentinel(contribution["env"]["OPENAI_API_KEY"])
    finally:
        _run_stop(record_path)


@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_a_symlinked_auth_json_is_removed_without_touching_its_target(
        tmp_path: Path, target_kind: str) -> None:
    """The other shapes the agent can choose.

    BOTH kinds of target, because they are not the same test. A symlink to a
    FILE behaves identically whether the cleanup stats or lstats, so it proves
    nothing about which one is used. A symlink to a DIRECTORY is where they
    diverge: under stat() the entry looks like a directory, shutil.rmtree
    refuses to act on a symlink, the hook returns 2 — and broker mode is bricked
    again by the very code meant to stop that. Under lstat() it is not a
    directory, the link itself is removed, and the target is untouched.

    Verified by running the two variants side by side before writing this, not
    by reasoning about rmtree's documented behaviour.
    """
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / "auth.json"
    _write_fake_credential(creds)

    target = tmp_path / "precious"
    if target_kind == "directory":
        target.mkdir()
        (target / "inside").write_text("must survive")
    else:
        target.write_text("must survive")

    link = _planned_broker_state_dir(session_dir) / "auth.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    proc = _run_hook(record_path, session_dir, creds)
    try:
        assert proc.returncode == 0, (
            f"a symlink to a {target_kind} bricks the broker: {proc.stderr}")
        assert _broker_state_dir(json.loads(proc.stdout)) == link.parent, (
            "the test planted its symlink somewhere the hook does not bind")
        assert not link.is_symlink(), "the symlink survived"
        assert target.exists(), (
            "the cleanup followed the symlink and destroyed its target")
        if target_kind == "directory":
            assert (target / "inside").read_text() == "must survive"
        else:
            assert target.read_text() == "must survive"
    finally:
        _run_stop(record_path)


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


# ───────────── routing: the config.toml is what points codex at us ─────────────
#
# #121. Every layer of this plugin worked except the one that mattered: the
# daemon started, held the real credential, and was NEVER CONTACTED. Codex
# 0.153.2 does not follow `OPENAI_BASE_URL` for its model calls — measured
# against a logging server on 2026-09-04, it went to chatgpt.com (subscription)
# or wss://api.openai.com (api-key) and sent our sentinel to OpenAI, which
# answered `401 "Could not parse your authentication token"`.
#
# These tests pin the delivery mechanism that DOES route, because the failure
# mode is silent from the host's side: the hook exits 0, the daemon is healthy,
# the contribution looks right, and the session still cannot authenticate. The
# only local evidence is the file itself.


def _broker_state_dir(contribution) -> Path:
    return Path(_bind_by_target(contribution["binds"], "/home/agent/.codex")["source"])


def _provider_block(cfg: str) -> dict:
    """Parse the [model_providers.botainer-broker] table."""
    return tomllib.loads(cfg)["model_providers"]["botainer-broker"]


def test_config_toml_points_codex_at_the_broker(spawned) -> None:
    contribution, _session_dir, _rp = spawned
    cfg = (_broker_state_dir(contribution) / "config.toml").read_text()
    doc = tomllib.loads(cfg)
    assert doc["model_provider"] == "botainer-broker"      # selected, not merely defined
    prov = _provider_block(cfg)
    # The provider base_url must be THE BROKER — same endpoint the contribution
    # advertises. A drift here routes the session to OpenAI with a sentinel.
    assert prov["base_url"] == contribution["env"]["OPENAI_BASE_URL"]
    assert prov["wire_api"] == "responses"
    # codex sends this env var as the Bearer; it holds the sentinel, which is
    # also the daemon's required token.
    assert prov["env_key"] == "OPENAI_API_KEY"
    # This is what stops codex demanding a ChatGPT login for a provider that
    # does not need one — the "asks me to log in" symptom.
    assert prov["requires_openai_auth"] is False
    # The WebSocket transport ignores the configured base URL, which is how
    # api-key mode reached wss://api.openai.com despite OPENAI_BASE_URL.
    assert prov["supports_websockets"] is False


def test_subscription_mode_writes_the_same_provider_config(tmp_path: Path) -> None:
    """A ChatGPT login and an API key look IDENTICAL inside the container: one
    plain provider, one key. Only the host-side upstream differs."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / "auth.json"
    _oauth_auth_json(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        prov = _provider_block(
            (_broker_state_dir(contribution) / "config.toml").read_text())
        assert prov["base_url"] == contribution["env"]["OPENAI_BASE_URL"]
        assert prov["base_url"].endswith("/backend-api/codex")
        assert prov["env_key"] == "OPENAI_API_KEY"
        assert prov["requires_openai_auth"] is False
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_no_auth_json_reaches_the_container_in_subscription_mode(
        tmp_path: Path) -> None:
    """botainer used to write a stub auth.json here so codex would believe it was
    logged in. That put codex in ChatGPT mode, where it talks to chatgpt.com
    DIRECTLY and the broker is bypassed. Nothing may write one again."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    creds = tmp_path / "auth" / "auth.json"
    _oauth_auth_json(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        assert not (_broker_state_dir(contribution) / "auth.json").exists()
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_a_stale_stub_auth_json_is_removed(tmp_path: Path) -> None:
    """The broker-state dir OUTLIVES the session, so a stub written by an older
    botainer is still sitting there on upgrade — and would send codex back to
    chatgpt.com with a sentinel. Upgrading must fix it, not just stop causing it."""
    record_path, session_dir = _make_record(tmp_path, runtime="docker")
    stale = (session_dir.parent.parent / "data" / "agent-codex" / "broker-state"
             / "default" / "auth.json")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"tokens": {"access_token": "stale-stub"}}))
    creds = tmp_path / "auth" / "auth.json"
    _write_fake_credential(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        assert not stale.exists(), "an upgrade left the bypassing stub in place"
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_broker_endpoint_stays_visible_to_the_cross_node_guard(
        tmp_path: Path) -> None:
    """THE COUPLING THAT IS INVISIBLE AT BOTH ENDS.

    Routing moved into config.toml, and it would have been tidy to drop
    `OPENAI_BASE_URL` from the contribution as no-longer-load-bearing. It is
    load-bearing, for something else entirely: check 3 of
    `_refuse_cross_node_binds` scans spec ENV for a loopback rendezvous, and it
    is the only check that can see this plugin at all (TCP transport, no bind).
    Without it `hpc submit` would bake a login-node 127.0.0.1 into an sbatch
    script and the job would die on a compute node hours later, after the queue
    wait and the allocation are spent.

    Neither file mentions the other, so this test is the link. It drives the
    REAL hook and the REAL guard rather than asserting on a string.
    """
    from botainer.core import composition
    from botainer.core.refusal import Refused as _Refused
    from botainer.core.spec import (EnvSpec, MountPlan, NetworkMode, NetworkSpec,
                                    SessionSpec)

    record_path, session_dir = _make_record(tmp_path, runtime="apptainer")
    creds = tmp_path / "auth" / "auth.json"
    _write_fake_credential(creds)
    proc = _run_hook(record_path, session_dir, creds)
    assert proc.returncode == 0, proc.stderr
    contribution = json.loads(proc.stdout)
    try:
        spec = SessionSpec(
            session_id="ses-x", project_uuid="u" * 32,
            project_root=str(tmp_path / "proj"), image="test:0.1",
            runtime="apptainer", state_dir=str(session_dir.parent.parent),
            plugins_enabled=("agent-codex-broker",),
            env=EnvSpec(values=dict(contribution["env"])),
            mount_plan=MountPlan(),
            network=NetworkSpec(mode=NetworkMode.INTERNET),
        )
        with pytest.raises(_Refused) as exc:
            composition._refuse_cross_node_binds(spec)
        assert "loopback" in str(exc.value).lower()
    finally:
        _run_stop(record_path)
        pid = contribution.get("broker_pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


# ───────────── the broker's own files must name the broker ─────────────


def test_the_daemon_files_carry_the_plugin_name(spawned) -> None:
    """An unattributable broker error file must not be constructible.

    Broker log filenames include the plugin name so errors can be attributed
    when multiple agent plugins use the same session directory. Diagnostics
    should point to a file that actually exists.

    Drives the REAL hook and looks at what it actually created.
    """
    _contribution, session_dir, _rp = spawned
    names = {p.name for p in session_dir.iterdir()}
    assert "agent-codex-broker-daemon.err" in names, sorted(names)
    assert "broker-daemon.err" not in names, (
        "the un-attributed name is back; two brokers in one session would "
        "collide on it again")


def test_the_two_brokers_cannot_collide_on_a_session_filename() -> None:
    """The sibling half: codex and claude must not pick the same names.

    A convention held in two files by memory is the drift shape this repo keeps
    recording (#136). Extracting the literals and asserting they are disjoint
    is the cheapest guard that fails when someone re-unifies them.
    """
    import re
    lits = {}
    for fam in ("codex", "claude"):
        src = (REPO / "plugins" / f"agent-{fam}-broker" / "hooks"
               / "start_broker.py").read_text(encoding="utf-8")
        lits[fam] = set(re.findall(
            r'session_dir\s*/\s*"([^"]+)"', src))
        assert lits[fam], f"no session_dir filenames found for {fam}"
    overlap = lits["codex"] & lits["claude"]
    assert not overlap, (
        f"both brokers write these names into one session dir: {sorted(overlap)}")
    for fam, names in lits.items():
        for n in names:
            assert f"agent-{fam}-broker" in n, (
                f"{fam} writes {n!r}, which does not name the plugin")


# ── the spawn-time pollution warning, three weeks after claude got it ──────

def test_a_polluted_broker_state_dir_is_CALLED_OUT(tmp_path: Path) -> None:
    """SIBLING DRIFT, closed. `ba8703d` warned on the claude side only.

    For three weeks the codex broker bound a polluted state dir in total
    silence — while the comment already in this plugin said "broker_state_dir
    is bound rw at /home/agent/.codex, so a caged agent can `mkdir
    /home/agent/.codex/auth.json`". The hazard was documented here and
    unguarded here, which is the shape row #136 is about.

    A WARNING, not a refusal, for the same reason as the claude side: the
    pollution can arrive by routes the user did not choose, refusing mid-launch
    would strand them, and a refusal has to name a remedy that exists.
    """
    record_path, session_dir = _make_record(tmp_path, runtime="apptainer")
    planned = _planned_broker_state_dir(session_dir)
    planned.mkdir(parents=True, exist_ok=True)
    planted = planned / "auth.json"
    planted.write_text('{"tokens": {"refresh_token": "PLANTED-NOT-REAL"}}')
    planted.chmod(0o600)

    creds = tmp_path / "auth" / "api_key"
    _write_fake_credential(creds)
    proc = _run_hook(record_path, session_dir, creds)
    try:
        assert "auth.json" in proc.stderr, (
            f"the hook bound a broker-state dir holding a credential and said "
            f"nothing about it:\n{proc.stderr}")
        assert "NO credential" in proc.stderr, proc.stderr
    finally:
        _run_stop(record_path)


def test_a_clean_broker_state_dir_says_NOTHING(tmp_path: Path) -> None:
    """THE CONTROL, and it is written knowing how the claude one failed.

    That one asserted on the bind's source and never read stderr, so mutating
    `if _leaked:` to `if True:` — warning on every healthy launch, which IS the
    scenery this test is named after — left it and 205 other tests green. It
    halted the loop. This one runs the hook and reads what it said, and the
    same mutation fails it by name.

    Hook silence on success is load-bearing here: `surface_hook_stderr` shows
    this channel to the user, and a warning on every launch is how the next
    real one gets read past.
    """
    record_path, session_dir = _make_record(tmp_path, runtime="apptainer")
    creds = tmp_path / "auth" / "api_key"
    _write_fake_credential(creds)

    proc = _run_hook(record_path, session_dir, creds)
    try:
        assert "WARNING" not in proc.stderr, (
            f"the hook warned on a CLEAN broker-state dir:\n{proc.stderr}")
        assert "NO credential" not in proc.stderr, proc.stderr
    finally:
        _run_stop(record_path)
