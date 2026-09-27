"""`botainer where` locates data and distinguishes reclaimable storage.

The command is read-only; it reports paths and sizes rather than moving or
deleting content. Tests explicitly check that credential storage is not labeled
reclaimable, because that recommendation would lose authentication state."""
from __future__ import annotations

import uuid

import pytest
from click.testing import CliRunner

from botainer.cli.where import where


@pytest.fixture
def populated(tmp_path, monkeypatch):
    """A state root shaped like a real one, with measurable content."""
    root = tmp_path / "state-root"
    uid = str(uuid.uuid4())
    proj = root / "state" / uid
    for sub in ("packages/pip", "scratch", "home/.npm", "sessions", "data/agent-claude"):
        (proj / sub).mkdir(parents=True)
    # scratch is deliberately BIGGER than packages: the render loop visits
    # packages first, so only a real size sort can put scratch on top.
    (proj / "packages" / "pip" / "big.bin").write_bytes(b"x" * 100_000)
    (proj / "scratch" / "mid.bin").write_bytes(b"x" * 300_000)
    (proj / "data" / "agent-claude" / ".credentials.json").write_bytes(b"x" * 200)
    (root / "plugins").mkdir()
    (root / "policy.yaml").write_text("{}")
    monkeypatch.setenv("MY_BOTAINER", str(root))
    monkeypatch.setattr(
        "botainer.state.dir.list_projects",
        lambda: [type("E", (), {"uuid": uid, "last_path": str(tmp_path / "myproj"),
                                "path_exists": True, "paths": (),
                                "display_name": "myproj", "last_session_at": "",
                                "last_session_runtime": "", "sessions_dir_count": 0})()])
    return root, proj


def _run() -> str:
    res = CliRunner().invoke(where, [])
    assert res.exit_code == 0, res.output
    return res.output


def test_it_names_the_state_root_and_how_it_was_chosen(populated) -> None:
    """The original question. `MY_BOTAINER` vs default is the thing that
    silently bites — a shell missing the export gets a second, empty root."""
    root, _ = populated
    out = _run()
    assert str(root) in out
    assert "MY_BOTAINER" in out


def test_regenerable_dirs_are_marked_reclaimable_with_a_command(populated) -> None:
    _, proj = populated
    out = _run()
    assert "Reclaimable" in out
    assert f"rm -rf {proj / 'packages'}" in out, out


def test_the_credential_directory_is_never_offered_for_deletion(populated) -> None:
    """The direction that matters. Offering `rm -rf .../data` would cost the
    user their login — worse than the problem this command solves."""
    _, proj = populated
    out = _run()
    assert f"rm -rf {proj / 'data'}" not in out
    assert f"rm -rf {proj / 'sessions'}" not in out
    # ...and it says so rather than merely omitting them
    assert "do NOT delete" in out


def test_biggest_first(populated) -> None:
    """A reclaim list in arbitrary order makes the user read all of it."""
    _, proj = populated
    out = _run()
    body = out[out.index("Reclaimable"):]
    assert body.index(str(proj / "scratch")) < body.index(str(proj / "packages")), (
        "reclaim list is not sorted by size — scratch (300K) must precede "
        "packages (100K) despite packages being rendered first")


def test_it_deletes_nothing(populated) -> None:
    """Read-only by contract: it prints commands, it does not run them."""
    _, proj = populated
    _run()
    assert (proj / "packages" / "pip" / "big.bin").exists()
    assert (proj / "scratch" / "mid.bin").exists()


def test_superseded_history_dirs_are_found_only_where_the_carry_puts_them(tmp_path) -> None:
    """`data/` is do-NOT-delete because it holds credentials — right for the
    directory as a whole — but a CARRY leaves timestamped copies inside it that
    are genuinely safe to remove, and pointing users away from the only
    reclaimable thing in there is its own defect.

    THE ASSERTION BELOW USED TO INCLUDE `.archived-`, AND THAT WAS WRONG (#226).
    The two suffixes are not interchangeable:

      `.superseded-`  written after a carry COPIED AND VERIFIED every file.
                      A live copy exists. Reclaimable.
      `.archived-`    written by `archive_dir`, "the way out of a BLOCKED
                      carry". Nothing was copied. It is the ONLY copy.

    `where` prints an `rm -rf` for everything in the reclaim list, so including
    archived dirs meant offering to delete irreplaceable history — to someone
    who, by construction, ran this command because they were looking for history
    they had lost.

    Narrow on purpose otherwise: only under `agent-*/{profiles,broker-state}/`,
    and only our own suffixes, so nothing a user happened to name similarly is
    offered up. Those decoys are still asserted below.
    """
    from botainer.cli.where import (_archived_history_dirs,
                                    _superseded_history_dirs)

    data = tmp_path / "data"
    prof = data / "agent-claude" / "profiles"
    prof.mkdir(parents=True)
    (prof / "default").mkdir()                              # live — keep
    (prof / "default.superseded-20260828T010203Z").mkdir()  # from a carry
    (prof / "default.archived-20260828T010203Z").mkdir()    # from a conflict
    (data / "agent-codex" / "broker-state").mkdir(parents=True)
    (data / "agent-codex" / "broker-state"
     / "work.superseded-20260828T010203Z").mkdir()
    # Decoys that must NOT be offered.
    (data / "shared-auth.superseded-something").mkdir()
    (prof / "default.superseded-link").symlink_to(prof / "default",
                                                  target_is_directory=True)

    found = {p.name for p in _superseded_history_dirs(data)}
    archived = {p.name for p in _archived_history_dirs(data)}

    assert found == {
        "default.superseded-20260828T010203Z",
        "work.superseded-20260828T010203Z",
    }, found
    assert archived == {"default.archived-20260828T010203Z"}, archived
    assert not (found & archived), "a directory cannot be both"
    assert "default" not in found, "the LIVE profile must never be reclaimable"
    assert "shared-auth.superseded-something" not in found, \
        "wrong parent — only agent-*/{profiles,broker-state}/ is scanned"
    assert "default.superseded-link" not in found, \
        "a symlink named like a set-aside copy is not one"


def test_no_superseded_dirs_means_nothing_extra_is_listed(tmp_path) -> None:
    from botainer.cli.where import _superseded_history_dirs

    data = tmp_path / "data"
    (data / "agent-claude" / "profiles" / "default").mkdir(parents=True)
    assert _superseded_history_dirs(data) == []
    assert _superseded_history_dirs(tmp_path / "nope") == []


def test_the_live_profile_dir_is_derived_from_either_marker() -> None:
    """`profiles/work.superseded-<ts>` came from `profiles/work`.

    Stripped by MARKER, not by splitting on the last dot: a profile name can
    contain no dot at all (`^[a-z][a-z0-9_-]{0,31}$`, because the name becomes
    a bind source and an mkdir), so the first marker is unambiguous and a
    dot-based split would be guessing.
    """
    from pathlib import Path

    from botainer.cli.where import live_profile_dir_for

    base = Path("/s/data/agent-claude/profiles")
    assert live_profile_dir_for(
        base / "work.superseded-20260828T010203Z") == base / "work"
    assert live_profile_dir_for(
        base / "my-work_2.archived-20260828T010203Z") == base / "my-work_2"
    assert live_profile_dir_for(base / "work") == base / "work", (
        "a directory with no marker is already the live one"
    )
    # The two-dot case, and it is not hypothetical: the restore instructions
    # this command prints tell the user to `mv <live> <old>.replaced`, so
    # following them produces exactly this name. Splitting on the LAST dot
    # would derive `work.superseded-<ts>` — itself an archive — instead of
    # `work`, and the next restore would move the wrong directory.
    assert live_profile_dir_for(
        base / "work.superseded-20260828T010203Z.replaced") == base / "work"


def test_where_offers_RESTORE_and_not_only_deletion(tmp_path, monkeypatch) -> None:
    """Listing these as reclaimable and stopping there would leave the one
    command that FINDS an old history offering only to destroy it.

    Wanting the transcripts back is at least as likely as wanting the bytes,
    and no command restores one — the profile charset forbids switching INTO a
    dotted name — so the two renames are printed the way this command already
    prints the `rm -rf`.
    """
    from click.testing import CliRunner

    from botainer.cli.where import where

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    prof = (tmp_path / "state" / "state" / "UID1" / "data" / "agent-claude"
            / "profiles")
    (prof / "default").mkdir(parents=True)
    old = prof / "default.superseded-20260828T010203Z"
    old.mkdir()
    (old / "history.jsonl").write_text("x")
    (tmp_path / "state" / "state" / "UID1" / "meta.json").write_text(
        '{"project_uuid": "UID1", "last_path": "/tmp/p", "path_history": []}')

    out = CliRunner().invoke(where, [], catch_exceptions=False).output

    assert "To restore one" in out
    assert f"mv {old} {prof / 'default'}" in out, (
        "the restore has to be the actual two renames for THIS directory, not "
        "a generic instruction the user has to translate"
    )
    assert "on the HOST" in out, (
        "host shell and inside-a-session are different places; a command "
        "without a where is one a user cannot run"
    )


def test_the_reclaim_footer_no_longer_claims_everything_regenerates(
        tmp_path, monkeypatch) -> None:
    """It used to say "Everything marked ♻ regenerates on next use".

    That stopped being true the moment superseded history joined the list:
    packages and caches rebuild themselves, an old transcript does not. A
    blanket reassurance over a list containing one irreplaceable member is the
    kind of sentence someone deletes a directory on.
    """
    from click.testing import CliRunner

    from botainer.cli.where import where

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    prof = (tmp_path / "state" / "state" / "UID1" / "data" / "agent-claude"
            / "profiles")
    (prof / "default.superseded-20260828T010203Z").mkdir(parents=True)
    (prof / "default.superseded-20260828T010203Z" / "h.jsonl").write_text("x")
    (tmp_path / "state" / "state" / "UID1" / "meta.json").write_text(
        '{"project_uuid": "UID1", "last_path": "/tmp/p", "path_history": []}')

    out = CliRunner().invoke(where, [], catch_exceptions=False).output

    assert "Everything marked ♻ regenerates" not in out
    assert "superseded history does NOT" in out
