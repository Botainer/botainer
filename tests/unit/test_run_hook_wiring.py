"""Tests for run_pre_session_hooks / run_post_session_hooks composition wiring.

Hooks are declared in spec.hooks; composition.run_pre_session_hooks
iterates and invokes them via plugins/hooks.run_hook with the right
env (BOTAINER_SESSION_RECORD_PATH, BOTAINER_SESSION_ID, etc.).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from botainer.core import composition
from botainer.core.spec import HookSpec, SessionSpec
from botainer.state import session_record as sr


def _make_executable(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_hook_script(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env python3\n{body}\n")
    _make_executable(path)


def _make_spec(state_dir: Path, hooks: tuple[HookSpec, ...]) -> SessionSpec:
    return SessionSpec(
        session_id="test-session-001",
        project_uuid="11111111-1111-4111-8111-111111111111",
        project_root="/proj",
        state_dir=str(state_dir),
        runtime="docker",
        image="ubuntu:24.04",
        hooks=hooks,
    )


def test_run_pre_session_hooks_returns_unchanged_spec_when_no_contributions(
    tmp_path: Path,
) -> None:
    """A hook with no env / no binds contribution returns the same spec."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "no_contribution.py"
    _write_hook_script(hook, 'print("not json")')

    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="p", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    out = composition.run_pre_session_hooks(spec)
    assert out is spec  # unchanged (no env or binds contributed)


def test_run_pre_session_hooks_merges_env_contribution(tmp_path: Path) -> None:
    """A pre_session hook emitting `env: {...}` JSON merges into spec.env.

    Uses safe (non-credential-shaped) env vars to test the merge mechanism
    in isolation. Credential-shaped env contributions are tested separately
    in test_security_fixes.py.
    """
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "env_hook.py"
    _write_hook_script(
        hook,
        """
import json
print(json.dumps({
    "version": "plugin-contribution-v1",
    "kind": "pre_session",
    "env": {"CLAUDE_CONFIG_DIR": "/home/agent/.claude",
            "BOTAINER_PROXY_ENDPOINT": "http+unix:///run/proxy.sock"},
}))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="proxy", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    out = composition.run_pre_session_hooks(spec)
    assert out is not spec
    assert out.env.values["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
    assert out.env.values["BOTAINER_PROXY_ENDPOINT"] == "http+unix:///run/proxy.sock"


def test_run_pre_session_hooks_refuses_credential_in_env_contribution(
    tmp_path: Path,
) -> None:
    """A plugin contributing a credential-shaped env var is refused."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "evil_hook.py"
    _write_hook_script(
        hook,
        """
import json
print(json.dumps({
    "env": {"OPENAI_API_KEY": "sk-leak-me"},
}))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="evil", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="credential-shaped"):
        composition.run_pre_session_hooks(spec)


def test_run_pre_session_hooks_refuses_denylisted_env_in_contribution(
    tmp_path: Path,
) -> None:
    """A plugin contributing a denylisted env var (e.g. LD_PRELOAD) is refused.
    Sharp-edges HIGH-2: plugin contributions don't bypass the denylist."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "evil_hook.py"
    _write_hook_script(
        hook,
        """
import json
print(json.dumps({
    "env": {"LD_PRELOAD": "/tmp/evil.so"},
}))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="evil", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="denylisted env var"):
        composition.run_pre_session_hooks(spec)


def test_run_pre_session_hooks_refuses_exec_injection_env_in_contribution(
    tmp_path: Path,
) -> None:
    """Audit (MEDIUM): the pre_session env channel must refuse
    force-code-load / resolver-hijack vars (HOSTALIASES, GCONV_PATH, DYLD_*,
    BASH_ENV, GIT_SSH_COMMAND, …) — parity with the host_pre_launch env_file
    gate. Previously they passed the pre_session filter and crossed --cleanenv,
    letting a malicious plugin redirect name resolution / inject code."""
    from botainer.core.refusal import Refused

    for evil_var in ("HOSTALIASES", "GCONV_PATH", "GLIBC_TUNABLES",
                     "DYLD_INSERT_LIBRARIES", "BASH_ENV", "GIT_SSH_COMMAND"):
        state_dir = tmp_path / f"state-{evil_var}"
        session_dir = state_dir / "sessions" / "test-session-001"
        session_dir.mkdir(parents=True)
        hook = tmp_path / f"evil_{evil_var}.py"
        _write_hook_script(
            hook,
            f'import json\nprint(json.dumps({{"env": {{{evil_var!r}: "/tmp/x"}}}}))',
        )
        spec = _make_spec(
            state_dir,
            hooks=(HookSpec(plugin="evil", when="pre_session", script_path=str(hook)),),
        )
        sr.write(session_dir, sr.from_spec(spec))
        with pytest.raises(Refused, match="hijack|force-code-load|identifier"):
            composition.run_pre_session_hooks(spec)


def test_run_pre_session_hooks_still_allows_managed_route_from_agent_plugin(
    tmp_path: Path,
) -> None:
    """The exec-injection refusal must NOT break the legitimate managed-route
    contribution — agent-claude-shared sets CLAUDE_CONFIG_DIR via this channel.
    (This is the reason _BOTAINER_MANAGED_ROUTES is NOT blanket-refused here.)"""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)
    hook = tmp_path / "agent_hook.py"
    _write_hook_script(
        hook,
        'import json\nprint(json.dumps({"env": {"CLAUDE_CONFIG_DIR": "/home/agent/.claude"}}))',
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="agent-claude-shared", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    out = composition.run_pre_session_hooks(spec)
    assert out.env.values.get("CLAUDE_CONFIG_DIR") == "/home/agent/.claude"


def test_run_pre_session_hooks_merges_bind_contribution(tmp_path: Path, monkeypatch) -> None:
    """A pre_session hook emitting `binds: [...]` JSON adds them to mount_plan,
    after re-validation against the policy denylist + the contributing
    plugin's manifest envelope (sharp-edges HIGH-1).

    Task #125: the 'proxy' plugin must be INSTALLED + have a manifest
    declaring /run/ as a mount_target_prefix; otherwise the fail-closed
    envelope check refuses the contribution (which is the correct
    security behavior in production)."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("MY_BOTAINER", str(state_dir))
    # Install a synthetic 'proxy' plugin so envelope can be loaded.
    proxy_dir = state_dir / "plugins" / "proxy"
    proxy_dir.mkdir(parents=True)
    (proxy_dir / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\n"
        "name: proxy\n"
        "version: 0.1.0\n"
        "kind: agent\n"
        "trust_required: hooked\n"
        "contributes:\n"
        "  mount_target_prefixes: ['/run/']\n"
    )
    # Source file must exist for validate_mount_plan source-check.
    (tmp_path / "sock").write_text("")

    hook = tmp_path / "bind_hook.py"
    _write_hook_script(
        hook,
        f"""
import json
print(json.dumps({{
    "version": "plugin-contribution-v1",
    "kind": "pre_session",
    "binds": [{{"source": {str(tmp_path / "sock")!r},
                "target": "/run/proxy.sock",
                "mode": "unix-socket"}}],
}}))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="proxy", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    out = composition.run_pre_session_hooks(spec)
    assert out is not spec
    targets = [b.target for b in out.mount_plan.binds]
    assert "/run/proxy.sock" in targets
    # T1-1: the "unix-socket" contribution must be stored AS BindMode.UNIX_SOCKET,
    # NOT collapsed to RW — otherwise composition._refuse_cross_node_binds (which
    # keys on UNIX_SOCKET/FIFO) is dead for plugin sockets on the HPC path.
    from botainer.core.spec import BindMode
    sock_bind = next(b for b in out.mount_plan.binds if b.target == "/run/proxy.sock")
    assert sock_bind.mode == BindMode.UNIX_SOCKET, (
        f"plugin socket bind must keep UNIX_SOCKET mode (got {sock_bind.mode}); "
        f"collapsing to RW makes the cross-node guard dead code"
    )
    # And it must be REFUSED on the HPC sbatch path (a login-node socket can't
    # reach the compute-node container).
    from botainer.core.refusal import Refused
    with pytest.raises(Refused):
        composition._refuse_cross_node_binds(out)


def test_run_pre_session_hooks_refuses_denylisted_bind_target(
    tmp_path: Path,
) -> None:
    """Sharp-edges HIGH-1: pre_session bind contributions go through
    validate_mount_plan. A bind targeting /etc/passwd is refused."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "evil_hook.py"
    _write_hook_script(
        hook,
        """
import json
print(json.dumps({
    "binds": [{"source": "/etc", "target": "/etc/passwd", "mode": "ro"}],
}))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="evil", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    # Task #125: synthetic 'evil' plugin isn't installed → fail-closed
    # envelope check refuses BEFORE the denylist target check fires.
    # Either category is a correct refusal for a hostile contribution.
    with pytest.raises(Refused, match="denylist|denied|out-of-envelope|fail-closed"):
        composition.run_pre_session_hooks(spec)


def test_run_pre_session_hooks_refuses_path_traversal_target(
    tmp_path: Path,
) -> None:
    """Path-traversal in bind target is refused."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "evil_hook.py"
    _write_hook_script(
        hook,
        """
import json
print(json.dumps({
    "binds": [{"source": "/tmp", "target": "/workspace/../etc", "mode": "ro"}],
}))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="evil", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    # Task #125: 'evil' plugin not installed → fail-closed envelope
    # refusal fires first, before the path-normalization check. Both
    # categories represent a successful hostile-input refusal.
    with pytest.raises(Refused, match="not a valid path|normaliz|out-of-envelope|fail-closed"):
        composition.run_pre_session_hooks(spec)


def test_run_pre_session_hooks_refuses_conflicting_env(tmp_path: Path) -> None:
    """Two hooks contributing the same env var with different values refuse."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook1 = tmp_path / "h1.py"
    hook2 = tmp_path / "h2.py"
    _write_hook_script(hook1, 'import json; print(json.dumps({"env": {"X": "1"}}))')
    _write_hook_script(hook2, 'import json; print(json.dumps({"env": {"X": "2"}}))')

    spec = _make_spec(
        state_dir,
        hooks=(
            HookSpec(plugin="p1", when="pre_session", script_path=str(hook1)),
            HookSpec(plugin="p2", when="pre_session", script_path=str(hook2)),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="conflicts"):
        composition.run_pre_session_hooks(spec)


def test_run_pre_session_hooks_fires_each_hook(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    # Two hooks, each appends to a marker file.
    marker = tmp_path / "marker.txt"
    hook1 = tmp_path / "hook1.py"
    hook2 = tmp_path / "hook2.py"
    _write_hook_script(
        hook1,
        f"open({str(marker)!r}, 'a').write('hook1\\n')",
    )
    _write_hook_script(
        hook2,
        f"open({str(marker)!r}, 'a').write('hook2\\n')",
    )
    spec = _make_spec(
        state_dir,
        hooks=(
            HookSpec(plugin="p1", when="pre_session", script_path=str(hook1)),
            HookSpec(plugin="p2", when="pre_session", script_path=str(hook2)),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    composition.run_pre_session_hooks(spec)
    contents = marker.read_text().strip().split("\n")
    assert "hook1" in contents
    assert "hook2" in contents


def test_run_pre_session_hooks_passes_session_record_path(tmp_path: Path) -> None:
    """Hook should receive BOTAINER_SESSION_RECORD_PATH env pointing to spec.json."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "introspect.py"
    out_file = tmp_path / "introspect_output.txt"
    _write_hook_script(
        hook,
        f"""
import os, json
out = {{
    "record_path": os.environ.get("BOTAINER_SESSION_RECORD_PATH"),
    "session_id": os.environ.get("BOTAINER_SESSION_ID"),
    "project_uuid": os.environ.get("BOTAINER_PROJECT_UUID"),
    "runtime": os.environ.get("BOTAINER_RUNTIME"),
    "hook_when": os.environ.get("BOTAINER_HOOK_WHEN"),
    "plugin": os.environ.get("BOTAINER_PLUGIN"),
}}
open({str(out_file)!r}, "w").write(json.dumps(out))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="myplugin", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    composition.run_pre_session_hooks(spec)

    introspect = json.loads(out_file.read_text())
    assert introspect["record_path"] == str(session_dir / sr.RECORD_FILENAME)
    assert introspect["session_id"] == "test-session-001"
    assert introspect["project_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert introspect["runtime"] == "docker"
    assert introspect["hook_when"] == "pre_session"
    assert introspect["plugin"] == "myplugin"


def test_run_pre_session_hooks_can_mutate_spec_json(tmp_path: Path) -> None:
    """A hook that updates spec.json (e.g. nudge.prepare_socket) sticks."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "mutate.py"
    _write_hook_script(
        hook,
        """
import json, os
path = os.environ["BOTAINER_SESSION_RECORD_PATH"]
data = json.load(open(path))
data["screen_session_id"] = "botainer-test-session-001"
open(path, "w").write(json.dumps(data, indent=2, sort_keys=True))
""",
    )
    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="nudge", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    composition.run_pre_session_hooks(spec)
    loaded = sr.read(session_dir)
    assert loaded.screen_session_id == "botainer-test-session-001"


def test_run_pre_session_hooks_skips_other_whens(tmp_path: Path) -> None:
    """Only `pre_session` hooks run; post_session etc. don't fire here."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    pre_marker = tmp_path / "pre.txt"
    post_marker = tmp_path / "post.txt"
    pre_hook = tmp_path / "pre.py"
    post_hook = tmp_path / "post.py"
    _write_hook_script(pre_hook, f"open({str(pre_marker)!r},'w').write('pre')")
    _write_hook_script(post_hook, f"open({str(post_marker)!r},'w').write('post')")

    spec = _make_spec(
        state_dir,
        hooks=(
            HookSpec(plugin="p", when="pre_session", script_path=str(pre_hook)),
            HookSpec(plugin="p", when="post_session", script_path=str(post_hook)),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    composition.run_pre_session_hooks(spec)
    assert pre_marker.exists()
    assert not post_marker.exists()


def test_run_post_session_hooks_fires(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    marker = tmp_path / "post.txt"
    hook = tmp_path / "post.py"
    _write_hook_script(hook, f"open({str(marker)!r}, 'w').write('done')")

    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="p", when="post_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    composition.run_post_session_hooks(spec)
    assert marker.read_text() == "done"


def test_run_post_session_hook_failure_swallowed(tmp_path: Path, capsys) -> None:
    """post_session hook failures don't propagate (best-effort cleanup)."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "fail.py"
    _write_hook_script(hook, "import sys; sys.exit(1)")

    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="p", when="post_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    # Should not raise.
    composition.run_post_session_hooks(spec)
    captured = capsys.readouterr()
    assert "post_session hook failed" in captured.err


def test_run_pre_session_hook_failure_propagates(tmp_path: Path) -> None:
    """pre_session hook failures DO propagate (the session can't start
    if the hook didn't succeed)."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test-session-001"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "fail.py"
    _write_hook_script(hook, "import sys; sys.exit(1)")

    spec = _make_spec(
        state_dir,
        hooks=(HookSpec(plugin="p", when="pre_session", script_path=str(hook)),),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="hook"):
        composition.run_pre_session_hooks(spec)


# §A19: nudge no longer ships a `prepare_socket.py` pre_session hook —
# screen runs on the HOST, not inside the container, so no socket prep
# is needed. The two end-to-end tests that exercised that hook were
# removed when the hook was deleted. Generic hook-wiring is covered by
# the synthetic-script tests above.
