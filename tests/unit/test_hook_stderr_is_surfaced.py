"""A successful hook's warnings must reach the user.

#138. `run_hook` passes `capture_output=True`, so a hook's stderr lands in
`HookResult.stderr` — and no caller has ever read it when the hook exited 0.
Everything the credential machinery reports on the happy-but-notable path was
written to a void:

  - the shared-auth unknown-file leak canary
  - the anti-poisoning refusal ("does NOT look like a Claude OAuth file")
  - "back-filled shared credential", which announces a HOST-WIDE credential
    change affecting every shared-mode project on the machine

Measured against the real hook before the fix: rc 0, 113 bytes of stderr
captured, nothing shown.

This is also why it cannot become noise: these hooks are SILENT on the happy
path. `test_a_healthy_session_produces_no_output` pins that, because a warning
channel that fires every time trains everyone to ignore it — the CLAUDE.md rule
that a permanently-firing warn is a bug or a lie, never scenery.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from botainer.plugins.hooks import run_hook, surface_hook_stderr

REPO = Path(__file__).resolve().parents[2]
SHARED_HOOK = REPO / "plugins" / "agent-claude-shared" / "hooks" / "pre_session.py"


class _Result:
    def __init__(self, stderr):
        self.stderr = stderr
        self.rc = 0


def _cap(capsys):
    return capsys.readouterr().err


def test_nothing_is_emitted_for_a_silent_hook(capsys):
    surface_hook_stderr(_Result(""), "p", "pre_session")
    surface_hook_stderr(_Result("   \n  \n"), "p", "pre_session")
    assert _cap(capsys) == ""


def test_a_message_is_emitted(capsys):
    surface_hook_stderr(_Result("something happened"), "myplugin", "pre_session")
    out = _cap(capsys)
    assert "something happened" in out
    assert "myplugin" in out


def test_a_message_that_already_names_its_plugin_is_not_double_prefixed(capsys):
    """The hooks prefix themselves. `[p pre_session] [p] msg` is unreadable."""
    surface_hook_stderr(
        _Result("[agent-claude-shared] back-filled shared credential"),
        "agent-claude-shared", "pre_session")
    out = _cap(capsys).strip()
    assert out == "[agent-claude-shared] back-filled shared credential"
    assert out.count("agent-claude-shared") == 1


def test_multiline_output_is_emitted_line_by_line(capsys):
    surface_hook_stderr(_Result("first\nsecond\n\nthird"), "p", "post_session")
    lines = [ln for ln in _cap(capsys).splitlines() if ln.strip()]
    assert len(lines) == 3
    assert all("p post_session" in ln for ln in lines)


def test_it_never_raises_on_a_result_without_stderr(capsys):
    class _NoAttr:
        rc = 0
    surface_hook_stderr(_NoAttr(), "p", "pre_session")   # must not raise
    assert _cap(capsys) == ""


# ── against the real hook, not a stub ──

def _write(path: Path, tag: str, expires_ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": f"sk-ant-oat-{tag}" + "x" * 58,
        "refreshToken": f"sk-ant-ort-{tag}" + "x" * 58,
        "expiresAt": expires_ms,
    }}))
    path.chmod(0o600)


def _run_real(root: Path):
    return run_hook(
        plugin_name="agent-claude-shared", hook_when="pre_session",
        script_path=SHARED_HOOK,
        env={"BOTAINER_PROJECT_UUID": "p1", "BOTAINER_STATE_ROOT": str(root)},
        agent_writable_roots=(),
    )


def test_a_healthy_session_produces_no_output(tmp_path, capsys):
    """The load-bearing half. If a clean run said anything, surfacing it would
    become noise and the channel would be worthless."""
    now = int(time.time() * 1000)
    _write(tmp_path / "shared-auth/agent-claude/.credentials.json", "A", now + 3_600_000)
    res = _run_real(tmp_path)
    assert res.rc == 0
    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    assert _cap(capsys) == "", "a healthy session emitted output; this would become noise"


def test_a_host_wide_credential_change_IS_reported(tmp_path, capsys):
    """The message that has never once reached a user."""
    now = int(time.time() * 1000)
    _write(tmp_path / "shared-auth/agent-claude/.credentials.json", "OLD", now + 3_600_000)
    _write(tmp_path / "state/p1/data/agent-claude/profiles/default/.credentials.json",
           "NEW", now + 7_200_000)

    res = _run_real(tmp_path)
    assert res.rc == 0, "the hook must SUCCEED — that is the whole point of #138"
    assert res.stderr, "the hook said nothing; fixture no longer triggers a back-fill"

    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    out = _cap(capsys)
    # Assert the MEANING the user needs, not the wording — the wording was
    # rewritten once already because "back-filled" told the user nothing.
    assert "refreshed its login token" in out
    assert "SHARED login" in out
    assert "every project using shared mode" in out, (
        "the message must say WHO is affected; a host-wide credential change "
        "that reads as project-local is worse than silence"
    )


def test_no_token_material_reaches_the_users_terminal(tmp_path, capsys):
    """Whatever a hook prints gets shown, so check the hooks do not print
    secrets. A leak here would be printed to a terminal and into scrollback."""
    now = int(time.time() * 1000)
    _write(tmp_path / "shared-auth/agent-claude/.credentials.json", "OLD", now + 3_600_000)
    _write(tmp_path / "state/p1/data/agent-claude/profiles/default/.credentials.json",
           "NEW", now + 7_200_000)
    res = _run_real(tmp_path)
    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    out = _cap(capsys)
    assert "sk-ant-oat" not in out
    assert "sk-ant-ort" not in out


def test_every_run_hook_call_site_surfaces_stderr():
    """Three call sites in composition.py. Missing one is a silent regression
    of exactly the kind #138 was — so pin the count rather than trusting review.
    """
    src = (REPO / "botainer" / "core" / "composition.py").read_text()
    calls = src.count("plugin_hooks.run_hook(")
    surfaces = src.count("plugin_hooks.surface_hook_stderr(")
    assert surfaces == calls, (
        f"{calls} run_hook call sites but {surfaces} surface_hook_stderr calls — "
        f"a hook's warnings are being discarded again"
    )


# ── the fixture-realism lesson ──

def _realistic_shared_dir(root: Path) -> None:
    """A shared dir as it exists on a REAL install, not a minimal one.

    My first version of `test_a_healthy_session_produces_no_output` created
    only `.credentials.json`, measured zero bytes, and I committed a claim that
    surfacing hook stderr could never become noise. A real shared dir also holds
    `.claude.json` — symlinked there by this very hook — and the broker's
    refresh lockfile. Against that, the leak canary fired on EVERY start.

    So the "healthy session is silent" property was true of my fixture and false
    of reality. Third time in two days that an unrealistic fixture produced a
    confident wrong answer; hence this helper, used by every test below.
    """
    now = int(time.time() * 1000)
    d = root / "shared-auth" / "agent-claude"
    _write(d / ".credentials.json", "A", now + 3_600_000)
    (d / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"u1"}}')
    (d / "..credentials.json.refresh.lock").write_text("")


def test_a_realistic_healthy_session_is_silent(tmp_path, capsys):
    _realistic_shared_dir(tmp_path)
    res = _run_real(tmp_path)
    assert res.rc == 0
    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    out = _cap(capsys)
    assert out == "", f"a healthy REAL install would warn on every start:\n{out}"


def test_botainers_own_files_are_not_reported_as_unexpected(tmp_path, capsys):
    """Each of these is created by botainer itself. Warning about them is
    warning the user about us."""
    _realistic_shared_dir(tmp_path)
    res = _run_real(tmp_path)
    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    out = _cap(capsys)
    for ours in (".claude.json", "refresh.lock"):
        assert ours not in out


def test_a_GENUINELY_unknown_file_still_warns(tmp_path, capsys):
    """The canary must not have been muted, only corrected.

    Widening an allowlist to silence a warning is how a real detector becomes
    decoration. This is the counterpart test: something botainer did not put
    there must still be reported.
    """
    _realistic_shared_dir(tmp_path)
    (tmp_path / "shared-auth" / "agent-claude" / "stolen-creds.json").write_text("{}")
    res = _run_real(tmp_path)
    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    out = _cap(capsys)
    assert "stolen-creds.json" in out
    assert "unexpected file" in out


def test_the_suffix_allowance_is_narrow(tmp_path, capsys):
    """`.refresh.lock` is allowed by SUFFIX because the broker derives the name
    from the credential filename. That must not become a wildcard."""
    _realistic_shared_dir(tmp_path)
    d = tmp_path / "shared-auth" / "agent-claude"
    (d / "evil.lock").write_text("")          # .lock but NOT .refresh.lock
    (d / "refresh.lock.evil").write_text("")  # suffix in the middle
    res = _run_real(tmp_path)
    surface_hook_stderr(res, "agent-claude-shared", "pre_session")
    out = _cap(capsys)
    assert "evil.lock" in out
    assert "refresh.lock.evil" in out
