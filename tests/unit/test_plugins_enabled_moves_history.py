"""Plugin changes warn about history only when resolved directories differ.

Isolated and shared modes use data/agent-claude/profiles/<profile>; broker
uses data/agent-claude/broker-state/<profile>. Changing plugins_enabled can
therefore select a different history directory even when profile is unchanged.

Compare resolved paths so isolated-to-shared changes stay silent while
broker transitions warn and offer the history-carry flow. This also avoids
hard-coding which mode names imply different history locations.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.config_cmd import config

UID = "11111111-1111-4111-8111-111111111111"
ISOLATED = "[agent-claude, git]"
SHARED = "[agent-claude-shared, git]"
BROKER = "[agent-claude-broker, git]"


def _project(tmp_path: Path, monkeypatch, plugins: str,
             plant_history_in: str = "profiles") -> Path:
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(UID + "\n")
    (proj / ".botainer" / "config.yaml").write_text(
        f"version: config-v1\nagent: claude\nruntime: docker\n"
        f"profile: default\nnetwork:\n  mode: internet\n"
        f"plugins_enabled: {plugins}\n")
    # History must EXIST for the warning to fire — `warn_history_will_move`
    # deliberately says nothing about moving an empty directory.
    hist = (tmp_path / "state" / "state" / UID / "data" / "agent-claude"
            / plant_history_in / "default")
    hist.mkdir(parents=True)
    (hist / "claude.json").write_text('{"projects":{}}')
    (hist / "todos").mkdir()
    (hist / "todos" / "t.md").write_text("x")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    return proj


def _set(key: str, value: str) -> str:
    """Run `config set` answering NO — the warning precedes the confirm."""
    return CliRunner().invoke(config, ["set", key, value], input="n\n").output


def test_switching_to_BROKER_warns_that_history_moves(tmp_path, monkeypatch):
    """The defect: this was silent, and the next session looked amnesiac."""
    _project(tmp_path, monkeypatch, ISOLATED)
    out = _set("plugins_enabled", BROKER)
    assert "session history" in out, (
        f"enabling the broker plugin relocates history and said nothing:\n{out}")


def test_the_warning_names_BOTH_directories(tmp_path, monkeypatch):
    """"Your history moved" without saying where is the #138 shape.

    Asserted separately so a warning that fires but points nowhere fails.
    """
    _project(tmp_path, monkeypatch, ISOLATED)
    out = _set("plugins_enabled", BROKER)
    assert "profiles/default" in out, f"never named the source:\n{out}"
    assert "broker-state/default" in out, f"never named the destination:\n{out}"


def test_switching_to_SHARED_is_SILENT(tmp_path, monkeypatch):
    """Isolated and shared are the SAME directory; a warning would be false.

    This is the assertion that makes the fix a path comparison rather than a
    key list — and the one that would have caught the version of this fix I
    almost wrote.
    """
    _project(tmp_path, monkeypatch, ISOLATED)
    out = _set("plugins_enabled", SHARED)
    assert "session history" not in out, (
        f"warned that history moves for a switch that keeps the same "
        f"directory:\n{out}")


def test_leaving_BROKER_warns_too(tmp_path, monkeypatch):
    """The move is symmetric; history left behind in broker-state is as lost."""
    _project(tmp_path, monkeypatch, BROKER, plant_history_in="broker-state")
    out = _set("plugins_enabled", ISOLATED)
    assert "session history" in out, (
        f"leaving broker mode strands the history in broker-state/ and said "
        f"nothing:\n{out}")
    assert "broker-state/default" in out and "profiles/default" in out, out


def test_a_key_that_moves_NOTHING_is_still_silent(tmp_path, monkeypatch):
    """Guards against the fix widening into a warning on every edit."""
    _project(tmp_path, monkeypatch, ISOLATED)
    out = _set("network.mode", "none")
    assert "session history" not in out, (
        f"warned about history for a network change:\n{out}")


def test_the_profile_axis_still_works(tmp_path, monkeypatch):
    """No regression: `profile` warned before this change and must still."""
    _project(tmp_path, monkeypatch, ISOLATED)
    out = _set("profile", "work")
    assert "session history" in out, f"the profile warning regressed:\n{out}"


def test_SETTING_the_key_for_the_first_time_does_not_crash(tmp_path, monkeypatch):
    """A config with no `plugins_enabled:` line is an ordinary starting state.

    `old_value` is then the `_MISSING` sentinel — an OBJECT, and a truthy one,
    so `old_value or []` passes it through and `list()` raises TypeError. The
    first version of this fix did exactly that and the existing suite caught
    it; this pins the case so it cannot come back.
    """
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(UID + "\n")
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))

    res = CliRunner().invoke(config, ["set", "--yes", "plugins_enabled", BROKER])

    # Not the exit code alone: `set` could exit 0 having written nothing, and
    # the crash this pins is a TypeError raised while deciding whether to warn
    # — after the diff is printed and before the write. So assert the write
    # HAPPENED and that the specific failure is absent from the output.
    assert res.exit_code == 0, (res.output, res.exception)
    assert not isinstance(res.exception, TypeError), (
        f"the _MISSING sentinel reached list(): {res.exception!r}")
    written = (proj / ".botainer" / "config.yaml").read_text()
    assert "agent-claude-broker" in written, (
        f"`set` exited 0 but never wrote the value:\n{written}")


def test_config_set_agent_names_the_RIGHT_dir_for_EACH_agent(tmp_path, monkeypatch):
    """One mode was resolved and applied to BOTH agents. (queue item 29)

    Observed on the real CLI: a claude project that merely has
    `agent-codex-broker` also enabled — an ordinary state, you use claude here
    and codex elsewhere — was told its CLAUDE history lived at
    `agent-claude/broker-state/default`. That directory does not exist. The real
    history, claude.json and todos/, sits in `profiles/default`.

    Enabling a broker plugin for ONE family says nothing about the other. This
    is #220 one layer down: a foreign family's auth mode driving a decision.
    """
    _project(tmp_path, monkeypatch, "[agent-claude, agent-codex-broker, git]")
    out = _set("agent", "codex")

    claude_line = [l for l in out.splitlines() if l.strip().startswith("claude:")]
    codex_line = [l for l in out.splitlines() if l.strip().startswith("codex:")]
    assert claude_line and codex_line, f"both agents must be named:\n{out}"

    assert "broker-state" not in claude_line[0], (
        "claude is not in broker mode — only agent-codex-broker is enabled — so "
        f"this names a directory that does not exist: {claude_line[0]!r}")
    assert "profiles/default" in claude_line[0], claude_line[0]
    assert "broker-state/default" in codex_line[0], (
        f"codex IS in broker mode; its path should say so: {codex_line[0]!r}")


def test_the_dir_it_names_for_the_CURRENT_agent_actually_exists(
        tmp_path, monkeypatch):
    """The claim is 'your history is here', so 'here' must be a real place.

    Asserted against the filesystem rather than against a string, because the
    defect was precisely that the string looked plausible.
    """
    _project(tmp_path, monkeypatch, "[agent-claude, agent-codex-broker, git]")
    out = _set("agent", "codex")
    claude_line = [l for l in out.splitlines()
                   if l.strip().startswith("claude:")][0]
    named = Path(claude_line.split(":", 1)[1].strip())
    assert named.exists(), (
        f"`config set agent` told the user their claude history is at {named}, "
        "which does not exist on disk")
