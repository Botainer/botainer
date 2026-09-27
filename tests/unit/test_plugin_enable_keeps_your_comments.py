"""`plugin enable/disable` must not eat the comments in `plugins_enabled`. (#226)

WHAT WAS WRONG, and the code SAID OTHERWISE. `_write_plugins_enabled`'s
docstring reads "Update the `plugins_enabled:` list in place, preserving
comments", cites a previous review that fixed exactly this, and gives an
example:

    - nudge  # see GETTING_STARTED nudge tradeoff

Running `botainer plugin enable` on precisely that line produced `  - nudge`.

The earlier fix was half applied. It stopped a whole-file `safe_load ->
safe_dump` from destroying comments ELSEWHERE in config.yaml — real, and it
worked — but the replacement block was still rebuilt from the plugin NAMES:

    new_block_lines.append(f"  - {name}")

so every comment INSIDE the block died anyway, including the docstring's own
example. A partially-applied fix documented as a complete one is worse than an
open bug: the next person reads the guarantee and believes it.

WHAT A REAL USER LOSES. On a default `botainer init` project the block carries
the note explaining that this project PINNED its auth mode at init — "this
project stays on 'isolated' even if you change the policy default later" — which
is the only place a user is told that, plus the entire commented opt-in plugin
menu. One `botainer plugin enable` and they are gone, with no warning. (`config
set` at least prints "yaml round-trip strips comments"; `plugin enable` says
nothing at all.)

THE FIX makes the ORIGINAL LINE the unit that moves, instead of the name. A
plugin that is staying keeps its own line byte for byte, so whatever the user
wrote after it survives by construction rather than by a copying rule someone
has to maintain.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from botainer.plugins.lifecycle import _write_plugins_enabled

# The docstring's own example, which the old code destroyed.
DOCSTRING_EXAMPLE = "  - nudge  # see GETTING_STARTED nudge tradeoff\n"

REALISTIC = """version: config-v1
agent: claude

plugins_enabled:
  - agent-claude   # captured from policy.default_auth_mode at init time;
                   # this project stays on 'isolated' even if you
                   # change the policy default later.
  - git            # guarded mode by default

  # Opt-in plugins (uncomment to enable).
  # - nudge        # wraps the agent in a host-side screen session

plugins:
  git:
    mode: guarded
"""


def _cfg(tmp_path: Path, text: str) -> Path:
    d = tmp_path / ".botainer"
    d.mkdir(parents=True)
    p = d / "config.yaml"
    p.write_text(text)
    return p


def test_the_docstring_example_survives(tmp_path) -> None:
    """The exact line the docstring promises to protect."""
    p = _cfg(tmp_path, "agent: claude\nplugins_enabled:\n" + DOCSTRING_EXAMPLE)
    _write_plugins_enabled(p, ["nudge", "git"])
    assert "# see GETTING_STARTED nudge tradeoff" in p.read_text(), (
        f"the comment the docstring cites was destroyed:\n{p.read_text()}")


def test_enabling_keeps_every_comment_in_the_block(tmp_path) -> None:
    """The realistic case: nothing a user wrote is lost by adding a plugin."""
    p = _cfg(tmp_path, REALISTIC)
    before = p.read_text().count("#")
    _write_plugins_enabled(p, ["agent-claude", "git", "hpc-launcher"])
    after = p.read_text()
    assert after.count("#") == before, (
        f"comment lines went from {before} to {after.count('#')}:\n{after}")
    assert "stays on 'isolated'" in after, (
        "the auth-mode pin note — the only place a user is told their project "
        f"pinned its mode — was destroyed:\n{after}")


def test_the_plugin_is_actually_ADDED(tmp_path) -> None:
    """Preserving comments is worthless if the write stops working.

    Guards the obvious wrong fix: keep the file untouched and lose the edit.
    """
    p = _cfg(tmp_path, REALISTIC)
    _write_plugins_enabled(p, ["agent-claude", "git", "hpc-launcher"])
    data = yaml.safe_load(p.read_text())
    assert data["plugins_enabled"] == ["agent-claude", "git", "hpc-launcher"], data


def test_disabling_removes_the_item_AND_its_own_comment(tmp_path) -> None:
    """A removed plugin takes its trailing comment with it.

    That comment was about that plugin; leaving it behind would attach it to
    whatever line followed, which is its own kind of wrong.
    """
    p = _cfg(tmp_path, REALISTIC)
    _write_plugins_enabled(p, ["agent-claude"])
    after = p.read_text()
    assert "guarded mode by default" not in after, (
        f"git was removed but its comment was orphaned:\n{after}")
    assert "stays on 'isolated'" in after, (
        f"removing git also destroyed the surviving plugin's comment:\n{after}")
    assert yaml.safe_load(after)["plugins_enabled"] == ["agent-claude"]


def test_the_result_is_still_valid_yaml(tmp_path) -> None:
    """Line-splicing can produce something that no longer parses.

    The whole file is re-read, not just the block, because a mangled block
    breaks everything after it.
    """
    p = _cfg(tmp_path, REALISTIC)
    _write_plugins_enabled(p, ["agent-claude", "hpc-launcher"])
    data = yaml.safe_load(p.read_text())
    assert data["agent"] == "claude"
    assert data["plugins"]["git"]["mode"] == "guarded", (
        "content AFTER the block was damaged")


def test_a_config_with_no_block_still_gets_one(tmp_path) -> None:
    """The hand-written-config path must keep working."""
    p = _cfg(tmp_path, "agent: claude\n")
    _write_plugins_enabled(p, ["git"])
    assert yaml.safe_load(p.read_text())["plugins_enabled"] == ["git"]


def test_disabling_everything_leaves_a_valid_empty_list(tmp_path) -> None:
    """`[]`, not a dangling `plugins_enabled:` with nothing under it."""
    p = _cfg(tmp_path, REALISTIC)
    _write_plugins_enabled(p, [])
    assert yaml.safe_load(p.read_text())["plugins_enabled"] in ([], None)


def test_emptying_the_list_and_refilling_it_stays_valid_yaml(tmp_path) -> None:
    """THE ROW-201 DEFECT, and the shape a mode switch actually takes.

    `auth use <mode>` disables the old agent plugin and enables the new one.
    When the agent plugin was the ONLY entry, the disable wrote our own empty
    marker `  []`, and the enable then treated that marker as user content and
    kept it — producing

        plugins_enabled:
          []
          - agent-claude-shared

    which is not YAML. The command exited 0 and `botainer config check` could
    no longer load the project at all. Driven through the real CLI on a scratch
    state root before the fix: rc 0, then "expected <block end>".
    """
    p = _cfg(tmp_path, "agent: claude\nplugins_enabled:\n  - agent-claude\n")
    _write_plugins_enabled(p, [])                       # the disable
    _write_plugins_enabled(p, ["agent-claude-shared"])  # the enable
    loaded = yaml.safe_load(p.read_text())              # raised before the fix
    assert loaded["plugins_enabled"] == ["agent-claude-shared"], p.read_text()
    assert "[]" not in p.read_text()


def test_the_writer_REFUSES_rather_than_writing_a_file_nobody_can_load(
        tmp_path, monkeypatch) -> None:
    """The structural half: this is a text editor for a YAML file.

    Splicing a block instead of round-tripping through a parser is deliberate —
    a parser eats the user's comments — but it means no parser ever sees the
    result, so any splicing bug becomes an unloadable config written by a
    command that exited 0. The writer now parses its own output first, and on a
    bad edit the ORIGINAL FILE IS UNTOUCHED. That is recoverable; the broken
    file was not.

    The bad block is injected rather than waited for: the point is that ANY
    future bug in the rebuild is caught, not just the one already fixed.
    """
    from botainer.core.refusal import Refused
    from botainer.plugins import lifecycle

    original = "agent: claude\nplugins_enabled:\n  - agent-claude\n"
    p = _cfg(tmp_path, original)
    monkeypatch.setattr(lifecycle, "_rebuild_block",
                        lambda body, enabled: "plugins_enabled:\n  []\n  - x\n")
    with pytest.raises(Refused) as exc:
        lifecycle._write_plugins_enabled(p, ["x"])
    assert "would not parse" in str(exc.value)
    assert p.read_text() == original, "the file must not have been touched"


def test_the_writer_REFUSES_when_the_result_would_say_the_wrong_thing(
        tmp_path, monkeypatch) -> None:
    """Parseable is not the same as correct.

    A rebuild that silently dropped or duplicated a name would produce perfectly
    valid YAML naming the wrong plugin set — which on this path decides which
    credential is mounted. So the check is not "does it parse" but "does it say
    what the caller asked for".
    """
    from botainer.core.refusal import Refused
    from botainer.plugins import lifecycle

    original = "agent: claude\nplugins_enabled:\n  - agent-claude\n"
    p = _cfg(tmp_path, original)
    monkeypatch.setattr(lifecycle, "_rebuild_block",
                        lambda body, enabled: "plugins_enabled:\n  - agent-claude\n")
    with pytest.raises(Refused) as exc:
        lifecycle._write_plugins_enabled(p, ["agent-claude-broker"])
    assert "agent-claude-broker" in str(exc.value)
    assert p.read_text() == original, "the file must not have been touched"


def test_a_mode_swap_is_ONE_write_with_no_agentless_state(tmp_path, monkeypatch) -> None:
    """`auth use <mode>` must never pass through "this project has no agent".

    It used to run `disable(old)` then `enable(new)`. Between those two writes
    the file says the project has NO agent plugin — a mode the user did not
    choose — and anything that stops the second write leaves it there, with the
    command having reported nothing. Observed by driving the two halves with the
    second one refusing: the project was left with `plugins_enabled: []`.

    So the assertion is not on the end state (both versions reach it) but on
    WHAT WAS WRITTEN: exactly one write, and no intermediate without an agent.
    """
    from botainer.plugins import lifecycle

    p = _cfg(tmp_path, "agent: claude\nplugins_enabled:\n  - agent-claude\n  - git\n")
    seen: list[list[str]] = []
    real = lifecycle._write_plugins_enabled
    monkeypatch.setattr(
        lifecycle, "_write_plugins_enabled",
        lambda path, enabled: (seen.append(list(enabled)), real(path, enabled))[1])

    lifecycle.swap(tmp_path, remove={"agent-claude"}, add={"agent-claude-shared"})

    assert len(seen) == 1, f"a swap must be one write, got {seen}"
    assert not any(
        not [n for n in state if n.startswith("agent-")] for state in seen), (
        f"a write left the project with no agent plugin: {seen}")
    assert yaml.safe_load(p.read_text())["plugins_enabled"] == [
        "git", "agent-claude-shared"]


def test_a_swap_keeps_the_comments_on_the_lines_that_STAY(tmp_path) -> None:
    from botainer.plugins import lifecycle

    p = _cfg(tmp_path, REALISTIC)
    lifecycle.swap(tmp_path, remove={"agent-claude"}, add={"agent-claude-broker"})
    text = p.read_text()
    assert "# guarded mode by default" in text
    assert "# - nudge" in text, "the commented opt-in menu must survive a swap"
    assert yaml.safe_load(text)["plugins_enabled"] == ["git", "agent-claude-broker"]


def test_a_config_that_ALREADY_lists_a_plugin_twice_can_still_be_edited(
        tmp_path) -> None:
    """The contents check compares SETS, so a repeat in the file is not fatal.

    `plugins_enabled` is a set of names: the model accepts a repeat and readers
    dedupe. A multiset comparison would refuse a caller that passed a deduped
    list against such a file — the writer would report a botainer bug at a user
    whose only sin was listing a plugin twice.

    STATED PLAINLY because I checked instead of assuming: no shipped caller hits
    this today. `enable`, `disable` and `swap` all build their list FROM the
    file, so duplicates appear on both sides and the multiset form passed as
    well. This pins the WRITER'S CONTRACT — "write exactly these names" — for
    the next caller that passes a set, which is the natural thing to do.
    """
    from botainer.plugins.lifecycle import _write_plugins_enabled

    p = _cfg(tmp_path,
             "agent: claude\nplugins_enabled:\n  - agent-claude\n  - agent-claude\n")
    _write_plugins_enabled(p, ["agent-claude", "git"])   # a DEDUPED list
    assert set(yaml.safe_load(p.read_text())["plugins_enabled"]) == {
        "agent-claude", "git"}


def test_auth_use_ITSELF_writes_once_driven_through_the_REAL_command(
        tmp_path, monkeypatch) -> None:
    """THE CALLER, not the helper. The checkpoint-11 tzar found this hole.

    `test_a_mode_swap_is_ONE_write_with_no_agentless_state` above calls
    `lifecycle.swap()` directly, so it proves the helper writes once and proves
    NOTHING about `auth use` using it. Reverting the caller to the old
    `disable()`-then-`enable()` pair — deleting the entire fix — passed the full
    suite, 3403 tests. That is this project's own "verify through the REAL
    caller" rule, broken in the commit right after it was written down again.

    So this drives `botainer auth use shared -y` through click and counts what
    the writer was asked to write.
    """
    from click.testing import CliRunner

    from botainer.cli.auth import auth
    from botainer.plugins import lifecycle

    root = tmp_path / "proj"
    (root / ".botainer").mkdir(parents=True)
    (root / ".botainer" / "project-id").write_text(
        "44444444-4444-4444-8444-444444444444\n")
    (root / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: docker\n"
        "profile: default\nnetwork:\n  mode: internet\n"
        "plugins_enabled:\n  - agent-claude\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.chdir(root)

    writes: list[list[str]] = []
    real = lifecycle._write_plugins_enabled
    monkeypatch.setattr(
        lifecycle, "_write_plugins_enabled",
        lambda path, enabled: (writes.append(list(enabled)), real(path, enabled))[1])

    result = CliRunner().invoke(auth, ["use", "shared", "-y"])

    assert result.exit_code == 0, result.output
    assert len(writes) == 1, (
        f"`auth use` wrote {len(writes)} times: {writes}. Two writes put this "
        f"project through a state with no agent plugin — a mode the user did "
        f"not choose — which is the whole point of lifecycle.swap()")
    assert not any(
        not [n for n in state if n.startswith("agent-")] for state in writes), (
        f"a write left the project with no agent plugin: {writes}")
