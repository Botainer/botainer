"""Every surface init prints must describe the config it ACTUALLY wrote.

THE CRITICAL THIS ENCODES (onboarding tzar's first road-walk).

`botainer start` in a fresh directory on a host where `botainer setup` has not
run is the shortest road the product offers. Its auto-onboard runs `init`
BEFORE it offers `setup` (cli/start.py). At that moment no plugins are
installed, so `build_default_config` cannot honour
`policy.default_auth_mode = "shared"` and falls back to the isolated
`agent-claude`. It says so — on plain stderr.

Three louder surfaces then said the opposite, because each re-read
`policy.default_auth_mode` (what init REQUESTED) rather than the config that
had just been written:

  1. a BOLD warning headed "Auth mode for this project: SHARED (host-wide
     credentials)", plus a paragraph of shared-mode security advice that did
     not apply
  2. the comment written into the user's OWN config.yaml — "this project stays
     on 'shared'" — one line under `- agent-claude`, outliving the session
  3. the printed next-step `auth login --shared … # matches this project's
     shared mode`

The true line was plain; the false ones were bold and persistent. The user runs
the printed login, starts, and the agent asks them to log in — verbatim the
maintainer's report.

WHY THIS RUNS IN-PROCESS. The first version of this file drove `init` as a
subprocess and PASSED WITH BOTH HALVES OF THE FIX REVERTED — vacuously, because
under an editable install `list_installed()` overlays the clone's `plugins/`
tree (lifecycle.py `_editable_source_plugins_root`), so the fallback never
fired and every assertion sat behind an `if` that was never true. A subprocess
cannot be made plugin-less here. So: patch `list_installed` to return nothing,
which is the state of a host before `botainer setup`, and assert the
precondition was actually reached — a fixture that fails to reproduce the case
must FAIL, not pass quietly.
"""
from __future__ import annotations

import pytest
import yaml

from botainer.cli import init as init_cli


@pytest.fixture()
def virgin(tmp_path, monkeypatch):
    """A host BEFORE `botainer setup`: policy asks for shared, nothing installed.

    Not the minimum that makes the code path run — installing any plugin makes
    the defect unreachable, which is exactly how the first version of this test
    managed to pass against the bug.
    """
    home = tmp_path / "home"
    (home / ".botainer").mkdir(parents=True)
    (home / ".botainer" / "policy.yaml").write_text(
        yaml.safe_dump({"default_auth_mode": "shared"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_BOTAINER", str(home / ".botainer"))
    monkeypatch.setattr("botainer.plugins.lifecycle.list_installed", lambda: [])
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def _written_mode(proj) -> str:
    cfg = yaml.safe_load(
        (proj / ".botainer" / "config.yaml").read_text(encoding="utf-8")) or {}
    for n in cfg.get("plugins_enabled") or []:
        if isinstance(n, str) and n.startswith("agent-"):
            tail = n.rsplit("-", 1)[-1]
            return tail if tail in ("shared", "broker", "proxy") else "isolated"
    return ""


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_no_surface_announces_a_mode_the_config_does_not_have(
        virgin, capsys, agent):
    init_cli.do_init(virgin, agent=agent)
    # BOTH streams. The banner goes to stderr (style.warn) and the next-steps to
    # stdout; checking only .out made the guard test below fail against a
    # working fix, which is a test bug that reads exactly like a code bug.
    cap = capsys.readouterr()
    out = cap.out + cap.err
    written = _written_mode(virgin)

    # PRECONDITION, asserted rather than assumed. If the fallback did not fire,
    # this fixture is not reproducing the reported case and the assertions below
    # would pass vacuously — which is how the previous version of this test
    # survived a full revert of the fix.
    assert written == "isolated", (
        f"fixture did not reach the case: with policy default_auth_mode=shared "
        f"and NO plugins installed, init should fall back to the isolated "
        f"variant, but wrote {written!r}. Fix the fixture, not the assertion."
    )

    assert "SHARED (host-wide credentials)" not in out, (
        "init wrote an ISOLATED project and printed the bold SHARED warning. A "
        "user acting on it logs in host-wide, starts, and the agent asks them "
        f"to log in.\n{out}")
    assert "--shared" not in out, (
        f"init wrote an ISOLATED project and printed an `auth login --shared` "
        f"next-step.\n{out}")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_the_comment_in_the_users_own_config_matches_the_plugin_above_it(
        virgin, agent):
    """This one outlives the session, which is why it gets its own test."""
    init_cli.do_init(virgin, agent=agent, quiet=True)
    written = _written_mode(virgin)
    assert written == "isolated", "fixture did not reach the case (see sibling)"

    raw = (virgin / ".botainer" / "config.yaml").read_text(encoding="utf-8")
    for mode in ("isolated", "shared", "broker", "proxy"):
        if f"this project stays on {mode!r}" in raw:
            assert mode == written, (
                f"config.yaml records the {written} plugin and then tells the "
                f"user, in their own file, that the project stays on {mode!r}. "
                f"That comment outlives every session.")
            break


def test_a_genuinely_shared_project_still_gets_the_warning(tmp_path, monkeypatch,
                                                           capsys):
    """The fix must not silence the warning where it is TRUE.

    Deleting a warning is an easy way to make the test above pass; this is what
    stops that. Here the shared variant IS installed, so no fallback happens.
    """
    home = tmp_path / "home"
    (home / ".botainer").mkdir(parents=True)
    (home / ".botainer" / "policy.yaml").write_text(
        yaml.safe_dump({"default_auth_mode": "shared"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_BOTAINER", str(home / ".botainer"))
    proj = tmp_path / "proj"
    proj.mkdir()

    init_cli.do_init(proj, agent="claude")
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert _written_mode(proj) == "shared", "fixture did not reach the case"
    assert "SHARED (host-wide credentials)" in out, (
        "the shared-mode warning disappeared for a project that IS shared — "
        f"the fix silenced a true warning instead of correcting a false one:\n{out}")
