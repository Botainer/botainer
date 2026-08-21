"""Test for task #284 / codex P0-1. Verifies plugin hooks do NOT receive
host credential env vars (ANTHROPIC_API_KEY, AWS_*, GITHUB_TOKEN, etc.).
"""

from __future__ import annotations

from pathlib import Path

from botainer.plugins.hooks import run_hook

DENY_LIST = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SSH_AUTH_SOCK",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "SLACK_TOKEN",
    "DB_PASSWORD",
]


def _make_hook_that_dumps_env(tmp_path: Path) -> Path:
    hook = tmp_path / "dump_env.sh"
    out_path = tmp_path / "env.txt"
    hook.write_text(
        f'#!/usr/bin/env bash\n'
        f'env > {out_path}\n'
        f'echo "{{}}"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def test_hook_does_not_receive_credential_env(tmp_path, monkeypatch):
    out_path = tmp_path / "env.txt"
    hook = _make_hook_that_dumps_env(tmp_path)
    for name in DENY_LIST:
        monkeypatch.setenv(name, f"SENSITIVE_VALUE_OF_{name}")
    run_hook(
        plugin_name="testplug",
        hook_when="pre_session",
        script_path=hook,
        env={"BOTAINER_TEST": "yes"},
        agent_writable_roots=[],
        timeout_seconds=10,
    )
    assert out_path.exists()
    env_dump = out_path.read_text(encoding="utf-8")
    leaked = [name for name in DENY_LIST if f"{name}=" in env_dump]
    assert not leaked, (
        f"Hook received {len(leaked)} sensitive env vars: {leaked}. "
        f"Task #284 / codex P0-1 not fully fixed."
    )


def test_hook_still_receives_minimal_execution_env(tmp_path, monkeypatch):
    out_path = tmp_path / "env.txt"
    hook = _make_hook_that_dumps_env(tmp_path)
    run_hook(
        plugin_name="testplug",
        hook_when="pre_session",
        script_path=hook,
        env={"BOTAINER_FOO": "bar"},
        agent_writable_roots=[],
        timeout_seconds=10,
    )
    env_dump = out_path.read_text(encoding="utf-8")
    assert "PATH=" in env_dump
    assert "BOTAINER_FOO=bar" in env_dump
    assert "BOTAINER_HOOK_WHEN=pre_session" in env_dump
    assert "BOTAINER_PLUGIN=testplug" in env_dump


# ───── AUDIT (MEDIUM): hook stdout fails CLOSED on malformed JSON ─────


def _hook_emitting(tmp_path: Path, stdout_body: str) -> Path:
    hook = tmp_path / "emit.sh"
    hook.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{stdout_body}\nEOF\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def test_run_hook_valid_json_parses(tmp_path) -> None:
    hook = _hook_emitting(tmp_path, '{"version":"plugin-contribution-v1","binds":[]}')
    res = run_hook(plugin_name="p", hook_when="pre_session", script_path=hook,
                   env={}, agent_writable_roots=[], timeout_seconds=10)
    assert res.parsed_contribution == {"version": "plugin-contribution-v1", "binds": []}


def test_run_hook_valid_json_after_debug_preamble_still_parses(tmp_path) -> None:
    # A debug line on stdout before the JSON is tolerated (heuristic finds the
    # last '{' line) — must NOT refuse, since a real contribution was produced.
    hook = _hook_emitting(tmp_path, 'debug: starting\n{"binds":[]}')
    res = run_hook(plugin_name="p", hook_when="pre_session", script_path=hook,
                   env={}, agent_writable_roots=[], timeout_seconds=10)
    assert res.parsed_contribution == {"binds": []}


def test_run_hook_empty_stdout_is_no_contribution(tmp_path) -> None:
    # Side-effect-only hook (no stdout) → no contribution, NOT a refusal.
    hook = tmp_path / "noop.sh"
    hook.write_text("#!/usr/bin/env bash\n:\n", encoding="utf-8")
    hook.chmod(0o755)
    res = run_hook(plugin_name="p", hook_when="pre_session", script_path=hook,
                   env={}, agent_writable_roots=[], timeout_seconds=10)
    assert res.parsed_contribution is None


def test_run_hook_malformed_contribution_refuses(tmp_path) -> None:
    """AUDIT (MEDIUM): a contribution corrupted (truncated/BOM/stray
    print) so it doesn't parse — but clearly attempted ('{' present) — must
    REFUSE, not silently drop. A dropped contribution may be a security
    control (git overlay, credential bind) the user believes is active."""
    from botainer.core.refusal import Refused, RefusalCategory
    import pytest as _pytest
    hook = _hook_emitting(tmp_path, '{"binds":[{"source":"/x","target":"/y"  TRUNCATED')
    with _pytest.raises(Refused) as exc:
        run_hook(plugin_name="p", hook_when="pre_session", script_path=hook,
                 env={}, agent_writable_roots=[], timeout_seconds=10)
    assert exc.value.category == RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED


def test_scrubbed_env_propagates_profile_lmod_bootstrap(
    monkeypatch, tmp_path
) -> None:
    """Cluster-ease C2: when the active cluster profile carries
    `lmod.bootstrap` but the operator hasn't exported BOTAINER_LMOD_BOOTSTRAP,
    the profile value flows into the hook env. Makes the YAML field actually
    load-bearing instead of decorative."""
    from botainer.plugins.hooks import _scrubbed_host_env
    from botainer.state import cluster_profile
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.delenv("BOTAINER_LMOD_BOOTSTRAP", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    profile = cluster_profile.ClusterProfile(
        name="testcluster",
        hostname_patterns=("test*.example.com",),
        description="t",
        lmod_bootstrap="/apps/lmod/init/bash",
    )
    cluster_profile.write_user_profile(profile)

    env = _scrubbed_host_env()
    assert env.get("BOTAINER_LMOD_BOOTSTRAP") == "/apps/lmod/init/bash"


def test_scrubbed_env_operator_env_wins_over_profile(
    monkeypatch, tmp_path
) -> None:
    """The operator's explicit env var WINS over the cluster profile's
    `lmod.bootstrap`. Standard operator-override pattern."""
    from botainer.plugins.hooks import _scrubbed_host_env
    from botainer.state import cluster_profile
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.setenv("BOTAINER_LMOD_BOOTSTRAP", "/operator/override")
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    profile = cluster_profile.ClusterProfile(
        name="testcluster",
        hostname_patterns=("test*.example.com",),
        description="t",
        lmod_bootstrap="/apps/lmod/init/bash",
    )
    cluster_profile.write_user_profile(profile)

    env = _scrubbed_host_env()
    assert env.get("BOTAINER_LMOD_BOOTSTRAP") == "/operator/override"


def test_scrubbed_env_no_profile_no_bootstrap(monkeypatch, tmp_path) -> None:
    """No active profile + no env set → BOTAINER_LMOD_BOOTSTRAP stays unset
    and the hook's own _detect_bootstrap path takes over."""
    from botainer.plugins.hooks import _scrubbed_host_env
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.delenv("BOTAINER_LMOD_BOOTSTRAP", raising=False)
    env = _scrubbed_host_env()
    assert "BOTAINER_LMOD_BOOTSTRAP" not in env
