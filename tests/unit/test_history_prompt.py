"""The switch TELLS you your history is moving, and offers to bring it.

Driven through a real click command with real keystrokes rather than by
monkeypatching `click.confirm`, because the thing being tested is what a person
sees and types. A test that stubs the prompt asserts the plumbing and not the
experience, and the defect this fixes was entirely in the experience: the files
were always on disk, the user just had no way to know.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from botainer.core.history_carry import SET_ASIDE_NUDGE_AT, set_aside_siblings
from botainer.cli._history_prompt import (
    history_dir_for,
    offer_carry,
    warn_history_will_move,
)


def _out(result) -> str:
    """Everything the user saw. The prompts write to stderr on purpose.

    Warnings and questions belong on stderr so that piping a command's real
    output somewhere does not swallow the question — click 8.2 split the two
    streams in CliRunner, so both are joined here rather than assuming which
    one a given line landed on.
    """
    text = result.output or ""
    try:
        if result.stderr:
            text += result.stderr
    except ValueError:
        pass
    return text


def _make_history(root: Path, *, prompt: str = "reproduce the bug",
                  files: int = 4) -> Path:
    """A directory shaped like an agent config dir that has been used."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    (root / "history.jsonl").write_text('{"display":"x"}\n')
    (root / "settings.json").write_text('{"model":"opus"}')
    (root / "todos").mkdir(exist_ok=True)
    for i in range(files):
        (root / "todos" / f"t{i}.json").write_text("[]")
    proj = root / "projects" / "-workspace"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "sess.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": prompt}}) + "\n")
    return root


def _drive(source: Path, destination: Path, *, keys: str = "",
           assume_yes: bool = False):
    """Run offer_carry inside a real command, feeding it real input."""
    holder: dict[str, bool] = {}

    @click.command()
    def cmd() -> None:
        holder["proceed"] = offer_carry(
            source, destination, what_changed="auth mode",
            assume_yes=assume_yes)

    result = CliRunner().invoke(cmd, input=keys, catch_exceptions=False)
    return result, holder.get("proceed")


# ── the ordinary case: destination is empty ──────────────────────────────────


def test_accepting_the_offer_moves_the_history(tmp_path) -> None:
    src = _make_history(tmp_path / "profiles" / "default")
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, proceed = _drive(src, dst, keys="y\n")

    assert proceed is True
    assert (dst / "history.jsonl").exists()
    assert (dst / "projects" / "-workspace" / "sess.jsonl").exists()
    assert (dst / "todos" / "t0.json").exists()
    assert "Moved" in _out(result)
    # The move half: the source keeps its sign-in and loses its history, so
    # there is one live copy and never a "which of these is current?".
    assert (src / ".credentials.json").exists()
    assert not (src / "history.jsonl").exists()
    assert list(src.parent.glob("default.superseded-*")), (
        "the old copies must be renamed aside, not deleted"
    )


def test_the_offer_names_both_paths_before_asking(tmp_path) -> None:
    """The half that was missing: users were told history moved, never where.

    A swallowed AttributeError meant the old warning printed the sentence and
    never the paths, so the only actionable part never reached anyone.
    """
    src = _make_history(tmp_path / "profiles" / "default")
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, _ = _drive(src, dst, keys="n\n")
    text = _out(result)

    assert str(src) in text
    assert str(dst) in text


def test_declining_leaves_everything_and_says_where(tmp_path) -> None:
    src = _make_history(tmp_path / "profiles" / "default")
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, proceed = _drive(src, dst, keys="n\n")

    assert proceed is True, "declining the COPY must not cancel the switch"
    assert not (dst / "history.jsonl").exists()
    assert (src / "history.jsonl").exists()
    assert str(src) in _out(result)


def test_the_login_does_not_travel_and_the_user_is_told_so_first(tmp_path) -> None:
    """Otherwise "my history came but I'm logged out" is the next report.

    Phrased in the user's terms rather than ours: "your login and account
    details do not travel" and "sign in", not "credential" and "auth material".
    Someone deciding whether to press y should not have to know what botainer
    calls things.
    """
    src = _make_history(tmp_path / "profiles" / "default")
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, _ = _drive(src, dst, keys="y\n")

    assert not (dst / ".credentials.json").exists()
    text = _out(result).lower()
    assert "do not travel" in text, "the consequence must be stated, not implied"
    assert "sign in" in text, "and so must what the user has to do about it"


def test_the_account_details_are_named_as_staying_behind_too(tmp_path) -> None:
    """Not just the login. `claude.json` is reduced on the way, and a user who
    later finds their account unset should have been told, not surprised."""
    src = _make_history(tmp_path / "profiles" / "default")
    (src / "claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "someone@example.com"},
        "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
    }))
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, _ = _drive(src, dst, keys="y\n")
    text = _out(result).lower()

    carried = json.loads((dst / "claude.json").read_text())
    assert "oauthAccount" not in carried
    assert carried["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert "account details" in text
    assert "without the account details" in text, (
        "the post-copy line must say the settings arrived REDUCED, or a "
        "reduced file is reported as a faithful copy"
    )


def test_yes_flag_copies_without_asking(tmp_path) -> None:
    src = _make_history(tmp_path / "profiles" / "default")
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, proceed = _drive(src, dst, keys="", assume_yes=True)

    assert proceed is True
    assert (dst / "history.jsonl").exists()


def test_nothing_to_carry_says_nothing(tmp_path) -> None:
    """A notice reading "0 files" on every first switch is noise.

    The project has a standing rule about this: a warning that fires every time
    trains people to skip the whole channel, and then the real one is missed.
    """
    src = tmp_path / "profiles" / "default"
    src.mkdir(parents=True)
    (src / ".credentials.json").write_text("{}")     # login only, no history
    dst = tmp_path / "broker-state" / "default"
    dst.mkdir(parents=True)

    result, proceed = _drive(src, dst, keys="")

    assert proceed is True
    assert _out(result).strip() == ""


def test_same_directory_is_silent(tmp_path) -> None:
    """shared <-> isolated share a directory; there is no move to report."""
    src = _make_history(tmp_path / "profiles" / "default")
    result, proceed = _drive(src, src, keys="")
    assert proceed is True
    assert _out(result).strip() == ""


# ── the hard case: both sides hold history ───────────────────────────────────


def test_two_populated_dirs_show_both_with_a_recognisable_prompt(tmp_path) -> None:
    """Sizes let you compare; the opening prompt lets you RECOGNISE yours."""
    src = _make_history(tmp_path / "profiles" / "default",
                        prompt="reproduce the mode switch bug")
    dst = _make_history(tmp_path / "broker-state" / "default",
                        prompt="add the gpu flag to the apptainer adapter")

    result, _ = _drive(src, dst, keys="k\n")
    text = _out(result)

    assert "reproduce the mode switch bug" in text
    assert "add the gpu flag to the apptainer adapter" in text
    assert str(src) in text and str(dst) in text


def test_keeping_the_destination_moves_nothing(tmp_path) -> None:
    src = _make_history(tmp_path / "profiles" / "default", prompt="old work")
    dst = _make_history(tmp_path / "broker-state" / "default", prompt="new work")

    result, proceed = _drive(src, dst, keys="k\n")

    assert proceed is True
    assert "new work" in (dst / "projects" / "-workspace" / "sess.jsonl").read_text(), (
        "the destination's own transcript was replaced"
    )
    assert (src / "history.jsonl").exists()
    assert not list((tmp_path / "broker-state").glob("default.archived-*")), (
        "keeping must not archive anything"
    )


def test_archiving_renames_the_destination_and_carries_the_other(tmp_path) -> None:
    """Every byte survives: the archive is a rename, and the carry is a copy."""
    src = _make_history(tmp_path / "profiles" / "default", prompt="old work")
    dst = _make_history(tmp_path / "broker-state" / "default", prompt="new work")

    result, proceed = _drive(src, dst, keys="a\n")

    assert proceed is True
    archives = list((tmp_path / "broker-state").glob("default.archived-*"))
    assert len(archives) == 1
    assert "new work" in (
        archives[0] / "projects" / "-workspace" / "sess.jsonl").read_text()
    assert "old work" in (
        dst / "projects" / "-workspace" / "sess.jsonl").read_text()
    assert (src / ".credentials.json").exists(), "the sign-in stays put"
    assert "Archived" in _out(result)


# ── the warning, which runs BEFORE the caller's own confirm ──────────────────


def _drive_warn(source: Path, destination: Path):
    holder: dict[str, bool] = {}

    @click.command()
    def cmd() -> None:
        holder["said"] = warn_history_will_move(
            source, destination, what_changed="auth mode")

    result = CliRunner().invoke(cmd, input="", catch_exceptions=False)
    return result, holder.get("said")


def test_the_warning_comes_early_and_names_both_paths(tmp_path) -> None:
    """This is the abort opportunity.

    The copy happens AFTER the change is applied — so if the user is only told
    then, they have been told about a move they already agreed to, which is the
    original defect wearing a different hat. The warning is what makes "no" a
    real answer at the command's own confirm.
    """
    src = _make_history(tmp_path / "profiles" / "default")
    dst = tmp_path / "broker-state" / "default"

    result, said = _drive_warn(src, dst)
    text = _out(result)

    assert said is True
    assert str(src) in text and str(dst) in text
    assert "move it across" in text


def test_the_warning_flags_the_conflict_case_up_front(tmp_path) -> None:
    """"You will be asked" is a promise; it has to be made before the confirm."""
    src = _make_history(tmp_path / "profiles" / "default")
    dst = _make_history(tmp_path / "broker-state" / "default")

    result, said = _drive_warn(src, dst)
    text = _out(result)

    assert said is True
    assert "BOTH" in text
    assert "deleted or merged" in text


def test_the_warning_is_silent_when_there_is_no_history_to_move(tmp_path) -> None:
    """A notice that fires on every switch regardless trains people to skip it.

    The project has already been bitten by exactly this: a check that reported
    the same thing on fifteen consecutive commits and was read past every time.
    """
    src = tmp_path / "profiles" / "default"
    src.mkdir(parents=True)
    (src / ".credentials.json").write_text("{}")
    dst = tmp_path / "broker-state" / "default"

    result, said = _drive_warn(src, dst)

    assert said is False
    assert _out(result).strip() == ""


def test_yes_flag_never_silently_picks_a_side(tmp_path) -> None:
    """--yes must not resolve an ambiguity that costs the user transcripts.

    It takes the option that moves nothing, and says so — an automated run that
    cannot ask is not licence to choose which history survives.
    """
    src = _make_history(tmp_path / "profiles" / "default", prompt="old work")
    dst = _make_history(tmp_path / "broker-state" / "default", prompt="new work")

    result, proceed = _drive(src, dst, keys="", assume_yes=True)

    assert proceed is True
    assert not list((tmp_path / "broker-state").glob("default.archived-*"))
    assert "new work" in (
        dst / "projects" / "-workspace" / "sess.jsonl").read_text()
    assert (src / "history.jsonl").exists()
    text = _out(result)
    assert "--yes" in text and "moves nothing" in text


# ── the path derivation the whole thing hangs on ─────────────────────────────


@pytest.mark.parametrize("plugin,mode,profile,expected", [
    ("agent-claude", "isolated", "default", "agent-claude/profiles/default"),
    ("agent-claude-shared", "shared", "default", "agent-claude/profiles/default"),
    ("agent-claude-broker", "broker", "default", "agent-claude/broker-state/default"),
    ("agent-codex", "isolated", "work", "agent-codex/profiles/work"),
    ("agent-codex-broker", "broker", "work", "agent-codex/broker-state/work"),
])
def test_history_dir_matches_what_the_plugins_actually_bind(
        tmp_path, plugin, mode, profile, expected) -> None:
    """Pinned against the paths the hooks build, not against this function.

    If these drift apart the carry copies between two directories that no
    session ever reads, which looks like success and does nothing.
    """
    got = history_dir_for(tmp_path, "UID-1", plugin, mode, profile)
    assert got == tmp_path / "state" / "UID-1" / "data" / Path(expected)


def test_shared_and_isolated_resolve_to_the_same_directory(tmp_path) -> None:
    """Which is why switching between those two must stay silent."""
    a = history_dir_for(tmp_path, "U", "agent-claude", "isolated")
    b = history_dir_for(tmp_path, "U", "agent-claude-shared", "shared")
    assert a == b


# ── a live session blocks the switch entirely ───────────────────────────────


def test_a_live_session_refuses_the_switch_and_says_which(tmp_path, monkeypatch) -> None:
    """A refusal, not a warning, and not a prompt.

    The directory a switch relocates is the one a running session has bound at
    /home/agent/.claude. There is no version of "rename this running agent's
    transcripts anyway" that ends well, so offering it would be offering a
    mistake. This got sharper when the carry became a move: a copy at least left
    the running session's files where they were.
    """
    from botainer.cli import _history_prompt

    monkeypatch.setattr(_history_prompt, "live_sessions_for",
                        lambda _root: ["sess-abc123", "sess-def456"])

    @click.command()
    def cmd() -> None:
        assert _history_prompt.refuse_if_a_session_is_live(tmp_path) is True

    text = _out(CliRunner().invoke(cmd, catch_exceptions=False))
    assert "refused" in text
    assert "sess-abc123" in text, "name the sessions, or the user cannot act"
    assert "Nothing has been changed" in text


def test_no_live_session_means_no_refusal_and_no_noise(tmp_path, monkeypatch) -> None:
    """The common case must stay silent — a refusal channel that prints on
    every ordinary switch is one people learn to skip."""
    from botainer.cli import _history_prompt

    monkeypatch.setattr(_history_prompt, "live_sessions_for", lambda _root: [])

    @click.command()
    def cmd() -> None:
        assert _history_prompt.refuse_if_a_session_is_live(tmp_path) is False

    assert _out(CliRunner().invoke(cmd, catch_exceptions=False)).strip() == ""


def test_an_unresolvable_project_does_not_block_the_switch(tmp_path) -> None:
    """A project that has never been started has no state dir and no session
    records. Treating "cannot tell" as "live" would make the feature unusable
    on exactly the projects most likely to need it."""
    from botainer.cli._history_prompt import live_sessions_for

    assert live_sessions_for(tmp_path / "never-a-project") == []


def test_a_blocked_carry_says_WHY_instead_of_going_quiet(tmp_path) -> None:
    """A refusal the user is not told about is the original bug, again.

    A blocked plan carries nothing, so it used to fall through the
    "nothing to carry, stay silent" branch and print absolutely nothing — the
    switch looked clean and the history simply did not move. Found by running
    the symlinked-destination case through the real CLI and noticing the
    output was suspiciously tidy.
    """
    src = _make_history(tmp_path / "profiles" / "personal")
    dst = tmp_path / "profiles" / "work"
    dst.mkdir(parents=True)
    (dst / "projects").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    result, proceed = _drive(src, dst, keys="")
    text = _out(result)

    assert proceed is True, "the config change itself still stands"
    assert "NOT moved" in text
    assert "symbolic link" in text, "and the reason has to be the real one"
    assert str(src) in text, "and where the history actually is"


# ── the set-aside copies pile up, and nothing ever removes one ───────────────
#
# Repeated carries can accumulate set-aside copies. The prompt should
# disclose that accumulation and explain how to inspect those copies.
#
# Driven through the real prompt with real keystrokes, like everything else in
# this file: the nudge IS the behaviour — there is no other observable effect,
# because it deliberately deletes nothing. So the test asserts on what the user
# sees, and pairs each with the on-disk count that produced it.


def _seed_set_asides(profile_dir: Path, n: int) -> None:
    """`n` earlier set-aside copies of `profile_dir`, as a switch would leave."""
    for i in range(n):
        (profile_dir.parent /
         f"{profile_dir.name}.superseded-2026080{i + 1}T000000Z").mkdir(
            parents=True, exist_ok=True)


def test_no_nudge_while_the_copies_are_few(tmp_path) -> None:
    """Silence below the threshold. A notice on every single switch is the
    "warn that fires on every commit" failure: people learn to skip the
    channel, and the one that matters goes unread with it."""
    src = _make_history(tmp_path / "profiles" / "personal")
    dst = tmp_path / "profiles" / "work"
    dst.mkdir(parents=True)
    _seed_set_asides(src, SET_ASIDE_NUDGE_AT - 2)   # this switch makes one more

    result, _ = _drive(src, dst, keys="y\n")
    text = _out(result)

    assert "Moved" in text, "the carry itself still happened"
    assert len(set_aside_siblings(src)) == SET_ASIDE_NUDGE_AT - 1
    assert "set-aside history copies" not in text
    assert "botainer where" not in text


def test_the_nudge_fires_when_they_reach_the_threshold(tmp_path) -> None:
    src = _make_history(tmp_path / "profiles" / "personal")
    dst = tmp_path / "profiles" / "work"
    dst.mkdir(parents=True)
    _seed_set_asides(src, SET_ASIDE_NUDGE_AT - 1)   # this switch makes the Nth

    result, _ = _drive(src, dst, keys="y\n")
    text = _out(result)

    assert len(set_aside_siblings(src)) == SET_ASIDE_NUDGE_AT
    assert f"{SET_ASIDE_NUDGE_AT} set-aside history copies" in text, (
        "the count is the whole point — 'several' is not actionable"
    )
    assert "botainer never\n  removes them" in text, (
        "and that nothing cleans them up, which is why it is worth saying"
    )
    # Never hand someone a command without saying where to run it: inside a
    # session there is no shell prompt at all, only the agent.
    assert "botainer where" in text
    assert "HOST shell" in text
    # Points at the command that shows sizes AND the restore lines, rather than
    # printing `rm -rf` at the moment the user is least likely to read it.
    assert "rm -rf" not in text


def test_the_count_is_this_profiles_copies_only(tmp_path) -> None:
    """A neighbour's set-asides must not inflate it.

    `personal-2.superseded-…` starts with `personal`, so a prefix match would
    fold it in and nag about directories the user cannot find under the name
    they were given.
    """
    src = _make_history(tmp_path / "profiles" / "personal")
    dst = tmp_path / "profiles" / "work"
    dst.mkdir(parents=True)
    _seed_set_asides(src, 1)
    _seed_set_asides(tmp_path / "profiles" / "personal-2", SET_ASIDE_NUDGE_AT)

    result, _ = _drive(src, dst, keys="y\n")

    assert len(set_aside_siblings(src)) == 2
    assert "set-aside history copies" not in _out(result)
