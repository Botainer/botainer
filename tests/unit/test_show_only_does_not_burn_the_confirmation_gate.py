"""Being SHOWN a capability grant is not consenting to it.

THE DEFECT, reproduced before the fix against this suite's own `_make_spec`:

    gate before anything     : True  (first launch of this project)
    show-only here/attach    : proceeds
    gate after the show-only : False  <- the next INTERACTIVE submit never asks

`hpc submit --mode=here` and `hpc attach` run where there is no terminal, so
`submit.py` called `_consent(interactive=False)` → `auto_yes=True` →
`print_and_maybe_confirm` → `record_shown(spec)`. The first-launch
capability-confirmation gate was satisfied on behalf of a user who was never
asked — and a FAILED here/attach burned it for a session that never ran.

ONE FLAG CARRYING TWO MEANINGS. Callers computed
`auto_yes = args.yes or not interactive`:

  * `--yes` — the user authorised the grant sight-unseen. That IS consent.
  * no TTY — there is nothing to answer a prompt with. That is not.

Both suppress the prompt, which is why one flag looked sufficient; only one may
record consent. `print_and_maybe_confirm` now takes the two FACTS
(`pre_authorised`, `interactive`) and derives both behaviours itself, so the
conflation is unrepresentable at a call site rather than merely corrected.

A PREVIOUS CHANGE SAW HALF OF THIS. The auto-confirm message already said
"either --yes was passed, or this is a non-interactive session", because
claiming "--yes passed" was wrong. It fixed what the user is TOLD and left
`record_shown` firing two lines below. `record_shown`'s own docstring says
"After successful launch, remember the CONFIRMED capability fingerprint" — the
code had drifted from its own stated contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from botainer.inspect import capability_summary as cs

sys.path.insert(0, str(Path(__file__).parent))
from test_capability_summary import _make_spec  # noqa: E402


@pytest.fixture
def spec(tmp_path, monkeypatch):
    """A real state root, so `determine_gate` reads real project meta."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    from botainer.state import dir as state_dir
    state_dir.ensure_user_state_dir(create_if_missing=True)
    return _make_spec()


def test_a_show_only_launch_leaves_the_gate_ARMED(spec):
    """THE DEFECT. The user was never asked, so nothing was confirmed."""
    assert cs.determine_gate(spec).confirm is True, "precondition: gate armed"

    proceeded = cs.print_and_maybe_confirm(
        spec, quiet=True, pre_authorised=False, interactive=False, on_sbatch_path=False)

    assert proceeded is True, (
        "a no-TTY launch must still proceed — refusing here would break "
        "`hpc submit --mode=here` on every first launch")
    assert cs.determine_gate(spec).confirm is True, (
        "a show-only launch satisfied the first-launch gate; the next "
        "INTERACTIVE submit will skip the confirmation the user never gave")


def test_explicit_yes_still_records(spec):
    """The control, and the half that MUST keep working.

    Without it, the fix could simply stop recording ever, which would re-ask
    on every launch and train people to type y without reading.
    """
    assert cs.determine_gate(spec).confirm is True

    cs.print_and_maybe_confirm(
        spec, quiet=True, pre_authorised=True, interactive=False, on_sbatch_path=False)

    assert cs.determine_gate(spec).confirm is False, (
        "`--yes` is a real pre-authorisation and must consume the gate")


def test_an_interactive_yes_still_records(spec, monkeypatch):
    """The other half: a human who answers 'y' has consented."""
    monkeypatch.setattr(cs.click, "prompt", lambda *a, **k: "y")

    cs.print_and_maybe_confirm(
        spec, quiet=False, pre_authorised=False, interactive=True, on_sbatch_path=False)

    assert cs.determine_gate(spec).confirm is False


def test_an_interactive_NO_does_not_record(spec, monkeypatch):
    """Declining must not arm-then-disarm the gate."""
    monkeypatch.setattr(cs.click, "prompt", lambda *a, **k: "n")

    proceeded = cs.print_and_maybe_confirm(
        spec, quiet=False, pre_authorised=False, interactive=True, on_sbatch_path=False)

    assert proceeded is False
    assert cs.determine_gate(spec).confirm is True


def test_json_mode_show_only_also_leaves_the_gate_armed(spec, capsys):
    """The JSON branch had its own copy of the same `gate and auto_yes` shape.

    Three branches recorded; fixing two would have left the third to be found
    later by someone scripting `--json`.
    """
    cs.print_and_maybe_confirm(
        spec, as_json=True, pre_authorised=False, interactive=False, on_sbatch_path=False)
    capsys.readouterr()

    assert cs.determine_gate(spec).confirm is True


def test_json_mode_with_explicit_yes_records(spec, capsys):
    """…and its `--yes` half still works."""
    cs.print_and_maybe_confirm(
        spec, as_json=True, pre_authorised=True, interactive=False, on_sbatch_path=False)
    capsys.readouterr()

    assert cs.determine_gate(spec).confirm is False


def test_every_CALL_SITE_passes_the_facts_apart(spec):
    """Checks the call's KEYWORDS via AST, in both callers.

    The defect lived in how `submit.py` CALLED this — `auto_yes=(args.yes or
    not interactive)`. Tests that only exercise `print_and_maybe_confirm`
    would all pass while a caller kept merging the two facts, which is exactly
    the mistake this file exists to prevent.

    WHY NOT DRIVE THE REAL CALLER. `_consent` is a closure inside
    `submit.py::main`, reached only on a non-dry-run `here`/`attach` — which
    would attempt an actual launch. This container has no apptainer and no
    Slurm, so that path is not reachable here; saying so beats pretending the
    coverage exists.

    AST rather than substring: this asserts the keywords the call actually
    passes, so reformatting cannot fool it and neither can a comment that
    happens to contain the old text.

    THIS IS NOW BACKUP, NOT THE PRIMARY CHECK, and the docstring above
    overstated what it can do. It pins keyword NAMES, not values — writing
    `pre_authorised=(args.yes or not interactive)` re-creates the original
    defect with BOTH keywords present, and this test and its six siblings all
    still pass. Measured.

    `tests/integration/test_here_mode_does_not_burn_the_consent_gate.py` is the
    primary check: it drives the real `hpc submit --mode=here` and asserts the
    fingerprint on disk, and it FAILS on that re-merge. The claim above that
    `_consent` is "not reachable here" was also wrong — it is, and that file
    carries the recipe.

    Kept because it is fast and it catches the old SPELLING cheaply, which is
    worth having as long as nobody mistakes it for the end-to-end one.
    """
    import ast

    roots = {
        "submit.py": (Path(__file__).resolve().parents[2]
                      / "plugins/hpc-launcher/host_helper/submit.py"),
        "start.py": (Path(__file__).resolve().parents[2]
                     / "botainer/cli/start.py"),
    }
    checked = 0
    for label, path in roots.items():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "print_and_maybe_confirm":
                continue
            kwargs = {k.arg for k in node.keywords}
            assert "auto_yes" not in kwargs, (
                f"{label} still passes the merged `auto_yes` flag")
            assert {"pre_authorised", "interactive"} <= kwargs, (
                f"{label} does not pass both facts; got {sorted(kwargs)}")
            checked += 1
    assert checked == 2, (
        f"expected both callers, found {checked} — a call site moved or a new "
        f"one appeared unchecked")
