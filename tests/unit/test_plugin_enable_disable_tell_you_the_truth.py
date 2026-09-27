"""`botainer plugin` must not report success for a no-op, and must know which
project it is in. (queue rows 45, 46, 47, 48, 49)

ALL FIVE REPRODUCED BY RUNNING the real CLI.

47 — `plugin list` LIED FROM ANY SUBDIRECTORY. It read
`Path.cwd()/.botainer/config.yaml`, so one directory down there was no config
and `enabled_in_project` stayed empty:

    [project root]  ● agent-claude  ENABLED
    [sub/deeper]    ○ agent-claude  installed, not enabled

Every enabled plugin read as not-enabled. `enable` and `disable` used the same
`Path.cwd()`, which is the write side of the same bug.

48 — `plugin disable <typo>` REPORTED SUCCESS. `plugin disable agent-claudeee`
printed "Disabled 'agent-claudeee' for this project." and exited 0. `enable`
got a typo guard in #235; `disable` never did, so a user fixing something by
disabling it walks away believing they have.

45 — ENABLING A SECOND SAME-FAMILY AGENT PLUGIN SUCCEEDED, and `config check`
called the result "no issues found". NARROWER THAN FILED, and worth saying:
`compose_session` DOES refuse this, by name, with the fix. So it was never a
hole in the cage — the enforcement was simply unreachable until launch. The fix
extracts that check (it is now `plugins/selection.check_family_exclusion`) and
calls it from three places instead of one.

46 + 49 — DISABLING THE LAST AGENT PLUGIN WAS SILENT, and the state it left was
invisible until launch, where the refusal blamed a missing IMAGE for a plugin
that was no longer enabled.

This one is a QUESTION, NOT A REFUSAL, deliberately. A session with no agent
plugin is a legitimate state — composition has a branch for it, and a preflight
with zero agent plugins gets past every plugin check — so refusing centrally
would break a supported use case. Asking at the point of action lets the user
confirm the intended agentless configuration.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.plugin import plugin

UID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def proj(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    (root / ".botainer").mkdir(parents=True)
    (root / ".botainer" / "project-id").write_text(UID + "\n")
    (root / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: docker\n"
        "profile: default\nnetwork:\n  mode: internet\n"
        "plugins_enabled:\n  - agent-claude\n  - git\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.chdir(root)
    return root


def _run(*args, stdin: str | None = None):
    return CliRunner().invoke(plugin, list(args), input=stdin)


def _enabled(root: Path) -> str:
    return (root / ".botainer" / "config.yaml").read_text()


# ── 47: which project am I in? ───────────────────────────────────────────────

def test_list_from_a_subdirectory_still_knows_what_is_enabled(proj, monkeypatch):
    """THE DEFECT: every enabled plugin printed as "installed, not enabled"."""
    deep = proj / "sub" / "deeper"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    out = _run("list").output

    assert "agent-claude" in out, out
    line = [l for l in out.splitlines() if "agent-claude " in l and "broker" not in l]
    assert line and "ENABLED" in line[0], (
        f"reported an enabled plugin as not-enabled from a subdirectory:\n{out}")


def test_disable_from_a_subdirectory_edits_the_PROJECT_config(proj, monkeypatch):
    """The write side of the same bug: `Path.cwd()` was the target."""
    deep = proj / "sub" / "deeper"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    _run("disable", "git")

    assert "- git" not in _enabled(proj), (
        "disable from a subdirectory did not reach the project config")
    assert not (deep / ".botainer").exists(), (
        "disable created a config in the subdirectory instead")


# ── 48: a no-op must not report success ──────────────────────────────────────

def test_disable_refuses_a_name_that_is_not_enabled(proj):
    """THE DEFECT: printed "Disabled 'agent-claudeee'" and exited 0."""
    res = _run("disable", "agent-claudeee")

    assert res.exit_code != 0, res.output
    assert "nothing to disable" in res.output, res.output
    assert "Disabled 'agent-claudeee'" not in res.output, res.output


def test_that_refusal_suggests_the_name_you_meant(proj):
    """`enable` got this in #235; the point is that `disable` now matches.

    ASSERTS THE QUOTED SUGGESTION, NOT A SUBSTRING. The first version asserted
    `"agent-claude" in out` — and the typo `agent-claudeee` CONTAINS
    "agent-claude", so the pre-fix output `Disabled 'agent-claudeee'` satisfied
    it. The test passed with the entire typo guard deleted. I had noticed it was
    green pre-fix and filed it under "ordinary-path controls"; the loop tzar
    caught that green-pre-fix was the question, not the answer.

    `Did you mean 'agent-claude'?` with both quotes cannot be satisfied by
    `'agent-claudeee'`, so it now pins the suggestion itself.
    """
    out = _run("disable", "agent-claudeee").output

    assert "Did you mean 'agent-claude'" in out, out
    assert "Did you mean 'agent-claudeee'" not in out, (
        "suggested the typo back at the user:\n" + out)


def test_disabling_something_installed_but_not_enabled_here_also_refuses(proj):
    """It is equally a no-op, and equally worth saying out loud."""
    res = _run("disable", "agent-claude-broker")
    assert res.exit_code != 0, res.output
    assert "not enabled for this project" in res.output, res.output


# ── 45: the exclusion check runs where the user is, not only at launch ───────

def test_enable_refuses_a_second_same_family_agent_plugin(proj):
    """THE DEFECT: succeeded here, passed `config check`, refused at `start`."""
    res = _run("enable", "agent-claude-broker")

    assert res.exit_code != 0, res.output
    assert "mutually exclusive" in res.output, res.output


def test_nothing_is_written_when_enable_refuses(proj):
    """Checking the PROSPECTIVE set is the point — refuse before the write."""
    before = _enabled(proj)
    _run("enable", "agent-claude-broker")
    assert _enabled(proj) == before, (
        "the config was modified by an enable that refused")


def test_an_ordinary_enable_still_works(proj):
    """Or the guard is just breakage."""
    res = _run("enable", "hpc-launcher")
    assert res.exit_code == 0, res.output
    assert "hpc-launcher" in _enabled(proj)


# ── 46 + 49: removing the last agent plugin is a question ───────────────────

def test_disabling_the_last_agent_plugin_ASKS(proj):
    """THE DEFECT: silent, and invisible until a launch-time image error."""
    out = _run("disable", "agent-claude", stdin="n\n").output

    assert "only agent plugin" in out, out
    assert "Disable it anyway?" in out, out


def test_answering_no_changes_nothing(proj):
    """A prompt that acts anyway is worse than no prompt."""
    before = _enabled(proj)
    _run("disable", "agent-claude", stdin="n\n")
    assert _enabled(proj) == before, "disabled despite the user declining"


def test_answering_yes_still_works(proj):
    """It is a question, NOT a refusal — a no-agent project is legitimate."""
    _run("disable", "agent-claude", stdin="y\n")
    assert "- agent-claude" not in _enabled(proj), (
        "the confirmation did not actually disable it")


def test_the_prompt_says_what_will_go_wrong_and_names_the_alternative(proj):
    """"Are you sure?" with no consequence stated is noise the user learns to
    click through. It must say what breaks AND what they probably wanted."""
    out = _run("disable", "agent-claude", stdin="n\n").output

    assert "nothing to launch" in out, out
    assert "auth use" in out, f"never named the mode-switch alternative:\n{out}"


def test_disabling_a_NON_agent_plugin_does_not_ask(proj):
    """Or the prompt fires on the ordinary path and gets trained away."""
    res = _run("disable", "git")

    assert "Disable it anyway?" not in res.output, res.output
    assert "- git" not in _enabled(proj), res.output
