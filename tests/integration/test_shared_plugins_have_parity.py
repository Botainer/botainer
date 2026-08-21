"""agent-claude-shared and agent-codex-shared must not drift apart.

THE PATTERN THIS EXISTS TO STOP (#136). Both plugins implement the same
protocol — symlink the project's credential into a shared store, detect the
break-away an in-container refresh causes, back-fill, re-link. Both were
hand-written. Every hardening applied to one has, at some point, not been
applied to the other, and each gap was found years later by accident:

  - #266 fail-closed locking landed in claude; codex kept an
    UNLOCKED `except OSError: tmp.rename(...)` fallback for 69 days — doing the
    dangerous thing precisely when the safety mechanism was unavailable.
  - Exit-time reconcile landed in claude; codex had NO post_session
    at all, so it still had the exact "new project inherits a stale token"
    bug that fix exists for.
  - Ordering: claude requires a strictly-later expiry before overwriting the
    shared store; codex back-filled on content inequality alone, so an OLDER
    token could overwrite a NEWER one.

And the inverse, which is why "copy claude's file" is not the fix either: codex
has a same-account `account_id` gate that claude has no equivalent for.
NEITHER plugin is a superset of the other.

These tests assert the SHAPE both must share. They are not a substitute for the
real fix — one trusted reconcile both plugins call, with the per-agent parts as
parameters — but they make the next divergence fail here instead of on someone's
cluster.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

AGENTS = {
    "agent-claude-shared": {
        "cred": ".credentials.json",
        "state_sub": "agent-claude",
        "shared_sub": "agent-claude",
    },
    "agent-codex-shared": {
        "cred": "auth.json",
        "state_sub": "agent-codex",
        "shared_sub": "agent-codex",
    },
}


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_both_have_an_exit_reconcile(plugin):
    """A refresh must propagate when the session ENDS, not only at the next
    start of that same project. Without it a DIFFERENT project starting in
    between inherits a stale token."""
    hook = REPO / "plugins" / plugin / "hooks" / "post_session.py"
    assert hook.exists(), (
        f"{plugin} has no post_session hook, so a token refreshed inside the "
        f"container waits until that project's NEXT start"
    )
    manifest = yaml.safe_load(
        (REPO / "plugins" / plugin / "botainer-plugin.yaml").read_text())
    whens = {h["when"] for h in (manifest.get("hooks") or [])}
    assert "post_session" in whens, (
        f"{plugin} ships a post_session.py that its manifest never registers — "
        f"the file exists and never runs"
    )


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_both_hooks_are_executable(plugin):
    """A deploy property, not a code property: git records the mode, and a
    non-executable hook fails only on the cluster (#118)."""
    for name in ("pre_session.py", "post_session.py"):
        hook = REPO / "plugins" / plugin / "hooks" / name
        if not hook.exists():
            continue
        mode = subprocess.run(
            ["git", "ls-files", "-s", str(hook.relative_to(REPO))],
            cwd=REPO, capture_output=True, text=True).stdout.split()
        assert mode and mode[0] == "100755", (
            f"{plugin}/{name} is not executable IN GIT (mode {mode[0] if mode else '?'}); "
            f"it will fail on a fresh clone even if it works here"
        )


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_neither_falls_back_to_an_unlocked_write(plugin):
    """Failing OPEN when the lock is unavailable is worse than failing at all.

    Two sessions back-filling at once both spend the rotating refresh token,
    which trips the provider's reuse detection and can lock the account. That
    is the exact scenario the lock exists for, so the fallback ran precisely
    when it must not.
    """
    src = (REPO / "plugins" / plugin / "hooks" / "pre_session.py").read_text()
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if "except OSError" not in line:
            continue
        window = "\n".join(lines[i:i + 4])
        assert not (".rename(shared_file)" in window
                    or ".replace(shared_file)" in window), (
            f"{plugin}/pre_session.py:{i+1} falls back to an UNLOCKED write to "
            f"the shared credential when locking fails:\n{window}"
        )


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_an_older_credential_never_overwrites_a_newer_one(plugin, tmp_path):
    """BEHAVIOURAL. An earlier version of this test grepped for `_is_newer`,
    and renaming the function to `_DISABLED_is_newer` still contained the
    substring — so it passed with the guard removed. Presence is not effect;
    drive it instead.

    The scenario: a stale session exits last, holding an OLD token, and its
    break-away file must not roll the shared store backwards.
    """
    spec = AGENTS[plugin]
    shared_dir = tmp_path / "shared-auth" / spec["shared_sub"]
    shared_dir.mkdir(parents=True)
    shared_file = shared_dir / spec["cred"]
    shared_file.write_text(_shared_cred(plugin, "NEW", "2026-08-01T00:00:00Z"))
    shared_file.chmod(0o600)

    # A break-away holding an OLDER credential, same account.
    proj = (tmp_path / "state" / "p1" / "data" / spec["state_sub"]
            / "profiles" / "default")
    proj.mkdir(parents=True)
    stale = proj / spec["cred"]
    stale.write_text(_shared_cred(plugin, "OLD", "2026-07-01T00:00:00Z"))
    stale.chmod(0o600)

    _run(plugin, "pre_session.py", tmp_path)

    after = shared_file.read_text()
    assert "NEW" in after and "OLD" not in after, (
        f"{plugin} let an OLDER credential overwrite the shared store — a "
        f"stale session exiting last rolls everyone back.\nshared is now: "
        f"{after[:120]}"
    )


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_the_reconcile_is_callable_by_both_hooks(plugin):
    """One implementation, two callers.

    If post_session re-implements the reconcile rather than calling it, the two
    WILL disagree about what a valid back-fill is — which is this whole file's
    subject.
    """
    post = (REPO / "plugins" / plugin / "hooks" / "post_session.py").read_text()
    assert "from pre_session import reconcile_shared_credential" in post, (
        f"{plugin}/post_session.py does not call pre_session's reconcile; a "
        f"second implementation will drift from the first"
    )


# ── behavioural parity, driven ──

def _shared_cred(plugin: str, tag: str, when: str) -> str:
    """`when` must drive each agent's OWN ordering field, or the fixture does
    not encode "older" at all.

    First version used `time.time()` for claude's expiresAt regardless of
    `when`, so the credential written SECOND got the later expiry — the "old"
    one was newer, and the test failed for the opposite of the reason it
    names. The ordering signal differs per agent (claude: expiresAt, codex:
    last_refresh), so the fixture has to speak both.
    """
    base = int(time.time() * 1000)
    # Later `when` => later expiry, so "NEW" really is newer than "OLD".
    offset = 7_200_000 if when >= "2026-08-01" else 3_600_000
    if plugin == "agent-claude-shared":
        return json.dumps({"claudeAiOauth": {
            "accessToken": f"sk-ant-oat-{tag}" + "x" * 58,
            "refreshToken": f"sk-ant-ort-{tag}" + "x" * 58,
            "expiresAt": base + offset,
        }})
    return json.dumps({
        "tokens": {"access_token": tag + "a" * 40,
                   "refresh_token": tag + "r" * 40,
                   "account_id": "acct-1"},
        "last_refresh": when,
    })


def _run(plugin: str, hook: str, root: Path, uid: str = "p1"):
    env = dict(os.environ)
    env.update({"BOTAINER_PROJECT_UUID": uid, "BOTAINER_STATE_ROOT": str(root)})
    return subprocess.run(
        [sys.executable, str(REPO / "plugins" / plugin / "hooks" / hook)],
        env=env, capture_output=True, text=True)


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_a_healthy_start_links_and_says_nothing(plugin, tmp_path):
    spec = AGENTS[plugin]
    shared = tmp_path / "shared-auth" / spec["shared_sub"]
    shared.mkdir(parents=True)
    f = shared / spec["cred"]
    f.write_text(_shared_cred(plugin, "A", "2026-08-01T00:00:00Z"))
    f.chmod(0o600)

    res = _run(plugin, "pre_session.py", tmp_path)
    assert res.returncode == 0, res.stderr

    link = (tmp_path / "state" / "p1" / "data" / spec["state_sub"]
            / "profiles" / "default" / spec["cred"])
    assert link.is_symlink(), f"{plugin} did not create the shared symlink"


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_exit_reconcile_runs_without_error_on_a_clean_project(plugin, tmp_path):
    """post_session must never fail a session that already succeeded."""
    spec = AGENTS[plugin]
    shared_dir = tmp_path / "shared-auth" / spec["shared_sub"]
    shared_dir.mkdir(parents=True)
    f = shared_dir / spec["cred"]
    f.write_text(_shared_cred(plugin, "A", "2026-08-01T00:00:00Z"))
    f.chmod(0o600)
    _run(plugin, "pre_session.py", tmp_path)

    link = (tmp_path / "state" / "p1" / "data" / spec["state_sub"]
            / "profiles" / "default" / spec["cred"])
    before = os.readlink(link)

    res = _run(plugin, "post_session.py", tmp_path)

    assert res.returncode == 0, (
        f"{plugin} post_session exited {res.returncode}; a finished session "
        f"must never be reported as failed:\n{res.stderr}"
    )
    assert "Traceback" not in res.stderr, res.stderr
    # The observable effect, not just the exit code: the project is still
    # linked to the shared store and the shared credential is untouched.
    assert link.is_symlink() and os.readlink(link) == before, (
        f"{plugin} post_session disturbed the symlink on a clean project"
    )
    assert json.loads((shared_dir / spec["cred"]).read_text()), (
        f"{plugin} post_session left the shared credential unparseable"
    )


@pytest.mark.parametrize("plugin", sorted(AGENTS))
def test_exit_reconcile_is_a_noop_when_there_is_nothing_to_do(plugin, tmp_path):
    """No project dir, no shared dir: return quietly rather than erroring."""
    res = _run(plugin, "post_session.py", tmp_path)
    assert res.returncode == 0
    assert "Traceback" not in res.stderr
