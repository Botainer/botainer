"""`auth status` and `auth doctor` must not disagree about a dangling link.

THE DISAGREEMENT. A project that has left shared mode can keep a
leftover symlink into `/shared-auth/…` — a CONTAINER path. On the host it
resolves to nothing. Three surfaces described that one fact three ways:

  auth status   "✗ per-project (default): not present"   — true, and it hides
                that there is debris to remove; `exists()` follows the link, so
                a stale leftover looks exactly like never having logged in.
  auth doctor   "kind: SYMLINK -> … (shared store MISSING on the host)" plus
                "unreadable (dangling symlink)" — correct and detailed.
  auth doctor   "✓ Every project still points at the shared store."
  (verdict)     "✓ All readable holders carry the SAME refresh token."

The verdict is the serious one: TWO GREEN TICKS on a project in ISOLATED mode
whose only holder reaches nothing.

WHY THEY FIRED, both measured rather than guessed:

  * `broken_links` is built in the REGULAR-FILE branch. A symlink takes the
    other branch and `continue`s, so a dangling link never lands there, and the
    `elif projects:` all-clear ran over exactly the case that disproves it.
  * `distinct` counts READABLE tokens. With nothing readable it is 0, and
    `distinct <= 1` is true — a green tick and "nothing has invalidated them"
    derived from an empty set. Agreement over no evidence is not agreement.

AND THE FALSE POSITIVE THE FIX NEARLY SHIPPED. The first version of the `auth
status` note fired for EVERY shared-mode project, because in shared mode that
symlink is the correct, expected state — and it reads as "not present" on the
host regardless, since the target is container-absolute. The finder was written
for `auth use`, which calls it precisely WHEN leaving shared mode, so it is
mode-blind by design and the caller has to supply the mode. Caught by running
the healthy fixture, which is why both directions are pinned below.
"""
from __future__ import annotations

import json

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli

SHARED_LINK = "/shared-auth/agent-claude/.credentials.json"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0
    assert r.invoke(cli, ["init"]).exit_code == 0

    def _setup(plugin: str, *, link: bool, store: bool):
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text())
        d["agent"] = "claude"
        d["plugins_enabled"] = [plugin]
        cfg.write_text(yaml.safe_dump(d, sort_keys=False))

        from botainer.core import identity
        uid, _ = identity.resolve_identity(proj, identity_accept=True)
        prof = (tmp_path / "root" / "state" / uid / "data" / "agent-claude"
                / "profiles" / "default")
        prof.mkdir(parents=True, exist_ok=True)
        if store:
            sh = tmp_path / "root" / "shared-auth" / "agent-claude"
            sh.mkdir(parents=True, exist_ok=True)
            (sh / ".credentials.json").write_text(json.dumps(
                {"claudeAiOauth": {"refreshToken": "TOK", "accessToken": "a",
                                   "expiresAt": 4102444800000}}))
        if link:
            (prof / ".credentials.json").symlink_to(SHARED_LINK)
        return r

    return _setup


def test_the_verdict_does_not_call_a_dangling_link_still_sharing(project):
    """The false tick. `broken_links` cannot hold a symlink, so the all-clear
    fired over the one case that disproves it."""
    r = project("agent-claude", link=True, store=False)
    out = r.invoke(cli, ["auth", "doctor"]).output

    assert "Every project still points at the shared store" not in out, (
        f"a project whose link reaches nothing was called still-sharing:\n"
        f"{out[-1200:]}")
    assert "not on this host" in out, (
        f"the dangling link is not named in the verdict at all:\n{out[-1200:]}")


def test_zero_readable_holders_is_not_an_all_clear(project):
    """`distinct == 0` satisfied `distinct <= 1`, so agreement was claimed over
    an empty set — with "nothing has invalidated them" as reassurance."""
    r = project("agent-claude", link=True, store=False)
    out = r.invoke(cli, ["auth", "doctor"]).output

    assert "All readable holders carry the SAME refresh token" not in out, (
        f"a green tick was printed with nothing readable to compare:\n"
        f"{out[-1200:]}")
    assert "nothing was compared" in out and "NOT an all-clear" in out, (
        f"the empty case does not say it compared nothing:\n{out[-1200:]}")


def test_a_HEALTHY_shared_project_keeps_both_green_ticks(project):
    """OPPOSITE DIRECTION. Trading a false positive for a false negative would
    be no improvement — an intact link over a real store is the good case and
    must still read as one."""
    r = project("agent-claude-shared", link=True, store=True)
    out = r.invoke(cli, ["auth", "doctor"]).output

    assert "Every project still points at the shared store" in out, (
        f"the healthy case lost its all-clear:\n{out[-1200:]}")
    assert "All readable holders carry the SAME refresh token" in out, (
        f"the healthy case lost its token agreement:\n{out[-1200:]}")


def test_auth_status_names_the_leftover_when_the_project_has_LEFT_shared(project):
    """The third surface. "not present" is true and insufficient: it cannot be
    told apart from never having logged in."""
    r = project("agent-claude", link=True, store=False)
    out = r.invoke(cli, ["auth", "status"]).output

    assert "leftover shared-mode link" in out, (
        f"`auth status` reports only absence, so the debris is invisible "
        f"here while `auth doctor` describes it:\n{out[-900:]}")


def test_auth_status_is_SILENT_for_a_project_still_in_shared_mode(project):
    """THE FALSE POSITIVE THE FIX NEARLY SHIPPED.

    In shared mode the symlink is correct, and the per-project path reads as
    "not present" on the host anyway because the target is container-absolute.
    Without a mode gate, every healthy shared-mode project is told its working
    link reaches nothing.
    """
    r = project("agent-claude-shared", link=True, store=True)
    out = r.invoke(cli, ["auth", "status"]).output

    assert "leftover shared-mode link" not in out, (
        f"a healthy shared-mode project was told its correct link is "
        f"debris:\n{out[-900:]}")
