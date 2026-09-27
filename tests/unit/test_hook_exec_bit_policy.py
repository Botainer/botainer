"""A `.py` hook does not need the execute bit; anything else does.

A `.py` hook is dispatched as `[sys.executable, script]` so it uses
botainer's interpreter and dependencies. This route reads the script;
it does not execute the file directly and therefore needs no execute bit.
Applying the general executable-file guard to it incorrectly refuses
readable 0o644 Python hooks.

`cli/auth.py` already carried the exemption (`if not is_py and not …`) and
`plugins/manifest.py` dispatches the same way, so `hooks.py` was the sibling
that had drifted — the same sibling-drift shape this project keeps finding.

WHY NO TEST CAUGHT IT: reverting all four call sites to `os.access` failed ZERO
of 821 collected tests. The call sites had no coverage at all; only the helper
did. That is what this file fixes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.refusal import Refused
from botainer.plugins import hooks


def _run(script: Path):
    return hooks.run_hook(
        plugin_name="testplug",
        hook_when="pre_session",
        script_path=script,
        env={},
        agent_writable_roots=(),
    )


def test_a_non_executable_PY_hook_still_runs(tmp_path) -> None:
    """THE REGRESSION. This shape worked, then was refused, and works again.

    `.py` hooks are handed to `sys.executable`; the bit is irrelevant to them.
    """
    script = tmp_path / "hook.py"
    script.write_text("print('{}')\n")
    script.chmod(0o644)

    result = _run(script)

    assert result.rc == 0, result
    assert result.parsed_contribution == {}, (
        f"the hook ran but its stdout was not parsed as the empty JSON "
        f"contribution it printed: {result!r}")


def test_a_non_executable_SHELL_hook_is_REFUSED(tmp_path) -> None:
    """The other half — and the reason the check exists at all.

    A `.sh` hook IS exec'd via its shebang, so a missing bit means
    `bad interpreter: Permission denied` buried in output. Refusing early with
    a sentence the user can act on is the whole point (task #118).
    """
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\necho '{}'\n")
    script.chmod(0o644)

    with pytest.raises(Refused) as exc:
        _run(script)

    msg = str(exc.value)
    assert "not executable" in msg, msg
    assert "chmod +x" in msg, msg


def test_an_executable_shell_hook_runs(tmp_path) -> None:
    """The control. Without it the rule above could refuse every shell hook."""
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\necho '{}'\n")
    script.chmod(0o755)

    result = _run(script)

    assert result.rc == 0, result
    assert result.parsed_contribution == {}, result


def test_the_exemption_and_the_interpreter_ASK_THE_SAME_FUNCTION(tmp_path) -> None:
    """The pairing, pinned by an input the two old spellings DISAGREE on.

    THE TEST THIS REPLACES ASSERTED NOTHING, and a refuting review proved it:
    swapping the exemption to `Path(path).suffix == ".py"` while the interpreter
    dispatch kept `endswith(".py")` left all 14 tests in this file green. It was a
    near-duplicate of the first test, not a drift guard, because every filename it
    used made the two spellings agree.

    A FILE LITERALLY NAMED `.py` is where they part company: `Path(".py").suffix`
    is `""` — a leading dot makes it a hidden file with no suffix — while
    `".py".endswith(".py")` is True. So under the drifted pair, this file would be
    refused for its mode and then handed to python anyway, or exempted and then
    exec'd. Nobody ships a hook called `.py`; that is not the point. The point is
    that it is the input which can tell one predicate from two.

    They are now ONE function, so the pairing holds by construction — and this
    test fails if someone re-inlines either copy.
    """
    import inspect as _inspect

    # The discriminating input: exempt from the bit AND run through our
    # interpreter, or neither. `rc == 0` with no execute bit proves both.
    odd = tmp_path / ".py"
    odd.write_text("print('{}')\n")
    odd.chmod(0o600)

    result = _run(odd)
    assert result.rc == 0, (
        f"a hook named `.py` with mode 0o600 was refused, which means the "
        f"exemption and the interpreter dispatch no longer answer the same "
        f"question — the exact drift this test exists to catch: {result!r}")
    assert result.parsed_contribution == {}, result

    # And structurally: `run_hook` must not carry its own copy of the test.
    #
    # ASSERTED ON THE CALLER'S SOURCE, not on a count over the module — my first
    # attempt counted `endswith(".py")` across the whole file and failed on the
    # explanation in the helper's own docstring. Counting a literal in prose is
    # the same theatre a review broke in another test an hour earlier; the
    # question worth asking is whether the CALLER delegates.
    caller = _inspect.getsource(hooks.run_hook)
    assert 'endswith(".py")' not in caller, (
        "`run_hook` carries its own copy of the suffix test again; both the "
        "exemption and the interpreter choice must come from "
        "`_runs_under_our_interpreter` or they can disagree")
    assert caller.count("_runs_under_our_interpreter(") == 2, (
        f"the shared predicate is consulted "
        f"{caller.count('_runs_under_our_interpreter(')} times in run_hook; it "
        f"decides BOTH the exemption and the interpreter, so it is exactly 2")


def test_a_shell_hook_named_py_SOMETHING_still_needs_the_bit(tmp_path) -> None:
    """The other side of the same input class, so the fix cannot be "always exempt".

    `py.sh` and `python-wrapper` contain the letters but are not `.py`, so they
    ARE exec'd and DO need the bit. A predicate loosened to `"py" in name` would
    pass the test above and silently stop requiring the execute bit on shell
    hooks — which is the refusal this file's whole subject matter exists for.
    """
    sh = tmp_path / "py.sh"
    sh.write_text("#!/bin/sh\necho '{}'\n")
    sh.chmod(0o644)          # readable, NOT executable

    with pytest.raises(Refused) as exc:
        _run(sh)
    assert "not executable" in str(exc.value), exc.value


def test_doctor_does_not_call_a_working_PY_hook_broken(tmp_path, monkeypatch) -> None:
    """`doctor` must mirror `run_hook`, or it sends people to fix a non-problem.

    THE SECOND HALF OF THE SAME REGRESSION, found by a refuting review after I
    had fixed only `run_hook`. `doctor`'s `plugins.hooks_not_executable` finding
    says "`botainer start` refuses with 'hook script not executable'" — which is
    now false for a `.py` hook, because `start` runs it fine. Every hook in
    every bundled plugin is `.py`, so EVERY firing of that finding would have
    been wrong, and it is an `err` that makes `doctor` exit 1.

    Two surfaces disagreeing about one rule is the sibling-drift shape this
    project keeps finding; this pins them together.

    IT DID NOT PIN THEM AT FIRST. This test used to RE-IMPLEMENT doctor's
    predicate in its own body and assert on the copy, while the commit message
    claimed it pinned the two surfaces together. A loop-tzar checkpoint
    measured what that was worth:
    deleting the `.py` exemption from `cli/doctor.py` left this file at 5
    passed and the whole `-k "doctor or hook"` selection at 315 passed. It
    tested a line of test code. It now drives `collect_plugin_hook_findings`,
    the function `botainer doctor` actually calls.
    """
    import types

    from botainer.cli import doctor as doctor_mod
    from botainer.plugins import lifecycle as lifecycle_module

    plugin_dir = tmp_path / "agent-testplug"
    (plugin_dir / "hooks").mkdir(parents=True)
    py = plugin_dir / "hooks" / "h.py"
    py.write_text("print('{}')\n")
    py.chmod(0o644)
    (plugin_dir / "botainer-plugin.yaml").write_text(
        "name: agent-testplug\n"
        "hooks:\n"
        "  - when: pre_session\n"
        "    script: hooks/h.py\n")

    monkeypatch.setattr(
        lifecycle_module, "list_installed",
        lambda: [types.SimpleNamespace(name="agent-testplug",
                                       plugin_dir=plugin_dir)])

    assert _run(py).rc == 0, "precondition: run_hook accepts this hook"

    findings = doctor_mod.collect_plugin_hook_findings()

    offenders = [f for f in findings if "not_executable" in f.check]
    assert not offenders, (
        f"doctor reports a hook broken that run_hook runs successfully — the "
        f"two surfaces have drifted apart again: "
        f"{[(f.level, f.check, f.detail) for f in offenders]}")
