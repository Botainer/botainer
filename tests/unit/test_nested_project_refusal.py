"""A botainer project nested inside another: WARN, naming every root involved.

Project discovery returns the nearest project and warns about enclosing
projects. Refusing during discovery would strand commands that have no explicit
project-path option. The warning must still explain the risk of agent-writable
nested project configuration.

THE HAZARD IS UNCHANGED, and still the reason anything is printed. The null-bind
mask that makes `.botainer/config.yaml` trusted input is structural but covers
exactly one path — `_MASK_REQUIRED_UNDER_RW = {"/workspace":
("/workspace/.botainer",)}`. Project discovery, its consumer, walks up to the
NEAREST `.botainer/project-id`. The mask's domain is strictly smaller than its
consumer's: nothing masks `/workspace/anysubdir/.botainer/`, which a caged agent
(rw on its workspace) can create.

WHAT WAS WRONG WAS THAT THE COMMANDS WHICH REACH HERE HAD NO WAY OUT. They
resolve a project by walking up and take no project path of their own, so a
stray enclosing project made every one of them unusable — and the refusal's own
remedy, "run the command with an explicit project path", named something they do
not have. (Nine `botainer hpc` subcommands DO take `--project`; they pass a path
straight in and never reach this function. An earlier version of this file, and
of the code comment, said no command took one. That was false, and it was false
because the check I ran was the one that agreed with me.)

TWO CLAIMS I MADE HERE THAT A REFUTING REVIEW DISPROVED, kept in writing because
the reasoning is what future readers will re-derive:

  * "An agent cannot write ABOVE its own workspace root" — FALSE as stated.
    Nothing relates a `mounts.extra` source to the project root, so a config can
    bind the parent directory rw. It is consent-gated (the first-launch confirm
    lists the bind, and the gate re-fires when the bind set changes), which
    bounds it, but the sentence as written was wrong and is tracked as its own
    queue row.
  * "the nearest enclosing project is the one to measure" — FALSE, and it was a
    live regression: with `work/` having run a session and `work/foo/` planted
    empty, asking about `work/foo/bar` reported "No session has ever run",
    which is a false all-clear about a tree that HAD been bound. One `mkdir`
    defeats it, and the threat model already grants the agent that. Hence
    worst-known-wins across EVERY enclosing root.

What is pinned now: the nearest still resolves; the warning names every
enclosing root and which project is being used; the measurement is a session
RECORD WITH A START TIME, not a directory (`inspect` creates a directory and
launches nothing); the three states are keyed correctly with HAS RUN beating
CANNOT TELL beating NEVER RAN; "no session" is stated as a measurement and never
as a safety verdict; the remedy names the enclosing project and not the user's
own; nothing names a remedy that does not exist; and the warning fires once per
process, because `hpc submit` resolves the project three times.
"""
from __future__ import annotations

import json
import uuid as uuidmod
from pathlib import Path

import pytest

from botainer.cli import _common
from botainer.cli._common import find_project_root

OUTER_UUID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fresh_warn_state():
    """The warn-once set is process-global; each test starts empty."""
    _common._WARNED_NESTED.clear()
    yield
    _common._WARNED_NESTED.clear()


def _make_project(root: Path, project_uuid: str = OUTER_UUID) -> Path:
    (root / ".botainer").mkdir(parents=True, exist_ok=True)
    (root / ".botainer" / "project-id").write_text(
        project_uuid + "\n", encoding="utf-8")
    return root


def _state_root(monkeypatch, tmp_path: Path) -> Path:
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    return state_dir.ensure_user_state_dir(create_if_missing=True).state_dir


def _session(state: Path, uuid: str, name: str, *, started: bool) -> Path:
    """A session directory holding a real record.

    `started=False` is the shape `botainer inspect` leaves behind — OBSERVED on
    a fresh install, not assumed: `spec.json` present, `started_at` null,
    nothing ever launched.
    """
    d = state / uuid / "sessions" / name
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps({
        "session_id": name, "project_uuid": uuid, "project_root": "/x",
        "runtime": "docker", "image": "i", "host": "h", "spec": {},
        "started_at": "2026-09-15T00:00:00Z" if started else None,
    }), encoding="utf-8")
    return d


# ── unchanged behaviour ──────────────────────────────────────────────────────

def test_ordinary_project_still_resolves(tmp_path: Path) -> None:
    proj = _make_project(tmp_path / "proj")
    (proj / "src").mkdir()
    assert find_project_root(proj / "src") == proj


def test_no_project_returns_none(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) is None


# ── the directive ────────────────────────────────────────────────────────────

def test_a_nested_project_RESOLVES_to_the_inner_one(tmp_path, capsys) -> None:
    """The directive, pinned: the command runs. It used to raise."""
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    got = find_project_root(inner)

    assert got == inner, capsys.readouterr().err


def test_the_warning_names_the_ENCLOSING_root_and_which_one_is_used(
        tmp_path, capsys) -> None:
    """Asserted as SENTENCES, because a bare `str(outer) in err` is vacuous.

    `inner` is a path UNDER `outer`, so the outer path is a substring of the
    inner one and the old assertion passed even when every line named `inner`.
    Two mutations — swapping `{outer}` for `{inner}` in the header and in the
    remedy — survived the whole file because of it. The remedy one mattered: it
    told the user to delete their OWN project.
    """
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    find_project_root(inner)

    err = capsys.readouterr().err
    assert "NESTED PROJECT" in err, err
    assert f"Using {inner}, which is inside" in err, err
    assert f"another botainer project at {outer}." in err, err
    remedy = [ln for ln in err.splitlines() if "was a mistake" in ln]
    assert remedy, err
    assert str(outer) in remedy[0], remedy
    assert str(inner) not in remedy[0], (
        f"the remedy names the user's OWN project — following it deletes the "
        f"thing they are standing in:\n{remedy[0]}")


def test_it_warns_from_a_SUBDIR_of_the_nested_project(tmp_path, capsys) -> None:
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "s" / "w", str(uuidmod.uuid4()))
    (inner / "deep").mkdir()

    got = find_project_root(inner / "deep")

    assert got == inner
    assert "NESTED PROJECT" in capsys.readouterr().err


# ── the measurement, in all three states ─────────────────────────────────────

def test_when_the_OUTER_HAS_run_the_warning_says_the_agent_could_have_written_it(
        tmp_path, monkeypatch, capsys) -> None:
    """The dangerous shape, and the only one the old refusal was right about."""
    state = _state_root(monkeypatch, tmp_path)
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))
    _session(state, OUTER_UUID, "abc123", started=True)

    find_project_root(inner)

    err = capsys.readouterr().err
    assert f"A session HAS run for {outer}" in err, err
    assert "agent has had that whole tree bound read-write" in err, err
    assert "plugins, mounts, network and agent permissions" in err, err


def test_a_session_DIRECTORY_that_never_STARTED_is_not_a_session(
        tmp_path, monkeypatch, capsys) -> None:
    """`botainer inspect` launches nothing and still leaves a session dir.

    OBSERVED on a fresh install: 0 entries after `init`, 1 after `inspect`,
    whose `spec.json` has `started_at: null`. The directory-counting version of
    this check therefore told a user that an agent had held their tree
    read-write when all that had happened was an inspect. `started_at` is the
    discriminator.
    """
    state = _state_root(monkeypatch, tmp_path)
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))
    _session(state, OUTER_UUID, "inspected", started=False)

    find_project_root(inner)

    err = capsys.readouterr().err
    assert f"No session has ever run for {outer}" in err, (
        f"a composed-but-never-started session was counted as a launch:\n{err}")


def test_when_the_OUTER_NEVER_ran_the_warning_SAYS_SO_and_claims_nothing_more(
        tmp_path, monkeypatch, capsys) -> None:
    """No recorded outer launch permits only a route-qualified statement."""
    _state_root(monkeypatch, tmp_path)
    outer = _make_project(tmp_path / "enclosing")
    # The outer must have state under THIS root, or the honest answer is
    # "cannot tell" — see the no-state-here test below.
    (tmp_path / "root" / "state" / OUTER_UUID).mkdir(parents=True)
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    find_project_root(inner)

    err = capsys.readouterr().err
    assert f"No session has ever run for {outer}" in err, err
    assert "bound as a workspace by that route" in err, (
        f"the measurement must be qualified to the route it actually covers:\n{err}")
    for verdict in ("is safe", "safer", "no risk", "harmless", "protects you"):
        assert verdict not in err.lower(), (
            f"a safety VERDICT reached a one-liner: {verdict!r} in\n{err}")


def test_a_MISSING_state_root_does_not_read_as_NEVER(
        tmp_path, monkeypatch, capsys) -> None:
    """Silence-as-all-clear, the failure this repo keeps re-learning."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "nonexistent-root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    find_project_root(inner)

    err = capsys.readouterr().err
    assert f"Could not tell whether a session has run for {outer}" in err, err
    assert "NOT a statement that none has" in err, err
    assert "No session has ever run" not in err, err


def test_NO_STATE_FOR_THIS_PROJECT_HERE_does_not_read_as_NEVER(
        tmp_path, monkeypatch, capsys) -> None:
    """A project with no state under THIS root may have run under another.

    `MY_BOTAINER` can differ between runs, and hand-deleting a state directory
    is currently the ONLY way to remove a project — so the remedy this very
    warning offers manufactures exactly this state. Reporting it as "never ran"
    would let a user erase the evidence and be reassured by its absence.
    """
    _state_root(monkeypatch, tmp_path)          # root exists, project's dir does not
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    find_project_root(inner)

    err = capsys.readouterr().err
    assert f"Could not tell whether a session has run for {outer}" in err, err
    assert "No session has ever run" not in err, err


def test_an_EMPTY_project_id_cannot_be_measured(
        tmp_path, monkeypatch, capsys) -> None:
    """An unusable id is not an absent launch."""
    _state_root(monkeypatch, tmp_path)
    outer = tmp_path / "enclosing"
    (outer / ".botainer").mkdir(parents=True)
    (outer / ".botainer" / "project-id").write_text("\n", encoding="utf-8")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    find_project_root(inner)

    err = capsys.readouterr().err
    assert "Could not tell whether a session has run" in err, err


# ── the regression the review found ──────────────────────────────────────────

def test_EVERY_enclosing_project_is_consulted_not_just_the_nearest(
        tmp_path, monkeypatch, capsys) -> None:
    """THE CRITICAL. One `mkdir` defeated the nearest-only version.

        work/            real project, HAS run a session
        work/foo/        planted, never ran
        work/foo/bar/    planted

    Standing in `bar`, the nearest enclosing project is `foo`, which has never
    run — so the nearest-only version printed "No session has ever run". That
    tree HAD been bound read-write, as part of `work`'s session. A false
    all-clear, produced by the cheapest action the threat model already grants
    the agent, on the exact measurement the whole change rests on.
    """
    state = _state_root(monkeypatch, tmp_path)
    work_uuid, foo_uuid = str(uuidmod.uuid4()), str(uuidmod.uuid4())
    work = _make_project(tmp_path / "work", work_uuid)
    foo = _make_project(work / "foo", foo_uuid)
    bar = _make_project(foo / "bar", str(uuidmod.uuid4()))
    (state / foo_uuid).mkdir(parents=True)          # foo: state, no session
    _session(state, work_uuid, "real", started=True)

    find_project_root(bar)

    err = capsys.readouterr().err
    assert f"A session HAS run for {work}" in err, (
        f"the outermost project that RAN must win; got:\n{err}")
    assert "No session has ever run" not in err, (
        f"a false all-clear about a tree that was bound read-write:\n{err}")
    assert str(foo) in err, f"every enclosing root must be named:\n{err}"


def test_the_warning_fires_ONCE_per_process(tmp_path, capsys) -> None:
    """`hpc submit` resolves the project THREE times.

    Without this the user sees the same four lines three times over, which is
    how a warning channel becomes scenery.
    """
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    for _ in range(3):
        assert find_project_root(inner) == inner

    assert capsys.readouterr().err.count("NESTED PROJECT") == 1


def test_the_warning_names_NO_REMEDY_THAT_DOES_NOT_EXIST(
        tmp_path, capsys) -> None:
    """What the previous version of this file got wrong, pinned in reverse.

    It asserted `"explicit project path" in msg`, holding in place a remedy the
    commands that reach here do not have. A test enforces a falsehood exactly as
    well as a truth, so this one forbids the shape.
    """
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))

    find_project_root(inner)

    err = capsys.readouterr().err
    for dead in ("--project", "explicit project path"):
        assert dead not in err, (
            f"the warning offers {dead!r}, which these commands do not take:\n{err}")


def test_when_SEVERAL_enclosing_projects_ran_ALL_of_them_are_named(
        tmp_path, monkeypatch, capsys) -> None:
    """No index means no wrong index.

    The first version picked one root out of the list. Whichever end it picked,
    the other was a surviving mutation and a true statement left unsaid — so
    the reported state names every root in it. With two enclosing projects that
    have both run, both appear.
    """
    state = _state_root(monkeypatch, tmp_path)
    work_uuid, foo_uuid = str(uuidmod.uuid4()), str(uuidmod.uuid4())
    work = _make_project(tmp_path / "work", work_uuid)
    foo = _make_project(work / "foo", foo_uuid)
    bar = _make_project(foo / "bar", str(uuidmod.uuid4()))
    _session(state, work_uuid, "a", started=True)
    _session(state, foo_uuid, "b", started=True)

    find_project_root(bar)

    err = capsys.readouterr().err
    hazard = [ln for ln in err.splitlines() if "A session HAS run for" in ln]
    assert hazard, err
    # THE PREFIX TRAP AGAIN, IN MY OWN NEW TEST. `work` is a path prefix of
    # `foo`, so `str(work) in hazard[0]` is satisfied by naming `foo` alone —
    # and two mutations (`ran[:1]`, `ran[-1:]`) survived on exactly that. Assert
    # the JOIN, which only a list of both can produce. `outers` is built walking
    # upward, so the nearest comes first.
    assert f"{foo}, {work}" in hazard[0], (
        f"only one of two bound trees was named:\n{hazard[0]}")


def test_an_UNREADABLE_record_is_not_an_ABSENT_LAUNCH(
        tmp_path, monkeypatch, capsys) -> None:
    """A corrupt `spec.json` must not be counted as "never started".

    The loop skips records it cannot parse; without this it would skip its way
    to `False` and report a clean bill built entirely out of files it failed to
    read.
    """
    state = _state_root(monkeypatch, tmp_path)
    outer = _make_project(tmp_path / "enclosing")
    inner = _make_project(outer / "w", str(uuidmod.uuid4()))
    d = state / OUTER_UUID / "sessions" / "corrupt"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{not json", encoding="utf-8")

    find_project_root(inner)

    err = capsys.readouterr().err
    assert "Could not tell whether a session has run" in err, err
    assert "No session has ever run" not in err, (
        f"unparseable records were counted as an absence of launches:\n{err}")


def test_HAS_RUN_beats_CANNOT_TELL_when_the_roots_disagree(
        tmp_path, monkeypatch, capsys) -> None:
    """The precedence the code states, which nothing was checking.

    A loop checkpoint measured that INVERTING the stated order — letting
    CANNOT TELL win over HAS RUN — left all sixteen tests in this file green,
    while changing what the user is told from "a session HAS run for <work>" to
    "could not tell ... for <work/mid>". Worst-known-wins is the whole safety
    argument for the warning; an unchecked precedence is a decision that exists
    only in a comment.

        work/           HAS run a session
        work/mid/       no state under this root  -> CANNOT TELL
        work/mid/inner/ where the user is standing

    Both enclosing roots are real; they disagree; the dangerous one must win.
    """
    state = _state_root(monkeypatch, tmp_path)
    work_uuid = str(uuidmod.uuid4())
    work = _make_project(tmp_path / "work", work_uuid)
    mid = _make_project(work / "mid", str(uuidmod.uuid4()))   # no state dir at all
    inner = _make_project(mid / "inner", str(uuidmod.uuid4()))
    _session(state, work_uuid, "real", started=True)

    find_project_root(inner)

    err = capsys.readouterr().err
    assert f"A session HAS run for {work}" in err, (
        f"CANNOT TELL outranked HAS RUN — the user is told nothing is known "
        f"about a tree that WAS bound read-write:\n{err}")
    assert "Could not tell" not in err, (
        f"the weaker state was reported alongside the stronger one, which "
        f"blurs the only actionable sentence:\n{err}")
