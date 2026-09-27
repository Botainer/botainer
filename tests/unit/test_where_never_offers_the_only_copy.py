"""`botainer where` must never print `rm -rf` for the only copy of history. (#226)

THE TWO SET-ASIDE MARKERS DO NOT MEAN THE SAME THING:

    <profile>.superseded-<ts>   written by supersede_carried(), which runs ONLY
                                after a carry copied and verified every file.
                                A live copy exists. Safe to delete.
    <profile>.archived-<ts>     written by archive_dir(), whose own docstring
                                calls it "the way out of a BLOCKED carry".
                                A blocked carry copied NOTHING. This directory
                                is the ONLY copy of that history.

`where` matched both, labelled both "already copied across", and put both in the
reclaimable list it prints `rm -rf` lines for. The justification was written down
and was half wrong:

    "Both are made by RENAME after the live copy was written and verified, so
     every byte in them also exists at the live location — which is exactly what
     makes them the one safe thing to delete inside `data/`."

WHO THIS HITS. Someone runs `botainer where` because they are looking for history
they cannot find — that is what the command is for. The directory most likely to
be the thing they are looking for is the one the command tells them to delete.

The assertion is on the RECLAIM SURFACE (does an `rm -rf` line name it), not on
the wording, because the wording can be softened while the offer remains.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


def _state_with_both_markers(tmp_path: Path) -> Path:
    """A project holding one of each kind, both non-empty.

    Both must be present and non-empty: an empty directory is skipped by the
    size filter, so a fixture with an empty archived dir would pass without
    exercising anything — the check-that-cannot-fire shape.
    """
    uid = "u" * 32
    data = tmp_path / "state" / uid / "data" / "agent-claude" / "profiles"
    for name in ("work.superseded-20260901T000000Z",
                 "work.archived-20260902T000000Z"):
        d = data / name
        d.mkdir(parents=True)
        (d / "history.jsonl").write_text('{"m": "%s"}\n' % name * 40)
    (data / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / uid / "meta.json").write_text(
        '{"last_path": "/tmp/p", "paths": ["/tmp/p"]}')
    return tmp_path


def _run_where(tmp_path, monkeypatch) -> str:
    from botainer.cli.main import cli
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    return CliRunner().invoke(cli, ["where", "--sizes"]).output


def test_an_archived_dir_is_never_offered_for_deletion(tmp_path, monkeypatch) -> None:
    _state_with_both_markers(tmp_path)
    out = _run_where(tmp_path, monkeypatch)
    offered = [ln for ln in out.splitlines()
               if "rm -rf" in ln and ".archived-" in ln]
    assert not offered, (
        "`where` printed a deletion command for a blocked-carry directory, "
        "which is the only copy of that history:\n  " + "\n  ".join(offered))


def test_a_superseded_dir_IS_still_offered(tmp_path, monkeypatch) -> None:
    """Guard against fixing the leak by dropping the whole feature.

    Reclaiming genuinely-copied history is the point of listing these at all,
    so a fix that simply stops offering everything must fail here.
    """
    _state_with_both_markers(tmp_path)
    out = _run_where(tmp_path, monkeypatch)
    offered = [ln for ln in out.splitlines()
               if "rm -rf" in ln and ".superseded-" in ln]
    assert offered, (
        "no `rm -rf` offered for a superseded copy — the reclaim feature was "
        f"removed rather than corrected. Output:\n{out}")


def test_the_archived_dir_is_still_SHOWN_and_named_as_the_only_copy(
        tmp_path, monkeypatch) -> None:
    """Hiding it would be its own defect.

    Finding stranded history is why someone runs this command. Silently omitting
    the one directory that holds it would trade a data-loss trap for a
    can't-find-it trap.
    """
    _state_with_both_markers(tmp_path)
    out = _run_where(tmp_path, monkeypatch)
    assert ".archived-" in out, f"the archived copy is not listed at all:\n{out}"
    line = next((ln for ln in out.splitlines() if ".archived-" in ln), "")
    assert "ONLY COPY" in line.upper(), (
        f"the archived copy is listed without saying it is irreplaceable: {line!r}")


@pytest.mark.parametrize("marker,copied", [
    (".superseded-", True),
    (".archived-", False),
])
def test_the_classifier_keys_on_the_marker_not_on_set_aside_ness(
        tmp_path, marker, copied) -> None:
    """The unit beneath the display, so the rule survives a UI rewrite."""
    from botainer.cli.where import (_archived_history_dirs,
                                    _superseded_history_dirs)
    data = tmp_path / "agent-claude" / "profiles"
    d = data / f"work{marker}20260901T000000Z"
    d.mkdir(parents=True)
    (d / "history.jsonl").write_text("x")
    assert bool(_superseded_history_dirs(tmp_path)) is copied
    assert bool(_archived_history_dirs(tmp_path)) is (not copied)
