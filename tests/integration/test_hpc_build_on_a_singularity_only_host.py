"""`hpc build` on a host that has `singularity` and not `apptainer`.

THE SHIPPED DOCS PROMISE THIS CONFIGURATION. `GETTING_STARTED-HPC.md` and
`docs/HPC-WORKFLOW.md` both list the prerequisite as `apptainer` (or
`singularity`), and `tools/pkg/install-hpc.sh` prints `botainer hpc build` as the
next step. Apptainer is the rename of Singularity and plenty of clusters still
ship only the old name.

WHAT HAPPENED. `hpc build` accepted such a host at its precheck — which tests
for EITHER binary — and then ran `["apptainer", "build"]`. `subprocess` raises
`FileNotFoundError`, the refusal handler catches only `Refused`, and the user
got a raw Python traceback on the documented image step. Its sibling
`botainer image build` resolved the name correctly all along, so this was drift
between two commands that do the same thing, not a missing feature.

WHAT IS NOT FIXED, and this test pins the disclosure rather than pretending
otherwise: the LAUNCH path still emits a literal `apptainer exec`. The sbatch
script is rendered on a login node and executed on a compute node that may have
a different binary, and the hpc-launcher carries a cage-integrity check that
refuses an argv not beginning with `["apptainer", "exec"]`. So a
singularity-only host can now BUILD and still cannot LAUNCH — and the build says
so, up front, because the alternative is finding out after twenty minutes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.adapters.apptainer import image_build_binary


@pytest.fixture
def singularity_only(tmp_path, monkeypatch):
    """A PATH with `singularity` and no `apptainer`.

    A real executable on a real PATH, not a patched `shutil.which`: the defect
    was that the resolved name and the executed name were different, and a
    patched `which` cannot show that — it would answer for both call sites and
    hide exactly the divergence under test.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "invoked.txt"
    sing = bindir / "singularity"
    sing.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
    sing.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))     # apptainer is NOT here
    return bindir, log


def test_the_resolver_picks_singularity_when_apptainer_is_absent(singularity_only):
    assert shutil.which("apptainer") is None
    assert image_build_binary() == "singularity"


def test_the_resolver_says_NONE_when_neither_is_present(tmp_path, monkeypatch):
    """Not 'apptainer' as a hopeful default — a caller must be able to tell."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert image_build_binary() is None


def test_hpc_build_RUNS_SINGULARITY_instead_of_tracebacking(
        singularity_only, tmp_path, monkeypatch):
    """The reported defect, driven through the real command.

    Asserts the ARGV that was actually executed, not that a name was resolved
    somewhere — the whole bug was that resolution and execution disagreed.
    """
    bindir, log = singularity_only
    executed = {}

    def _spy(cmd, *a, **kw):
        executed["cmd"] = list(cmd)
        return 0

    monkeypatch.setattr(subprocess, "call", _spy)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)

    from botainer.cli.main import cli
    res = CliRunner().invoke(cli, ["hpc", "build", "agent-claude"])

    assert executed.get("cmd"), (
        f"no build command was executed at all — exit {res.exit_code}\n"
        f"{res.output}")
    assert executed["cmd"][0] == "singularity", (
        f"hpc build ran {executed['cmd'][0]!r} on a host where only "
        f"`singularity` exists. That is FileNotFoundError, which the refusal "
        f"handler does not catch, so the user sees a raw traceback on the "
        f"documented image step.")


def test_hpc_build_WARNS_that_launching_still_needs_apptainer(
        singularity_only, tmp_path, monkeypatch):
    """Half a fix, disclosed as half a fix.

    Building now works on a singularity-only host; launching does not. Saying
    so costs one line before the build; not saying so costs the user twenty
    minutes and then an unexplained failure.
    """
    monkeypatch.setattr(subprocess, "call", lambda *a, **kw: 0)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)

    from botainer.cli.main import cli
    res = CliRunner().invoke(cli, ["hpc", "build", "agent-claude"])

    assert "launching currently requires" in res.output, (
        f"a singularity-only host was allowed to build with no word that the "
        f"image cannot be launched yet:\n{res.output}")
