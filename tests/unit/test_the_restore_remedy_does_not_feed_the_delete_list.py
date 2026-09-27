"""`where`'s restore commands must not turn live history into a delete offer.

THE DEFECT. `botainer where --sizes` prints two `mv` lines to put a superseded
history directory back. The pair for the ARCHIVED case was right; the pair for
the SUPERSEDED case parked the live directory under a name derived from the
SUPERSEDED one:

    mv <live>  <live>.superseded-20260901T000000Z.replaced     # what it printed
    mv <live>  <live>.replaced                                 # what it meant

BOTH ARE RENAMES, SO NOTHING IS DESTROYED BY THE MOVE ITSELF. That is why this
reads as cosmetic and is not. The name it parks under still contains
`.superseded-`, and `_set_aside_history_dirs` matches that marker by name — so
the moment the user follows the printed instructions, their CURRENT history is
classified as a carried-and-verified spare copy, and the very next
`where --sizes` prints an `rm -rf` line for it under the heading that calls it
"safe to reclaim".

WHO THIS HITS is the same person as #226: someone running `where` BECAUSE they
are hunting for history they cannot find. They follow the remedy the command
gave them, and the command then offers to delete what they just recovered.

WHY THE TEST RUNS THE COMMANDS INSTEAD OF READING THEM. A string assertion on
the printed line would pin the spelling of a remedy, not its effect, and this
defect IS an effect two steps downstream of the spelling — the harm only appears
after the rename, in a later invocation, through a classifier neither line
mentions. So the test executes exactly what was printed and then asks the
product again. (The repo's standing rule: run the remedy a message names.)
"""
from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

LIVE_MARKER = "this-is-the-live-history"
STAMP = "20260901T000000Z"


def _state_with_a_superseded_copy(tmp_path: Path) -> tuple[Path, Path]:
    """One live profile dir and one superseded copy, both non-empty.

    Both must hold real bytes: the reclaim list is size-filtered, so an empty
    directory is skipped entirely and a fixture built that way would exercise
    nothing while passing.
    """
    uid = "u" * 32
    profiles = tmp_path / "state" / uid / "data" / "agent-claude" / "profiles"

    live = profiles / "work"
    live.mkdir(parents=True)
    (live / "history.jsonl").write_text('{"m": "%s"}\n' % LIVE_MARKER * 40)

    old = profiles / f"work.superseded-{STAMP}"
    old.mkdir(parents=True)
    (old / "history.jsonl").write_text('{"m": "an older carried copy"}\n' * 40)

    (tmp_path / "state" / uid / "meta.json").write_text(
        '{"last_path": "/tmp/p", "paths": ["/tmp/p"]}')
    return live, old


def _run_where(tmp_path, monkeypatch) -> str:
    from botainer.cli.main import cli
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    return CliRunner().invoke(cli, ["where", "--sizes"]).output


def _printed_mv_pairs(out: str) -> list[tuple[str, str]]:
    """The `mv SRC DST` lines exactly as the command printed them.

    Split on whitespace rather than parsed cleverly: the fixture's paths are
    under `tmp_path` and contain no spaces, and a remedy whose paths needed
    quoting to survive a copy-paste would be its own defect.
    """
    pairs = []
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] == "mv":
            pairs.append((parts[1], parts[2]))
    return pairs


def _reclaim_offers(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if "rm -rf" in ln]


def test_following_the_restore_remedy_does_not_put_live_history_on_the_delete_list(
        tmp_path, monkeypatch) -> None:
    """THE DEFECT, measured through its consequence rather than its wording."""
    live, _old = _state_with_a_superseded_copy(tmp_path)

    first = _run_where(tmp_path, monkeypatch)
    pairs = _printed_mv_pairs(first)
    assert len(pairs) == 2, (
        "expected the two restore renames for the superseded copy; the fixture "
        f"or the output shape has moved:\n{first}")

    for src, dst in pairs:
        assert not Path(dst).exists(), (
            f"the remedy would clobber an existing path: mv {src} {dst}")
        os.rename(src, dst)

    after = _run_where(tmp_path, monkeypatch)

    # Where did the live history's bytes end up? Ask the disk, not the output.
    holding_live = [p.parent for p in tmp_path.rglob("history.jsonl")
                    if LIVE_MARKER in p.read_text()]
    assert len(holding_live) == 1, (
        f"the live history should exist exactly once after two renames; "
        f"found {len(holding_live)}: {holding_live}")
    parked = holding_live[0]

    offered = [ln for ln in _reclaim_offers(after) if str(parked) in ln]
    assert not offered, (
        "following the restore commands `where` itself printed left the user's "
        f"live history at {parked.name}, which `where` now offers to delete:\n  "
        + "\n  ".join(offered))


def test_the_live_directory_is_parked_under_its_own_name(
        tmp_path, monkeypatch) -> None:
    """The same defect one step earlier, so a failure says WHICH name is wrong.

    The test above proves the harm; this one localises it, and would still fail
    if the classifier were later taught to ignore a `.replaced` suffix — the
    printed name would remain a claim that the live history is a spare copy.
    """
    live, old = _state_with_a_superseded_copy(tmp_path)

    pairs = _printed_mv_pairs(_run_where(tmp_path, monkeypatch))
    park = next((dst for src, dst in pairs if src == str(live)), None)
    assert park is not None, (
        f"no printed rename moves the live directory {live} aside; pairs={pairs}")

    assert park == f"{live}.replaced", (
        "the live directory is parked under a name derived from the SUPERSEDED "
        f"copy instead of from itself:\n  got:      {park}\n  expected: "
        f"{live}.replaced")


def test_the_superseded_copy_really_is_offered_for_deletion_to_begin_with(
        tmp_path, monkeypatch) -> None:
    """THE OPPOSITE DIRECTION, and the reason the first test is not vacuous.

    Everything above turns on `where` printing `rm -rf` for a superseded
    directory. If it stopped doing that — a changed heading, a size filter that
    excluded the fixture — the first test would pass by finding no offer at all
    while the defect sat untouched.
    """
    _live, old = _state_with_a_superseded_copy(tmp_path)

    offered = [ln for ln in _reclaim_offers(_run_where(tmp_path, monkeypatch))
               if str(old) in ln]
    assert offered, (
        "the reclaim list no longer names the superseded copy, so the test "
        "above can no longer detect the live history joining it")
