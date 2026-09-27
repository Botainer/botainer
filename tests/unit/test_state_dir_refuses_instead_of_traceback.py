"""An uncreatable state dir must REFUSE, not emit a Python traceback.
(queue rows 71 + 72)

REPRODUCED BY RUNNING, state root under a directory the account cannot write:

    $ MY_BOTAINER=<unwritable>/cannot-create botainer start --dry-run
      File ".../botainer/core/identity.py", line 179, in resolve_identity
        paths = state_dir.ensure_user_state_dir(create_if_missing=True)
      File ".../botainer/state/dir.py", line 514, in ensure_user_state_dir
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
      File ".../pathlib/__init__.py", line 1012, in mkdir
        os.mkdir(self, mode)
    PermissionError: [Errno 13] Permission denied: '<...>/cannot-create'

THE ROWS' LINE POINTER WAS WRONG, and reproducing is what caught it. Both rows
said `dir.py:600` (`ensure_project_dirs`); the first thing to blow up is
`dir.py:514` (`ensure_user_state_dir`), which runs earlier and on every
command. Fixing only the cited line would have left the reproduction untouched.

`botainer where` already survived this — it passes `create_if_missing=False`
and has an explicit "(does not exist yet — run `botainer setup`)" branch. The
product knew how to say it; the creation path had never learned.

WHY IT MATTERS MOST ON A CLUSTER: ENOSPC and EDQUOT are the ordinary failure
modes of an HPC home directory, and a quota error rendered as a traceback is
indistinguishable, to the person reading it, from the launcher being broken.
"""
from __future__ import annotations

import ast
import errno
from pathlib import Path

import pytest

from botainer.core.refusal import RefusalCategory, Refused
from botainer.state import dir as state_dir


@pytest.fixture
def unwritable(tmp_path) -> Path:
    """A directory whose child cannot be created."""
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    yield parent / "cannot-create"
    parent.chmod(0o700)  # so pytest can clean up


def test_it_refuses_instead_of_raising_OSError(unwritable, monkeypatch) -> None:
    """THE DEFECT: a bare OSError escaped to the top and printed a traceback."""
    monkeypatch.setenv("MY_BOTAINER", str(unwritable))

    with pytest.raises(Refused) as exc:
        state_dir.ensure_user_state_dir(create_if_missing=True)

    assert exc.value.category is RefusalCategory.STATE_WRITE_FAILED


def test_the_message_says_what_where_why_and_what_to_do(
        unwritable, monkeypatch) -> None:
    """A refusal that only says "failed" is a traceback with better manners.

    Asserting all four parts because any one of them missing puts the user back
    to guessing: which directory, which path, why, and which knob moves it.
    """
    monkeypatch.setenv("MY_BOTAINER", str(unwritable))

    with pytest.raises(Refused) as exc:
        state_dir.ensure_user_state_dir(create_if_missing=True)
    msg = str(exc.value)

    assert "the botainer state root" in msg, msg          # WHAT
    assert str(unwritable) in msg, msg                    # WHERE
    assert "permission" in msg.lower(), msg               # WHY
    assert "MY_BOTAINER" in msg, msg                      # WHICH KNOB
    assert "botainer setup" in msg, msg                   # WHAT TO DO
    assert "Traceback" not in msg, msg


def test_it_names_ONLY_the_env_var_the_launcher_actually_READS(
        unwritable, monkeypatch) -> None:
    """MY_BOTAINER is an input; BOTAINER_STATE_ROOT is an OUTPUT.

    THIS TEST CAUGHT A BUG IN THE FIX IT WAS WRITTEN FOR. My first version of
    the refusal offered BOTAINER_STATE_ROOT as an alternative knob. It is not
    one: `_resolve_state_root` reads MY_BOTAINER and nothing else, and
    `subprocess_state_env` documents BOTAINER_STATE_ROOT as the value the
    launcher EXPORTS to plugin subprocesses — "they do NOT read MY_BOTAINER
    directly". So the advice would have sent someone to export a variable the
    launcher ignores, and they would have seen no effect and no explanation.

    It matters in exactly the situation the refusal fires in: INSIDE a session
    BOTAINER_STATE_ROOT is always set, so any code branching on it will take
    that branch every time.

    `botainer where` had the identical bug on the same pair and is fixed in the
    same commit.
    """
    monkeypatch.delenv("MY_BOTAINER", raising=False)
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(unwritable))
    monkeypatch.setattr(
        state_dir, "_resolve_state_root", lambda: unwritable)

    with pytest.raises(Refused) as exc:
        state_dir.ensure_user_state_dir(create_if_missing=True)
    msg = str(exc.value)

    assert "BOTAINER_STATE_ROOT" not in msg, (
        "the refusal offered a variable the launcher never reads:\n" + msg)
    assert "MY_BOTAINER" in msg, msg


@pytest.mark.parametrize("err,expected", [
    (errno.EACCES, "permission"),
    (errno.EROFS, "READ-ONLY"),
    (errno.ENOSPC, "FULL"),
    (errno.EDQUOT, "QUOTA"),
    (errno.ENOTDIR, "is a FILE"),
])
def test_each_cause_is_translated_into_plain_language(
        tmp_path, monkeypatch, err, expected) -> None:
    """`[Errno 122]` is not an explanation.

    EDQUOT especially: on a cluster that is the single most likely failure, and
    the raw errno is one almost nobody recognises.
    """
    target = tmp_path / "x"

    def boom(*a, **k):
        raise OSError(err, "simulated")

    monkeypatch.setattr(Path, "mkdir", boom)

    with pytest.raises(Refused) as exc:
        state_dir._mkdir_0700(target, "the test directory")

    assert expected in str(exc.value), str(exc.value)


def test_quota_and_full_add_the_cluster_specific_advice(
        tmp_path, monkeypatch) -> None:
    """On HPC "disk full" is nearly always a HOME quota, and the fix is to
    point the state root at project or scratch space — which the generic
    "choose a writable directory" line does not convey."""
    def boom(*a, **k):
        raise OSError(errno.EDQUOT, "simulated")

    monkeypatch.setattr(Path, "mkdir", boom)

    with pytest.raises(Refused) as exc:
        state_dir._mkdir_0700(tmp_path / "x", "the test directory")
    msg = str(exc.value)

    assert "quota" in msg.lower(), msg
    assert "scratch" in msg.lower(), msg


def test_an_ordinary_creation_still_works(tmp_path) -> None:
    """Or the guard is breakage rather than a fix.

    Also pins the mode, which used to be repeated at 12 call sites and now
    lives only inside the helper.
    """
    target = tmp_path / "a" / "b"

    state_dir._mkdir_0700(target, "the test directory")

    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700


def test_calling_it_twice_is_not_an_error(tmp_path) -> None:
    """`exist_ok=True` was the old behaviour at every call site; keep it."""
    target = tmp_path / "a"
    state_dir._mkdir_0700(target, "the test directory")
    state_dir._mkdir_0700(target, "the test directory")
    assert target.is_dir()


# ── the structural half ─────────────────────────────────────────────────────

_ALLOWED_RAW_MKDIR = {
    # The chokepoint itself — this is the one place that may call mkdir.
    "_mkdir_0700",
    # Best-effort discoverability. `_refresh_by_name_symlink` already swallows
    # failure by design, and turning it into a hard refusal would break
    # launches that work today. A DECISION, marked in the source as one.
    "_refresh_by_name_symlink",
}


def test_no_other_function_in_dir_py_calls_mkdir_directly() -> None:
    """STRUCTURE, not a rule someone must remember.

    The fix is only worth anything if a 12th call site cannot quietly reappear
    with a bare `mkdir(parents=True, exist_ok=True, mode=0o700)`. Wrapping each
    site in try/except would have been a convention; routing them through one
    helper is a property — and this test is what keeps it one.

    AST, NOT a substring scan of the source: a grep for "mkdir(" matches this
    module's own docstrings (it quotes the old shape), and the assertion-shape
    gate explicitly calls source-substring checks out as weak.
    """
    src = Path(state_dir.__file__).read_text()
    tree = ast.parse(src)

    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _ALLOWED_RAW_MKDIR:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "mkdir"):
                offenders.append(f"{fn.name}:{node.lineno}")

    assert not offenders, (
        "these call mkdir directly instead of via _mkdir_0700, so a failure "
        "there is a raw traceback again:\n  " + "\n  ".join(offenders))


def test_the_allowlist_itself_is_not_stale() -> None:
    """An exemption for a function that no longer exists silently widens.

    If `_refresh_by_name_symlink` is ever renamed, its entry would go on
    excusing nothing while looking like it covered a case — the same
    stale-baseline shape this repo keeps finding.
    """
    missing = [n for n in _ALLOWED_RAW_MKDIR if not hasattr(state_dir, n)]
    assert not missing, f"allowlist names functions that no longer exist: {missing}"
