"""The carry moves history and REFUSES to move credentials.

These tests are written against the three properties the module docstring
claims, because those are the ones a future edit could quietly lose:

  1. a credential is never carried,
  2. a symlink is never carried at any depth,
  3. nothing is deleted or overwritten.

Each is checked with a fixture that looks like a real agent config directory
rather than the minimum that makes the code path run — a directory holding one
file where reality holds twenty is how three wrong conclusions were reached in
one week (CLAUDE.md, "a fixture must match a real install").
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from botainer.core.history_carry import (
    CREDENTIAL_FILENAMES,
    archive_dir,
    describe_dir,
    execute_carry,
    has_history,
    plan_carry,
)


def _realistic_claude_dir(root: Path, *, with_credential: bool = True) -> Path:
    """A directory shaped like a real ~/.claude, not a stub.

    A representative layout with synthetic content: config JSON, prompt
    history, session transcripts, todos, user-level subagents, settings,
    shell snapshots and caches. Carry must decide correctly for each kind;
    a fixture containing only two files cannot demonstrate that.
    """
    root.mkdir(parents=True, exist_ok=True)
    if with_credential:
        (root / ".credentials.json").write_text('{"claudeAiOauth": {"x": 1}}')
    (root / "claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "someone@example.com"},
        "userID": "abc123",
        "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
        "numStartups": 42,
    }))
    (root / "history.jsonl").write_text('{"display":"do the thing"}\n')
    (root / "settings.json").write_text('{"model": "opus"}')
    (root / ".last-cleanup").write_text("1787842919")
    (root / "stats-cache.json").write_text("{}")

    proj = root / "projects" / "-workspace"
    proj.mkdir(parents=True)
    (proj / "705b1a05.jsonl").write_text(
        json.dumps({"type": "user",
                    "message": {"content": "synthetic history fixture"}})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"content": "ok"}}) + "\n"
    )

    (root / "todos").mkdir()
    (root / "todos" / "705b1a05.json").write_text("[]")
    (root / "agents").mkdir()
    (root / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n")
    (root / "shell-snapshots").mkdir()
    (root / "shell-snapshots" / "snapshot-zsh-1.sh").write_text("# snapshot\n")
    (root / "file-history").mkdir()
    (root / "file-history" / "abc.json").write_text("{}")
    (root / "cache").mkdir()
    (root / "cache" / "blob").write_bytes(b"\x00" * 128)
    return root


def _carried_names(plan) -> set[str]:
    return {p.name for p in plan.carry}


def _withheld_names(plan) -> set[str]:
    return {w.path.name for w in plan.withheld}


# ── property 1: a credential is never carried ────────────────────────────────


@pytest.mark.parametrize("credname", sorted(CREDENTIAL_FILENAMES))
def test_no_credential_name_is_ever_carried(tmp_path, credname) -> None:
    """Every credential filename the project knows about, one per run.

    Parametrised over the real set rather than spot-checking `.credentials.json`
    so that adding a name to CREDENTIAL_FILENAMES automatically gets a test —
    the alternative is a set that grows and a test that does not.
    """
    src = _realistic_claude_dir(tmp_path / "src", with_credential=False)
    (src / credname).write_text("SECRET-TOKEN-VALUE")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert credname not in _carried_names(plan)
    assert not (dst / credname).exists(), (
        f"{credname} reached the destination — a mode switch would carry the "
        f"credential into a directory whose contract may forbid one"
    )
    assert credname in _withheld_names(plan), (
        "withholding silently is its own defect: the user must be told why "
        "they have to log in again"
    )


def test_credential_backups_and_locks_are_withheld_too(tmp_path) -> None:
    """`.pre-shared` backups ARE credentials; a lock guards the wrong file.

    These names are DERIVED elsewhere in the codebase (the refresh lock is built
    from the credential filename), so they can never be covered by a literal
    list — the suffix rule is what covers them and this is what pins it.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    (src / ".credentials.json.pre-shared").write_text("OLD-TOKEN")
    (src / "..credentials.json.refresh.lock").write_text("")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not list(dst.glob("*pre-shared*"))
    assert not list(dst.glob("*refresh.lock*"))


def test_a_nested_credential_is_withheld_not_just_a_top_level_one(tmp_path) -> None:
    """Depth must not be a way past the rule.

    A credential that ends up one directory down — a backup tool, a stray copy,
    a future upstream layout change — is the same credential. Checking only the
    top level would be a filter that holds until the layout moves.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    (src / "backups").mkdir()
    (src / "backups" / ".credentials.json").write_text("SECRET")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not (dst / "backups" / ".credentials.json").exists()


# ── property 2: a symlink is never carried, at any depth ─────────────────────


def test_the_shared_mode_credential_symlink_is_refused(tmp_path) -> None:
    """The specific hazard this rule exists for.

    Shared mode puts a `.credentials.json` SYMLINK in the per-project directory
    pointing at the host-wide store. Copying that link into an isolated or
    broker directory would re-share the credential while every surface still
    said "isolated". Refusing symlinks makes that unrepresentable.
    """
    shared_store = tmp_path / "shared-auth" / "agent-claude"
    shared_store.mkdir(parents=True)
    (shared_store / ".credentials.json").write_text("SHARED-TOKEN")

    src = _realistic_claude_dir(tmp_path / "src", with_credential=False)
    (src / ".credentials.json").symlink_to(shared_store / ".credentials.json")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not (dst / ".credentials.json").exists()
    assert not (dst / ".credentials.json").is_symlink()


def test_a_symlink_whose_name_is_not_a_credential_is_still_refused(tmp_path) -> None:
    """The symlink rule must stand on its own, not lean on the name rule.

    Caught by mutation: deleting the symlink check left every test passing,
    because the only symlink under test was named `.credentials.json` and the
    credential-name rule caught it anyway. So the symlink property was untested.

    `claude.json` is the case that matters. Shared mode symlinks it into the
    host-wide store too — it holds the account state, not a token, so no name
    rule touches it. Copying that link into an isolated or broker directory
    would leave the "isolated" mode reading and WRITING the shared store's
    account state through a link nothing in the surface mentions.
    """
    shared_store = tmp_path / "shared-auth" / "agent-claude"
    shared_store.mkdir(parents=True)
    (shared_store / "claude.json").write_text('{"oauthAccount": {"x": 1}}')

    src = _realistic_claude_dir(tmp_path / "src")
    (src / "claude.json").unlink()
    (src / "claude.json").symlink_to(shared_store / "claude.json")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not (dst / "claude.json").exists(), (
        "a symlink into the shared store was copied into another mode"
    )
    assert "claude.json" in _withheld_names(plan), (
        "and the user was not told their account state stayed behind"
    )


def test_a_symlinked_directory_is_not_followed_and_is_reported(tmp_path) -> None:
    """Following one would copy files from outside the source entirely."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not history")

    src = _realistic_claude_dir(tmp_path / "src")
    (src / "linkdir").symlink_to(outside, target_is_directory=True)
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not (dst / "linkdir").exists()
    assert not (dst / "private.txt").exists()
    assert "linkdir" in _withheld_names(plan)


# ── property 3: nothing is deleted or overwritten ────────────────────────────


def test_a_destination_holding_anything_of_its_own_is_protected(tmp_path) -> None:
    """The first line of defence: a populated destination blocks the carry.

    Written after the second line (below) was the only one being tested — the
    plan blocks before it ever reaches the per-file check, so a test that put a
    file in the destination and then asserted the per-file behaviour was
    actually asserting this.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "settings.json").write_text('{"model": "sonnet"}')

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert plan.blocked
    assert (dst / "settings.json").read_text() == '{"model": "sonnet"}'
    assert not (dst / "history.jsonl").exists()


def test_a_file_that_appears_after_planning_is_still_not_overwritten(tmp_path) -> None:
    """The second line of defence, and the one that makes the claim TRUE.

    Planning reads the destination; copying writes it. Between the two, a
    session can start and write the very file about to be copied. A pre-check
    would say "absent" and then clobber it. The copy therefore uses an exclusive
    create, so "never overwrites" holds regardless of what happened in between —
    a property rather than a check that was correct a moment ago.

    Replace the O_EXCL open in execute_carry with a plain copy and this fails.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    assert Path("settings.json") in plan.carry

    # ... the destination gains the file after the plan was computed.
    (dst / "settings.json").write_text('{"model": "sonnet"}')

    report = execute_carry(plan)

    assert (dst / "settings.json").read_text() == '{"model": "sonnet"}', (
        "the destination's own settings were replaced by the source's"
    )
    assert Path("settings.json") in [w.path for w in report.failed]
    assert (dst / "history.jsonl").exists(), (
        "one collision must not abort the rest of the carry"
    )


def test_the_source_still_has_everything_afterwards(tmp_path) -> None:
    """Carry is a COPY. If the user switches back, the old mode is intact."""
    src = _realistic_claude_dir(tmp_path / "src")
    before = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
    dst = tmp_path / "dst"
    dst.mkdir()

    execute_carry(plan_carry(src, dst))

    after = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
    assert after == before


# ── the carry actually carries ───────────────────────────────────────────────


def test_the_history_set_arrives_intact(tmp_path) -> None:
    """The positive case, stated file by file.

    Named individually rather than asserting a count, because a count passes
    just as happily when the wrong twelve files arrive.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    report = execute_carry(plan_carry(src, dst))

    for rel in (
        "claude.json",
        "history.jsonl",
        "settings.json",
        "projects/-workspace/705b1a05.jsonl",
        "todos/705b1a05.json",
        "agents/reviewer.md",
        "shell-snapshots/snapshot-zsh-1.sh",
        "file-history/abc.json",
    ):
        assert (dst / rel).exists(), f"{rel} did not arrive"

    assert (dst / "projects" / "-workspace" / "705b1a05.jsonl").read_text() == (
        src / "projects" / "-workspace" / "705b1a05.jsonl").read_text()
    assert report.bytes_copied > 0
    assert not report.failed


def test_the_project_config_travels_but_the_account_does_not(tmp_path) -> None:
    """`claude.json` is reduced on the way, not copied and not skipped.

    An earlier cut of this carried the file WHOLE, on the reasoning that
    `oauthAccount` is account state rather than a credential and that without it
    a session sees a token but no account and prompts for login. That reasoning
    was wrong in its own terms: the carry does not copy the credential either, so
    the destination logs in regardless — and a directory that names an account it
    cannot authenticate as is worse than one that names none.

    §4cm settles it: a profile IS an account boundary, and there is no sound
    same-account signal on disk. So identity stays put and the project config —
    which is what the user actually notices losing — comes across.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    execute_carry(plan_carry(src, dst))
    carried = json.loads((dst / "claude.json").read_text())

    assert carried["projects"]["/workspace"]["hasTrustDialogAccepted"] is True, (
        "the project's own config — MCP servers, allowed tools, the trust "
        "answer — is the substance of this file and must travel"
    )
    assert "oauthAccount" not in carried
    assert "userID" not in carried


@pytest.mark.parametrize("key", [
    "oauthAccount",                      # the account itself
    "userID",
    "cachedExtraUsageDisabledReason",    # an ORG-level flag
    "passesEligibilityCache",            # entitlement caches, six of them
    "cachedUsageUtilization",
    "modelAccessCache",
    "orgModelDefaultCache",
    "additionalModelCostsCache",
    "passesLastSeenRemaining",
])
def test_no_account_or_entitlement_key_crosses_the_boundary(tmp_path, key) -> None:
    """Every account/entitlement key observed in a real install, one per run.

    Enumerated from an actual `~/.claude.json` rather than guessed, because the
    interesting ones are not the obvious ones: `cachedExtraUsageDisabledReason`
    holds an org-level policy string, and the entitlement caches would show a
    work account's model access inside a personal profile.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    blob = json.loads((src / "claude.json").read_text())
    blob[key] = {"marker": "ACCOUNT-SCOPED"} if key.endswith("Cache") else "ACCOUNT-SCOPED"
    (src / "claude.json").write_text(json.dumps(blob))
    dst = tmp_path / "dst"
    dst.mkdir()

    execute_carry(plan_carry(src, dst))

    assert "ACCOUNT-SCOPED" not in (dst / "claude.json").read_text(), (
        f"{key} crossed a profile boundary; §4cm calls that boundary an ACCOUNT "
        f"boundary and a broker self-heal that crossed it was reverted"
    )


def test_an_unparseable_config_file_carries_nothing_rather_than_everything(
        tmp_path) -> None:
    """A file we cannot read is one whose account keys we cannot find.

    The tempting fallback — copy it verbatim when the reduction fails — is
    exactly backwards: it hands the whole file over precisely in the case where
    we could not verify what is in it.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    (src / "claude.json").write_text('{"oauthAccount": {"x": 1}, TRUNCATED')
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not (dst / "claude.json").exists()
    assert "claude.json" in _withheld_names(plan)
    assert (dst / "history.jsonl").exists(), "the rest of the carry still runs"


def test_the_dotted_filename_is_handled_too(tmp_path) -> None:
    """Claude Code has used both `claude.json` and `.claude.json` in the config
    dir depending on version. Handling one and not the other would carry the
    account wholesale on whichever version used the name nobody matched."""
    src = _realistic_claude_dir(tmp_path / "src")
    (src / "claude.json").rename(src / ".claude.json")
    dst = tmp_path / "dst"
    dst.mkdir()

    execute_carry(plan_carry(src, dst))

    assert "oauthAccount" not in json.loads((dst / ".claude.json").read_text())


def test_ordinary_config_files_are_carried_verbatim(tmp_path) -> None:
    """Only the one mixed file is reduced. `settings.json` is the user's own
    configuration — model, permissions, hooks — and carries no account, so
    reducing it would lose settings for no benefit."""
    src = _realistic_claude_dir(tmp_path / "src")
    (src / "settings.json").write_text('{"model": "opus", "hooks": {"x": 1}}')
    dst = tmp_path / "dst"
    dst.mkdir()

    execute_carry(plan_carry(src, dst))

    assert (dst / "settings.json").read_text() == '{"model": "opus", "hooks": {"x": 1}}'
    assert (dst / "agents" / "reviewer.md").exists(), (
        "the user's own subagents travel too — they are project work, not "
        "a function of how the credential is delivered"
    )


# ── the blocked case: both sides hold history ────────────────────────────────


def test_two_populated_directories_block_rather_than_merge(tmp_path) -> None:
    """Merging would interleave two separate sets of transcripts.

    Neither is safe to prefer automatically, so the carry declines and the
    caller has to ask. This is the named case (Q~009): tell the user, and let
    them choose which copy to keep and which to archive.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = _realistic_claude_dir(tmp_path / "dst")

    plan = plan_carry(src, dst)

    assert plan.blocked
    assert plan.carry == []
    assert plan.blocked_reason

    report = execute_carry(plan)
    assert report.copied == [], "a blocked plan must not copy anything"


def test_a_freshly_logged_in_destination_is_not_a_conflict(tmp_path) -> None:
    """A directory holding ONLY a credential has no history.

    Otherwise the very first switch — log in to the new mode, then switch —
    would always look like a conflict and the feature would never fire.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / ".credentials.json").write_text('{"claudeAiOauth": {}}')

    assert not has_history(dst)
    plan = plan_carry(src, dst)
    assert not plan.blocked
    assert plan.carry


def test_has_history_is_false_for_a_missing_or_empty_directory(tmp_path) -> None:
    assert not has_history(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    assert not has_history(tmp_path / "empty")


def test_same_directory_is_blocked(tmp_path) -> None:
    """shared <-> isolated share a directory; there is nothing to carry."""
    src = _realistic_claude_dir(tmp_path / "src")
    plan = plan_carry(src, src)
    assert plan.blocked


def test_a_missing_source_is_not_an_error(tmp_path) -> None:
    """First use of a mode: nothing to carry, and that is normal, not a fault."""
    dst = tmp_path / "dst"
    dst.mkdir()
    plan = plan_carry(tmp_path / "never-existed", dst)
    assert not plan.blocked
    assert plan.is_empty
    assert plan.blocked_reason


# ── archive: the way out of a blocked carry ──────────────────────────────────


def test_archive_renames_and_keeps_every_byte(tmp_path) -> None:
    d = _realistic_claude_dir(tmp_path / "dst")
    names_before = sorted(p.relative_to(d) for p in d.rglob("*") if p.is_file())

    archived = archive_dir(d, now=datetime(2026, 8, 27, 22, 5, 0, tzinfo=timezone.utc))

    assert not d.exists()
    assert archived.name == "dst.archived-20260827T220500Z"
    assert sorted(p.relative_to(archived)
                  for p in archived.rglob("*") if p.is_file()) == names_before


def test_two_archives_in_the_same_second_do_not_collide(tmp_path) -> None:
    """Otherwise the second rename would land on the first and lose it."""
    stamp = datetime(2026, 8, 27, 22, 5, 0, tzinfo=timezone.utc)
    first = archive_dir(_realistic_claude_dir(tmp_path / "d"), now=stamp)
    second = archive_dir(_realistic_claude_dir(tmp_path / "d"), now=stamp)

    assert first.exists() and second.exists()
    assert first != second


def test_archive_then_carry_unblocks(tmp_path) -> None:
    """The whole recovery path, end to end."""
    src = _realistic_claude_dir(tmp_path / "src")
    dst = _realistic_claude_dir(tmp_path / "dst")
    assert plan_carry(src, dst).blocked

    archive_dir(dst)
    dst.mkdir()

    plan = plan_carry(src, dst)
    assert not plan.blocked
    report = execute_carry(plan)
    assert (dst / "history.jsonl").exists()
    assert report.copied


# ── describe: what the user needs to choose between two directories ──────────


def test_describe_reports_facts_including_a_recognisable_prompt(tmp_path) -> None:
    """Sizes let you compare; the prompt lets you RECOGNISE which is yours."""
    d = _realistic_claude_dir(tmp_path / "src")

    info = describe_dir(d)

    assert info["exists"] is True
    assert int(info["files"]) >= 10
    assert int(info["bytes"]) > 0
    assert info["modified"]
    assert info["last_prompt"] == "synthetic history fixture"


def test_describe_survives_a_corrupt_transcript(tmp_path) -> None:
    """Transcripts are written by the agent, i.e. untrusted input here.

    A truncated or hostile line must not take down the command that is trying to
    help the user avoid losing their work.
    """
    d = _realistic_claude_dir(tmp_path / "src")
    bad = d / "projects" / "-workspace" / "corrupt.jsonl"
    bad.write_text("{not json at all\n\x00\x00\n")
    import os
    os.utime(bad, (2 ** 31, 2 ** 31))  # newest, so it is tried first

    info = describe_dir(d)
    assert info["exists"] is True
    assert info["last_prompt"] in (None, "synthetic history fixture")


def test_describe_of_a_missing_directory_says_so_without_raising(tmp_path) -> None:
    info = describe_dir(tmp_path / "gone")
    assert info["exists"] is False
    assert info["files"] == 0


# ── drift guard against the auth CLI's own credential registry ───────────────


def test_credential_names_cover_everything_the_auth_cli_knows(tmp_path) -> None:
    """The duplication is deliberate; this is what stops it drifting.

    `botainer.cli.auth` owns the per-family credential filenames. Core cannot
    import the CLI without inverting the layering, so instead the names are
    restated here and pinned by reading the auth registries directly. If auth
    learns a new credential filename and the carry does not, this fails — which
    is the failure we want, because the silent alternative is that filename
    riding along in a carry.
    """
    from botainer.cli import auth as auth_cli

    known: set[str] = set()
    for family in ("anthropic", "openai"):
        known.add(auth_cli._family_creds_filename(family))
        known.update(auth_cli._family_isolated_creds_filenames(family))

    missing = known - set(CREDENTIAL_FILENAMES)
    assert not missing, (
        f"botainer.cli.auth knows credential filenames the carry would happily "
        f"copy: {sorted(missing)}. Add them to CREDENTIAL_FILENAMES."
    )


# ── the carry is a MOVE: one live copy, and no "which is current?" ───────────


def test_the_source_keeps_its_credential_but_not_its_history(tmp_path) -> None:
    """The point of the move, stated as the two halves it has to get right.

    Credential stays, so switching away and back still finds you signed in.
    History goes, so there is exactly one live copy and "which of these two is
    current?" is never a question the user has to answer from timestamps.
    """
    from botainer.core.history_carry import supersede_carried

    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    report = execute_carry(plan)
    superseded = supersede_carried(plan, report)

    assert (src / ".credentials.json").exists(), (
        "the credential must stay — it is what makes switching back and forth "
        "not require a fresh login every time"
    )
    assert not (src / "history.jsonl").exists()
    assert not (src / "projects" / "-workspace" / "705b1a05.jsonl").exists()
    assert (dst / "history.jsonl").exists()
    assert superseded is not None


def test_the_moved_files_are_renamed_aside_not_deleted(tmp_path) -> None:
    """Every byte still on disk afterwards. A move that goes wrong must be
    recoverable, and the project's standing "nothing is deleted" property has
    to survive turning a copy into a move."""
    from botainer.core.history_carry import supersede_carried

    src = _realistic_claude_dir(tmp_path / "src")
    original = (src / "projects" / "-workspace" / "705b1a05.jsonl").read_text()
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    superseded = supersede_carried(plan, execute_carry(plan))

    assert (superseded / "history.jsonl").exists()
    assert (superseded / "projects" / "-workspace" / "705b1a05.jsonl"
            ).read_text() == original


def test_a_file_that_failed_to_copy_is_not_moved_away(tmp_path) -> None:
    """Moving a file the destination never received would be losing it.

    Reproduced through the real race the exclusive create defends against: the
    destination gains the file between planning and copying, so that one copy
    fails while the rest succeed.
    """
    from botainer.core.history_carry import supersede_carried

    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    plan = plan_carry(src, dst)
    (dst / "settings.json").write_text('{"model": "sonnet"}')

    report = execute_carry(plan)
    supersede_carried(plan, report)

    assert Path("settings.json") in [w.path for w in report.failed]
    assert (src / "settings.json").exists(), (
        "the one file that did not make it must stay in the source"
    )
    assert not (src / "history.jsonl").exists(), "the ones that did, moved"


def test_a_declined_carry_moves_nothing(tmp_path) -> None:
    """supersede is driven by what execute_carry actually wrote, so a plan that
    was never executed cannot strip the source."""
    from botainer.core.history_carry import CarryReport, supersede_carried

    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    assert supersede_carried(plan_carry(src, dst), CarryReport()) is None
    assert (src / "history.jsonl").exists()


def test_the_emptied_skeleton_is_pruned_but_the_source_survives(tmp_path) -> None:
    """Otherwise the source keeps an empty projects/, todos/ and agents/ tree
    that reads as "there is still something here"."""
    from botainer.core.history_carry import supersede_carried

    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    supersede_carried(plan, execute_carry(plan))

    assert src.is_dir(), "the source dir itself must never be removed"
    assert not (src / "projects").exists()
    assert not (src / "todos").exists()


def test_a_round_trip_leaves_exactly_one_live_copy(tmp_path) -> None:
    """The whole point, end to end.

    Switch A->B, work in B, switch back. With a copy this ends in a two-sided
    conflict the user has to adjudicate from mtimes. With a move, B's newer
    history simply comes home and A was empty when it arrived.
    """
    from botainer.core.history_carry import supersede_carried

    a = _realistic_claude_dir(tmp_path / "a")
    b = tmp_path / "b"
    b.mkdir()

    plan = plan_carry(a, b)
    supersede_carried(plan, execute_carry(plan))

    # ... work happens in b.
    (b / "projects" / "-workspace" / "newer.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "work done in b"}}))

    back = plan_carry(b, a)
    assert not back.blocked, (
        "A was emptied by the outbound move, so coming home is not a conflict"
    )
    supersede_carried(back, execute_carry(back))
    assert (a / "projects" / "-workspace" / "newer.jsonl").exists()
    assert (a / "history.jsonl").exists()


def test_a_source_with_no_credential_is_not_removed_by_the_prune(tmp_path) -> None:
    """broker-state holds NO credential, so a move can empty it completely.

    Exposed by mutation: removing the `here == root` guard left every test
    green, because every fixture had a credential keeping the source
    non-empty and `rmdir` refuses a non-empty directory. The one directory in
    the system that is *defined* by holding no credential is exactly the one
    that would have been removed.
    """
    from botainer.core.history_carry import supersede_carried

    src = _realistic_claude_dir(tmp_path / "broker-state", with_credential=False)
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    supersede_carried(plan, execute_carry(plan))

    assert src.is_dir(), (
        "the source directory itself was removed; the next session's hook "
        "would recreate it, but botainer must not delete a directory it was "
        "only asked to move the contents of"
    )


# ── the DESTINATION is hostile too: it is the dir the agent writes to ────────


def test_a_symlink_planted_in_the_destination_cannot_redirect_the_carry(
        tmp_path) -> None:
    """The escape this file did not cover until it was demonstrated.

    Every symlink test above guards the SOURCE. The DESTINATION is the config
    directory bound at /home/agent/.claude — the agent writes there — and it was
    never checked at all. A session that leaves

        <destination>/projects -> /somewhere/else

    behind made the carry write agent-controlled content to an agent-chosen path
    outside the destination, because mkdir(parents=True, exist_ok=True) accepts
    an existing symlink.

    Two of this module's own rules combined to hide it: symlinks are skipped
    when walking, so a destination holding NOTHING BUT a planted link reports no
    history, so `has_history` said "no conflict" and the carry proceeded.
    """
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    (outside / "untouched.txt").write_text("HOST CONTENT")

    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "projects").symlink_to(outside, target_is_directory=True)

    plan = plan_carry(src, dst)
    execute_carry(plan)

    assert not (outside / "-workspace").exists(), (
        "the carry wrote through a symlink planted in the destination"
    )
    assert (outside / "untouched.txt").read_text() == "HOST CONTENT"
    assert plan.blocked, (
        "and it must REFUSE rather than silently carry a partial set"
    )
    assert "symbolic link" in plan.blocked_reason


def test_the_refusal_beats_the_no_history_check_to_it(tmp_path) -> None:
    """Ordering matters, and getting it wrong reopens the hole.

    A destination holding only a planted link has NO regular files, so
    has_history() is False and the ordinary path would proceed cheerfully. The
    symlink refusal therefore has to come first.
    """
    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "projects").symlink_to(tmp_path / "elsewhere",
                                  target_is_directory=True)

    assert not has_history(dst), "precondition: it looks empty"
    assert plan_carry(src, dst).blocked


def test_a_symlink_appearing_after_the_plan_still_cannot_redirect(tmp_path) -> None:
    """The refusal is a courtesy; the write itself must be unredirectable.

    Planning reads the destination and copying writes it. A session can plant
    the link in between, so the guarantee cannot rest on the pre-check — each
    component is opened relative to the one above it with O_NOFOLLOW, and there
    is no window between the check and the open.
    """
    outside = tmp_path / "somewhere-else"
    outside.mkdir()

    src = _realistic_claude_dir(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    plan = plan_carry(src, dst)
    assert not plan.blocked, "clean at plan time"

    # ... the agent plants it now.
    (dst / "projects").symlink_to(outside, target_is_directory=True)

    report = execute_carry(plan)

    assert not (outside / "-workspace").exists(), (
        "O_NOFOLLOW on each path component is what makes this impossible; a "
        "pre-check alone would be a TOCTOU race"
    )
    assert any("projects" in str(f.path) for f in report.failed)
    assert (dst / "history.jsonl").exists(), "the rest of the carry still runs"


def test_describe_reports_the_HISTORY_not_the_whole_directory(tmp_path) -> None:
    """The conflict prompt asks "which of these two is current?" and answers it
    with a file count, a size and a last-written time. Counting the credential
    breaks all three — and the mtime worst of all, because a credential
    rewritten seconds ago makes a directory of month-old transcripts look
    freshly used.

    Observed by running a real switch between two profiles whose transcripts
    were a month apart: both reported the same instant, because both had a
    credential written moments before.
    """
    d = _realistic_claude_dir(tmp_path / "src")
    old = 1_700_000_000                      # transcripts from long ago
    for rel in ("history.jsonl", "claude.json", "settings.json",
                "projects/-workspace/705b1a05.jsonl"):
        os.utime(d / rel, (old, old))
    for sub in ("todos/705b1a05.json", "agents/reviewer.md",
                "shell-snapshots/snapshot-zsh-1.sh", "file-history/abc.json",
                "cache/blob", ".last-cleanup", "stats-cache.json"):
        os.utime(d / sub, (old, old))
    # ... and a credential written just now.
    (d / ".credentials.json").write_text('{"claudeAiOauth": {"fresh": 1}}')

    info = describe_dir(d)

    assert info["modified"].startswith("2023-"), (
        f"the credential's mtime dominated the answer: {info['modified']}"
    )
    files_on_disk = sum(1 for p in d.rglob("*") if p.is_file())
    assert int(info["files"]) == files_on_disk - 1, (
        "the credential is not part of what would move, so counting it "
        "overstates what the user is choosing between"
    )


def test_codex_config_toml_is_never_carried_out_of_broker_mode(tmp_path) -> None:
    """A broker `config.toml` in `profiles/` would send the REAL key to a dead port.

    agent-codex-broker writes `config.toml` into `broker-state/<profile>/`
    naming the per-session broker as codex's model provider, with
    `env_key = "OPENAI_API_KEY"`. In broker mode that variable holds a
    SENTINEL, so the routing is harmless wherever it points.

    Both `broker-state/<p>` and `profiles/<p>` bind to /home/agent/.codex, so a
    broker -> isolated/shared switch carries files between them. Carry this one
    and every term flips: the broker's ephemeral port is dead and free for
    anyone to bind, and `OPENAI_API_KEY` in mount mode is the REAL key. Nothing
    in mount mode rewrites the file, so the next session hands the credential to
    whoever owns that port — on a shared login node, any co-tenant.

    Found by review on the day config.toml was introduced (2026-09-04). The
    plugin documents the file as agent-editable precisely BECAUSE a redirected
    sentinel is harmless; the carry is what breaks that argument, by delivering
    an agent-authored base_url next to the real credential.
    """
    src = tmp_path / "broker-state" / "default"
    dst = tmp_path / "profiles" / "default"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "config.toml").write_text(
        'model_provider = "botainer-broker"\n'
        "[model_providers.botainer-broker]\n"
        'base_url = "http://127.0.0.1:54321/v1"\n'
        'env_key = "OPENAI_API_KEY"\n'
    )
    (src / "history.jsonl").write_text('{"kept": true}\n')   # real history: carried

    plan = plan_carry(src, dst)
    carried = {p.name for p in plan.carry}
    assert "history.jsonl" in carried, "ordinary history must still be carried"
    assert "config.toml" not in carried, (
        "a broker model-provider config must never land in the directory that "
        "holds the real credential")

    execute_carry(plan)
    assert not (dst / "config.toml").exists()
    assert (dst / "history.jsonl").exists()
