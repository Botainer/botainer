"""`/scratch` may live outside the state root — and only exactly there.

WHY THIS EXISTS. `ProjectPaths` collapsed three things with three different
lifetimes into one root:

    state     credentials, project uuid, plugins, .sif   small, MUST persist
    packages  pip/npm caches                             large, rebuildable
    scratch   the agent's bulk intermediates             large, disposable

On HPC those belong on different filesystems — state on $HOME (durable,
quota-limited), scratch on the cluster's scratch (big, fast, auto-purged). One
root forces the user to pick which to get wrong: put it on $HOME and bulk data
eats the quota; put it on scratch and the purge takes the credentials.

`DN-008:245` said `scratch_template` "resolves at session
compose" from the beginning. It was printed by `hpc setup`'s storage section and
never resolved, so the product ADVISED a two-tier layout it could not implement.

The containment guard is the reason this was expensive rather than trivial: it
required every bind source under the state root, so the single-root assumption
had been baked into a SECURITY control. These tests pin the corrected shape —
each source contained by ITS OWN declared root, nothing loosened.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.state.dir import ProjectPaths


def test_scratch_defaults_under_the_state_root():
    """Unchanged behaviour when no root is declared — the Mac/default case."""
    p = ProjectPaths(base=Path("/state/u1"))
    assert p.scratch_dir == Path("/state/u1/scratch")


def test_scratch_root_moves_only_scratch():
    """State, home and packages must NOT follow scratch onto a purged volume.

    This is the whole point: the credentials stay on durable storage. A change
    that moved `base` instead would reintroduce the exact hazard `hpc setup`
    warns about — scratch auto-purge wiping credentials and project identity.
    """
    p = ProjectPaths(base=Path("/state/u1"),
                     scratch_root=Path("/vast/scratch/me/u1"))
    assert p.scratch_dir == Path("/vast/scratch/me/u1")
    assert p.data_dir == Path("/state/u1/data")
    assert p.home_dir == Path("/state/u1/home")
    assert p.packages_dir == Path("/state/u1/packages")


def test_the_containment_guard_still_refuses_an_escape(tmp_path):
    """Relaxing WHERE scratch may live must not relax the DEFAULT layout.

    REWRITTEN. The previous version built a symlinked `scratch_root`
    and asserted the escape was refused. It SKIPPED on every run since it was
    written — "symlink resolved to itself; platform cannot express this" — and
    therefore asserted nothing at all, while looking like it pinned a security
    property. The skip reason blamed the platform; the real cause is
    structural, and worth stating plainly:

        `scratch_dir` RETURNS `scratch_root` when one is set, so the guard's
        `resolved.relative_to(root)` compares a path to itself and can never
        fail. Containment is VACUOUS for any relocated component.

    That is not a hole, but it is a different mechanism: a relocated root
    arrives from host-side config the launcher resolves before the container
    exists, and the caged agent has no path to set it. A trust boundary, not a
    filter — and CLAUDE.md requires saying which.

    What the guard genuinely protects is the DEFAULT layout, where the source
    is `base/scratch`, a path inside a directory the agent writes to across
    sessions. That is the umbrella-bind class, and this test now pins it.
    See tests/integration/test_packages_home_roots.py for the same pair of
    properties stated for packages.
    """
    from botainer.core.refusal import Refused
    from botainer.hpc import jobs as _jobs

    state_root = tmp_path / "state"
    base = state_root / "u1"
    outside = tmp_path / "elsewhere"
    proj = tmp_path / "proj"
    for d in (base, outside, proj):
        d.mkdir(parents=True)
    (base / "packages").mkdir()
    (base / "scratch").symlink_to(outside)      # planted escape, default layout

    with pytest.raises(Refused) as ei:
        _jobs.child_core_binds(proj, ProjectPaths(base=base), state_root)
    assert "outside" in str(ei.value), str(ei.value)
