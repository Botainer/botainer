"""#54 P1: the directional job-dispatcher mailbox (INV-1)."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from botainer.core.spec import BindMode
from botainer.hpc import jobs
from botainer.state import dir as state_dir

UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    return state_dir.ensure_user_state_dir(create_if_missing=True)


def test_ensure_mailbox_creates_three_dirs_0700(paths) -> None:
    mb = jobs.ensure_mailbox(paths, UUID)
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        assert d.is_dir()
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_mailbox_lives_outside_the_container_bound_subtree(paths) -> None:
    """INV-1: the mailbox (esp. host-written run/ + out/) must NOT sit under
    state/<uuid>/, which the hpc-launcher binds RW into the agent container —
    else the caged agent could pre-plant a symlink the host writes through."""
    mb = jobs.ensure_mailbox(paths, UUID)
    bound_subtree = paths.for_project(UUID).base  # state/<uuid>/  (bound RW)
    for d in (mb.root, mb.in_dir, mb.out_dir, mb.run_dir):
        assert bound_subtree not in d.parents and d != bound_subtree, (
            f"{d} is inside the container-bound {bound_subtree}"
        )
    # It lives under the host-only hpc-jobs root.
    assert mb.root == paths.hpc_jobs_dir(UUID)


def test_binds_are_directional_in_rw_out_ro(paths) -> None:
    mb = jobs.ensure_mailbox(paths, UUID)
    binds = jobs.mailbox_binds(mb)
    by_target = {b.target: b for b in binds}
    assert by_target["/jobs/in"].mode == BindMode.RW
    assert by_target["/jobs/in"].source == str(mb.in_dir)
    assert by_target["/jobs/out"].mode == BindMode.RO
    assert by_target["/jobs/out"].source == str(mb.out_dir)


def test_run_dir_is_never_bound(paths) -> None:
    """run/ (generated .sbatch + SLURM --output) is host-private — no bind may
    expose it, and no bind source may be its parent (which would pull it in)."""
    mb = jobs.ensure_mailbox(paths, UUID)
    sources = [Path(b.source) for b in jobs.mailbox_binds(mb)]
    for src in sources:
        assert src != mb.run_dir, "run/ must not be a bind source"
        assert mb.run_dir not in [src, *src.parents], "run/ under a bind source"
        # And the reverse: no bind source is an ANCESTOR of run/ (parent-bind).
        assert src not in mb.run_dir.parents, f"{src} is a parent of run/ — would expose it"
    # Only two binds, only the two leaves — never root/.
    targets = {b.target for b in jobs.mailbox_binds(mb)}
    assert targets == {"/jobs/in", "/jobs/out"}
    assert str(mb.root) not in [b.source for b in jobs.mailbox_binds(mb)]
