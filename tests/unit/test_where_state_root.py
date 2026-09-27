"""`botainer where` must be able to answer "where?" to a script.

Automation needs a machine-readable state-root query that honors configured
roots. A missing directory must not be reported as an empty inspected one.
The query must avoid parsing prose or walking the tree to measure sizes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.where import where


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "state"
    (r / "images").mkdir(parents=True)
    monkeypatch.setenv("MY_BOTAINER", str(r))
    return r


def test_state_root_prints_one_bare_line(root) -> None:
    """THE POINT: `STATE=$(botainer where --state-root)` must yield a path.

    One line, nothing else. A label, a blank line or a trailing note would all
    survive a human reading it and silently corrupt the variable.
    """
    res = CliRunner().invoke(where, ["--state-root"])

    assert res.exit_code == 0, res.output
    lines = res.stdout.splitlines()
    assert len(lines) == 1, f"expected exactly one line, got: {lines!r}"
    assert lines[0] == str(root), lines[0]


def test_it_is_a_usable_path_not_a_sentence(root) -> None:
    """The defect was that the answer was EMBEDDED in prose.

    Asserting the absence of the old label, because "State root: /x" would pass
    a naive "does the output contain the path" check while being useless to a
    script.
    """
    out = CliRunner().invoke(where, ["--state-root"]).stdout

    assert "State root" not in out, out
    assert Path(out.strip()).is_dir(), out


def test_a_missing_root_still_prints_the_path_but_exits_3(tmp_path, monkeypatch) -> None:
    """A missing root is NOT an empty one, and must not read as a clean result.

    A diagnostic must distinguish an absent path from an inspected empty one;
    both can otherwise produce a misleading count of zero.

    The path still goes to stdout — it is the right answer to "where would it
    be" — so `$(…)` keeps working, while `set -e` stops and a human sees why.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "nope"))

    res = CliRunner().invoke(where, ["--state-root"])

    assert res.exit_code == 3, f"exit {res.exit_code}: {res.output}"
    assert res.stdout.strip() == str(tmp_path / "nope"), res.stdout


def test_the_warning_goes_to_stderr_not_stdout(tmp_path, monkeypatch) -> None:
    """Or command substitution captures the warning INTO the variable.

    Asserting on `res.stdout`/`res.stderr`, NOT `res.output`: click 8.2 removed
    `CliRunner(mix_stderr=...)` and `Result.output` now carries BOTH streams.
    A test written against `.output` passes whether or not the note lands on
    stdout — i.e. it would stay green with the feature broken, which is the
    assertion-shape defect this repo keeps finding.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "nope"))

    res = CliRunner().invoke(where, ["--state-root"])

    assert res.stdout.strip() == str(tmp_path / "nope"), res.stdout
    assert "does not exist" in res.stderr, res.stderr
    assert "does not exist" not in res.stdout, (
        "the note landed on stdout, so $(…) would capture it into the path")


def test_it_does_not_walk_the_tree(root, monkeypatch) -> None:
    """`--sizes` is the default and is slow on a network filesystem.

    A diagnostic that waits for a du-walk to learn one path is a diagnostic
    people stop running.

    PATCHED UNCONDITIONALLY, and asserting the PATH as well as the exit code.
    The first version guarded the patch with `hasattr(where_mod, "_dir_size")`
    and then asserted only `exit_code == 0` — so if the helper were ever
    renamed the patch would silently not apply, and an exit code alone stays
    green if the feature is deleted. The assertion-shape gate flagged exactly
    that (E1), correctly. `_dir_size` is a real symbol at where.py:104; there
    is nothing to guard against.
    """
    import botainer.cli.where as where_mod

    def explode(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("--state-root walked the tree for sizes")

    monkeypatch.setattr(where_mod, "_dir_size", explode)

    res = CliRunner().invoke(where, ["--state-root"])

    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == str(root), res.stdout


def test_plain_where_is_unchanged(root) -> None:
    """Adding a flag must not alter what the command already printed."""
    out = CliRunner().invoke(where, ["--no-sizes"]).output

    assert "State root:" in out, out
    assert str(root) in out, out


def test_where_names_ONLY_the_env_var_that_actually_CHOSE_the_root(
        tmp_path, monkeypatch) -> None:
    """`set by:` must not name a variable the launcher never reads.

    ADDED TO MAKE A COMMIT MESSAGE TRUE. `ba1b97f` claimed this change was
    "proven by stashing dir.py and where.py" — but reverting where.py's wrong
    two-branch `src` failed ZERO tests (183 passed), so the where.py half was
    never proven at all. The loop tzar caught the claim; this is the test that
    should have existed when I made it.

    THE DEFECT: `src` fell back to reporting "set by: BOTAINER_STATE_ROOT"
    whenever that variable was set. It is an OUTPUT — `subprocess_state_env`
    exports it TO plugin subprocesses, and `_resolve_state_root` reads
    MY_BOTAINER and nothing else. INSIDE a session it is always set, so `where`
    would confidently attribute the root to something with no part in choosing
    it, and a user who edited it would see no effect and no explanation.
    """
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.delenv("MY_BOTAINER", raising=False)
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(tmp_path / "somewhere-else"))
    monkeypatch.setattr(
        "botainer.state.dir._resolve_state_root", lambda: root)

    out = CliRunner().invoke(where, ["--no-sizes"]).output

    assert "BOTAINER_STATE_ROOT" not in out, (
        "`where` attributed the state root to a variable the launcher never "
        "reads:\n" + out)
    assert "set by:" in out, out


def test_where_DOES_name_MY_BOTAINER_when_it_is_set(root) -> None:
    """The control: the attribution still works for the real knob.

    Without this the test above passes if `set by:` stops naming anything.
    """
    out = CliRunner().invoke(where, ["--no-sizes"]).output

    assert "MY_BOTAINER" in out, out
