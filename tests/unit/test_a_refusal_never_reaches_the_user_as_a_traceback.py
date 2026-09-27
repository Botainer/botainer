"""A refusal is a message. A traceback is a bug report the user cannot act on.

MEASURED, with `MY_BOTAINER` pointed at /project — the exact mistake the refusal
text exists to correct, and the one a cluster user makes first:

    botainer doctor       refused: state-root-not-allowed: … + the remedy
    botainer list         Traceback (most recent call last):  … 43 lines, rc=1
    botainer image list   Traceback (most recent call last):  … 47 lines

Same state root, same refusal raised, three different experiences. `doctor` and
most commands carry `@handle_refusals`; those two did not. Decorating them would
have fixed those two — the rule. `main()` rendering any escaping refusal is the
structure: a command that forgets the decorator still cannot show a traceback.

WHAT THIS FILE PINS, and the second half is the one that keeps it honest: a
refusal renders as one red line plus its own text, AND a genuine programming error
still raises. A handler that swallows everything would pass the first assertion
and turn every future crash into a silent misreport.
"""
from __future__ import annotations

import pytest

from botainer.cli._refusal_handler import render_refusal
from botainer.core.identity import IdentityChangeRefused
from botainer.core.refusal import RefusalCategory, Refused


def test_a_refusal_renders_as_a_LINE_and_returns_its_exit_code(capsys):
    """The category and the detail both reach the user, in one line."""
    code = render_refusal(Refused(
        RefusalCategory.STATE_ROOT_NOT_ALLOWED,
        "MY_BOTAINER points at /project/grp/user/botainer"))

    err = capsys.readouterr().err
    assert "refused: state-root-not-allowed" in err, err
    assert "/project/grp/user/botainer" in err, (
        f"the detail was dropped; the category alone tells nobody what to do:\n{err}")
    assert "Traceback" not in err
    assert code == 2, f"a refusal exits 2 per the exit-code table, not {code}"


def test_a_refusal_with_NO_message_still_renders(capsys):
    """`Refused(category)` with no detail is legal and has bitten before.

    The decorator's own comment records an IndexError on `exc.args[0]`; the
    shared renderer must not reintroduce it, or the traceback comes back by a
    different door.
    """
    code = render_refusal(Refused(RefusalCategory.CONFIG_MISSING))

    err = capsys.readouterr().err
    assert "refused: config-missing" in err, err
    assert code == 2


def test_an_identity_refusal_renders_too(capsys):
    """The other refusal type the decorator handles. Missing it here would make
    the central net narrower than the decorator it backs up."""
    code = render_refusal(IdentityChangeRefused("this project id moved"))

    err = capsys.readouterr().err
    assert "refused:" in err and "moved" in err, err
    assert code == 2


def test_a_runtime_category_keeps_its_OWN_exit_code(capsys):
    """Exit codes are a contract with scripts, and the table distinguishes
    'refused' from 'the runtime cannot enforce this'. Flattening them to 2 would
    be invisible in the message and wrong in a pipeline."""
    code = render_refusal(Refused(
        RefusalCategory.RUNTIME_CANNOT_ENFORCE, "apptainer cannot do network=none"))

    assert code == 3, f"runtime-cannot-enforce is 3, got {code}"


def test_a_REAL_bug_still_raises():
    """THE CONTROL, and it is the point.

    A handler that caught everything would satisfy every assertion above and
    turn a genuine `KeyError` into `refused: …` or, worse, a clean exit. The
    renderer re-raises anything that is not a refusal, so a crash still looks
    like a crash.
    """
    with pytest.raises(KeyError):
        render_refusal(KeyError("a real bug"))

    with pytest.raises(ValueError):
        render_refusal(ValueError("also a real bug"))


def test_main_ROUTES_an_escaping_refusal_through_the_renderer(monkeypatch, capsys):
    """Through `main()`, because the defect was that a command bypassed the
    decorator — asserting on the renderer alone would not have caught it.

    Drives the real entry point with a command whose body raises, which is what
    `botainer list` did on a disallowed state root.
    """
    import click

    from botainer.cli import main as main_mod

    @click.command("boom-for-test")
    def _boom():
        raise Refused(RefusalCategory.STATE_ROOT_NOT_ALLOWED, "nope, not there")

    main_mod.cli.add_command(_boom)
    try:
        code = main_mod.main(["boom-for-test"])
    finally:
        main_mod.cli.commands.pop("boom-for-test", None)

    err = capsys.readouterr().err
    assert "Traceback" not in err, f"a refusal reached the user as a traceback:\n{err}"
    assert "refused: state-root-not-allowed" in err and "nope, not there" in err, err
    assert code == 2, code


def test_main_does_NOT_swallow_a_real_crash(monkeypatch, capsys):
    """The control, at the entry point this time.

    If `main()` caught everything, a genuine defect would exit cleanly and the
    user would be told nothing at all — strictly worse than the traceback this
    commit removes.
    """
    import click

    from botainer.cli import main as main_mod

    @click.command("crash-for-test")
    def _crash():
        raise RuntimeError("a genuine defect")

    main_mod.cli.add_command(_crash)
    try:
        with pytest.raises(RuntimeError):
            main_mod.main(["crash-for-test"])
    finally:
        main_mod.cli.commands.pop("crash-for-test", None)
