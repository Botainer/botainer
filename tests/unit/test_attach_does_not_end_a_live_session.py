"""Leaving the viewer is not the session ending.

The two attach branches have different lifetimes: foreground execution
waits for the agent, while a persistent-session viewer can leave sooner.

THE CLAIM IN THE CODE, at `_do_attach`:

    # THE SESSION IS OVER. `srun` is synchronous, so by the time it returns the
    # agent has exited — which is exactly when post_session belongs

That is TRUE on one branch and FALSE on the other, and the false one is the
only branch anybody attaches on:

    nudge OFF   srun … <apptainer exec …>        srun returns when the AGENT exits.
    nudge ON    srun … screen -r botainer-<id>   srun returns when the VIEWER goes.

Nobody detaches on purpose. A laptop sleeping, wifi dropping, a closed lid or an
SSH timeout all detach you — screen exists so those do not kill your work. So on
the nudge branch `srun` returning is the ORDINARY case and says nothing about
whether the agent is still running.

`launched=False` then reaches the single teardown site in `main()`, which runs
this project's post_session hooks against a session that is still alive. The
hook that matters is the shared-credential reconcile: it fires early, mid
session, while token rotation is in play.

WHY THE OBVIOUS FIX IS WRONG, and this is recorded in the code already: simply
returning `launched=True` here was tried, and it skipped the reconcile on this
path entirely — a refresh inside the container was never promoted to the shared
login, producing the silent cross-project "login expired" symptom that hook
exists to prevent. So the fix cannot be "never tear down"; it has to DISTINGUISH
the viewer leaving from the session ending.

THE SIGNAL IS SLURM, and 79a56bc is what makes it exact. The batch process IS
the screen session now (`exec screen -D -m`), so the job being alive and the
session being alive are the same fact. Ask `squeue`.

FAIL-SAFE DIRECTION, stated because it is a real choice: if the job state cannot
be determined — no squeue, a timeout, an error — this TEARS DOWN, i.e. keeps
today's behaviour. Skipping the reconcile silently is the worse of the two
failures, and it is the one that was already shipped and fixed once.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


def _load():
    """submit.py does `from _common import ...` (its sibling on disk), so
    _common must be staged in sys.modules under that bare name first — the
    same dance tests/unit/test_hpc_launcher.py::_load_submit does."""
    cspec = importlib.util.spec_from_file_location("hpcl_common_attach",
                                                   HELPER / "_common.py")
    assert cspec and cspec.loader
    common = importlib.util.module_from_spec(cspec)
    sys.modules["hpcl_common_attach"] = common
    cspec.loader.exec_module(common)
    sys.modules["_common"] = common

    spec = importlib.util.spec_from_file_location("hpc_submit_attach", HELPER / "submit.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_submit_attach"] = mod
    spec.loader.exec_module(mod)
    return mod


def _plan(mod, *, nudge: bool):
    return mod.SubmissionPlan(
        project_root=Path("/tmp/p"), project_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        state_root=Path("/tmp/s"), profile="default", partition="", account="",
        time_minutes=60, cpus=1, memory_gb=None, gpus=0, gpu_type=None,
        apptainer_image="x.sif", submission_mode="attach", existing_jobid="4242",
        nudge_enabled=nudge,
        agent_exec_argv=("apptainer", "exec", "--containall", "--cleanenv",
                         "--no-privs", "--drop-caps", "all", "x.sif", "AGENT"),
    )


@pytest.fixture
def mod(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "have_slurm", lambda: True)
    # `srun` returns 0 — the viewer went away. Says nothing about the agent.
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    return m


def test_a_live_job_means_the_session_did_NOT_end(mod, monkeypatch):
    """THE DEFECT. Detached viewer + job still running must NOT tear down."""
    monkeypatch.setattr(mod, "_attached_job_still_running", lambda jobid: True,
                        raising=False)
    out = mod._do_attach(_plan(mod, nudge=True), "4242", dry_run=False)
    assert out.launched is True, (
        "Leaving the viewer was treated as the session ending. The agent is "
        "still running on the compute node, and this runs post_session hooks "
        "against it — including the shared-credential reconcile, mid-session."
    )


def test_a_finished_job_still_tears_down(mod, monkeypatch):
    """THE OTHER DIRECTION, which the previous fix exists for.

    When the job really has ended, the reconcile MUST still run — skipping it
    is the silent cross-project "login expired" symptom.
    """
    monkeypatch.setattr(mod, "_attached_job_still_running", lambda jobid: False,
                        raising=False)
    out = mod._do_attach(_plan(mod, nudge=True), "4242", dry_run=False)
    assert out.launched is False, (
        "The job has ended, so the shared-credential reconcile must run.")


def test_the_no_nudge_branch_is_unchanged(mod, monkeypatch):
    """Without nudge, `srun` runs the agent itself — its return DOES mean the
    agent exited, and that behaviour is correct and must not move."""
    monkeypatch.setattr(mod, "_attached_job_still_running", lambda jobid: True,
                        raising=False)
    out = mod._do_attach(_plan(mod, nudge=False), "4242", dry_run=False)
    assert out.launched is False, (
        "The no-nudge branch runs the agent under srun; its return really is "
        "the agent exiting, so teardown must still happen.")


def test_an_unknown_job_state_tears_down(mod, monkeypatch):
    """FAIL-SAFE DIRECTION. No squeue / timeout / error => keep today's
    behaviour rather than silently skipping the reconcile."""
    monkeypatch.setattr(mod, "have_slurm", lambda: False)
    monkeypatch.setattr(mod, "_attached_job_still_running",
                        lambda jobid: False, raising=False)
    out = mod._do_attach(_plan(mod, nudge=True), "4242", dry_run=False)
    assert out.launched is False, (
        "With the job state unknown this must fall back to tearing down.")
