"""A scheduler rejection must reach the human AND the agent as a reason.

Before a rejected job produced, for the agent:

    internal error handling request (CalledProcessError)

and for the user: nothing at all, because an auto-started dispatcher has stdout
and stderr on /dev/null. So "Slurm says your account is invalid" — the single
most common first-run failure, since the generated template ships
`account: ""` — was unreadable from every direction at once.

The dispatcher's `except Exception` withholds internals from the caged agent on
purpose, so the fix is not to loosen that: sbatch failure becomes a Refused,
whose message IS forwarded verbatim, carrying the scheduler's own words with
host paths stripped out.
"""
from __future__ import annotations

import subprocess

import pytest

from botainer.cli import hpc as hpc_cli
from botainer.core.refusal import RefusalCategory, Refused


def _fail_sbatch(monkeypatch, stderr: str, rc: int = 1):
    def fake_run(argv, **kw):
        raise subprocess.CalledProcessError(rc, argv, output="", stderr=stderr)
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_rejection_carries_slurms_own_reason(tmp_path, monkeypatch):
    _fail_sbatch(monkeypatch,
                 "sbatch: error: Batch job submission failed: Invalid account "
                 "or account/partition combination specified\n")

    with pytest.raises(Refused) as ei:
        hpc_cli.sbatch_submit(tmp_path / "j.sbatch", profile_name="cpu")

    msg = str(ei.value)
    assert "Invalid account" in msg, (
        f"the scheduler's reason did not survive: {msg!r}")
    assert "CalledProcessError" not in msg
    assert ei.value.category == RefusalCategory.JOB_SUBMIT_REJECTED


def test_missing_account_names_the_profile_and_the_fix(tmp_path, monkeypatch):
    """The DEFAULT path: the generated template writes account: ""."""
    _fail_sbatch(monkeypatch,
                 "sbatch: error: Batch job submission failed: Invalid account\n")

    with pytest.raises(Refused) as ei:
        hpc_cli.sbatch_submit(tmp_path / "j.sbatch", profile_name="gpu",
                              has_account=False)

    msg = str(ei.value)
    assert "gpu" in msg, "did not say WHICH profile is missing an account"
    assert "sacctmgr" in msg, "did not say how to find the account"


def test_host_paths_are_stripped_before_the_agent_sees_them(tmp_path, monkeypatch):
    _fail_sbatch(monkeypatch,
                 "sbatch: error: unable to open /home/someone/.botainer/"
                 "hpc-jobs/abc-123/run/j.sbatch\n")

    with pytest.raises(Refused) as ei:
        hpc_cli.sbatch_submit(tmp_path / "j.sbatch")

    msg = str(ei.value)
    assert "/home/someone" not in msg and "abc-123" not in msg, (
        f"a host path reached the caged agent: {msg!r}")
    assert "<path>" in msg


def test_control_characters_cannot_reach_the_agent(tmp_path, monkeypatch):
    _fail_sbatch(monkeypatch, "sbatch: error: \x1b[31mred\x1b[0m \x00 boom\n")

    with pytest.raises(Refused) as ei:
        hpc_cli.sbatch_submit(tmp_path / "j.sbatch")

    msg = str(ei.value)
    assert "\x1b" not in msg and "\x00" not in msg


def test_reason_is_length_capped(tmp_path, monkeypatch):
    _fail_sbatch(monkeypatch, "sbatch: error: " + ("x" * 5000))

    with pytest.raises(Refused) as ei:
        hpc_cli.sbatch_submit(tmp_path / "j.sbatch")

    assert len(str(ei.value)) < 600


def test_no_sbatch_says_wrong_host_not_filenotfound(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "sbatch")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(Refused) as ei:
        hpc_cli.sbatch_submit(tmp_path / "j.sbatch")

    assert "LOGIN node" in str(ei.value)


def test_both_submit_paths_share_one_helper():
    """Cold submit and the warm pool each had their OWN sbatch call.

    Sibling drift is this project's most-repeated defect: a fix to one would
    not reach the other. Asserted on the module, not on prose.
    """
    import ast
    import inspect
    src = inspect.getsource(hpc_cli)
    tree = ast.parse(src)
    direct = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_run = (isinstance(fn, ast.Attribute) and fn.attr == "run"
                  and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess")
        if not is_run or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.List) and first.elts:
            head = first.elts[0]
            if isinstance(head, ast.Constant) and head.value == "sbatch":
                direct += 1
    assert direct == 1, (
        f"{direct} direct sbatch invocations — they must all go through "
        "sbatch_submit() or the error handling drifts apart again")
