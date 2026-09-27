"""`start --auth-mode X` reads a different history directory and says so.

A one-session auth-mode override can select a different history directory.
The command must disclose the selected directory; a temporary override does not
carry history between directories.

WHY IT DOES NOT TRANSFER, AND WHY THAT IS RIGHT. The mode and the profile are
PATH COMPONENTS of the agent's config dir:

    data/<agent>/profiles/<profile>/       isolated, shared
    data/<agent>/broker-state/<profile>/   broker

`auth use` and `config set` change what the config DECLARES, and both offer to
carry the history across. `offer_carry_for_declared_change` gives `start` the
same treatment for a hand-edited config — deliberately by reading the FILE, not
the effective spec, because a spec comparison would make one temporary
`--auth-profile work` look like a permanent switch and the next plain `start`
look like a switch back, bouncing history between directories on a flag.

So a one-session override must NOT carry. What was missing is that it also said
nothing: the agent came up with no history while the user's transcripts sat in
the other directory, and no line on screen connected the two.

WHAT THIS PINS. The disclosure fires for an override that changes the
directory, names BOTH paths, says when the one being read is empty, and points
at the commands that do carry. And — the opposite direction, which is the whole
reason the design reads the file — it stays SILENT when no override is passed,
because a warning on every ordinary `start` is scenery.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli


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

    def _setup(agent: str, plugin: str, *, with_history: bool = True):
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text())
        d["plugins_enabled"] = [plugin]
        d["agent"] = agent
        cfg.write_text(yaml.safe_dump(d, sort_keys=False))

        from botainer.core import identity
        uid, _ = identity.resolve_identity(proj, identity_accept=True)
        declared = (tmp_path / "root" / "state" / uid / "data"
                    / f"agent-{agent}" / "profiles" / "default")
        declared.mkdir(parents=True, exist_ok=True)
        if with_history:
            (declared / "history.jsonl").write_text('{"msg":"real work"}\n')
        return declared

    return r, _setup


def test_an_auth_mode_override_names_BOTH_directories(project):
    """Naming only one is what made this invisible: a user who is told the new
    path still cannot find the old one."""
    r, setup = project
    declared = setup("codex", "agent-codex-shared")

    res = r.invoke(cli, ["start", "--auth-mode", "broker", "--yes"])

    assert "DIFFERENT history directory" in res.output, (
        f"a one-session override moved which history is read and said "
        f"nothing:\n{res.output[:900]}")
    assert str(declared) in res.output, (
        f"the message does not name the directory the user's history is "
        f"actually in:\n{res.output[:900]}")
    assert "broker-state" in res.output, (
        f"the message does not name the directory this session will read:\n"
        f"{res.output[:900]}")


def test_it_says_when_the_directory_being_read_is_EMPTY(project):
    """The fact that explains the symptom. Without it the user reads two paths
    and still has to work out why the agent has amnesia."""
    r, setup = project
    setup("codex", "agent-codex-shared", with_history=True)

    res = r.invoke(cli, ["start", "--auth-mode", "broker", "--yes"])

    assert "EMPTY" in res.output, (
        f"the session reads an empty directory while history exists in the "
        f"declared one, and the output does not say so:\n{res.output[:900]}")


def test_it_points_at_the_commands_that_DO_carry(project):
    """A disclosure with no way forward is the dead-end shape this project has
    already been told off for."""
    r, setup = project
    setup("codex", "agent-codex-shared")

    res = r.invoke(cli, ["start", "--auth-mode", "broker", "--yes"])

    assert "auth use" in res.output and "carry" in res.output, (
        f"the user is told their history is elsewhere and not how to bring "
        f"it:\n{res.output[:900]}")


def test_a_PLAIN_start_says_nothing(project):
    """OPPOSITE DIRECTION, and the reason the carry reads the file not the spec.

    A warning on every ordinary launch is scenery, and scenery is what trains
    people to stop reading the gate.
    """
    r, setup = project
    setup("codex", "agent-codex-shared")

    res = r.invoke(cli, ["start", "--yes"])

    assert "DIFFERENT history directory" not in res.output, (
        f"an ordinary start warns about a change that did not happen:\n"
        f"{res.output[:900]}")


def test_an_override_that_changes_NOTHING_says_nothing(project):
    """Passing the mode the config already declares is not a change.

    Keyed on the resulting PATH rather than on "a flag was passed", so
    `--auth-mode shared` on a shared project is silent — the distinction a
    flag-presence check would get wrong.
    """
    r, setup = project
    setup("codex", "agent-codex-shared")

    res = r.invoke(cli, ["start", "--auth-mode", "shared", "--yes"])

    assert "DIFFERENT history directory" not in res.output, (
        f"an override naming the mode already in effect was reported as a "
        f"change:\n{res.output[:900]}")
