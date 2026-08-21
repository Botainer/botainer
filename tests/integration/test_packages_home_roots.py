"""`/packages` and `/home/user` may live outside the state root too.

WHY, and why it is NOT just "more of the scratch change".

`scratch_root` fixed the BYTE axis: the agent's bulk intermediates
belong on a big, purged filesystem rather than a quota-limited $HOME.

It did nothing for the INODE axis, which is a different failure with a
different culprit. On the maintainer's cluster hit a hard
**500,000-file cap** on $HOME. The component that exhausted it was
`state/<uuid>/packages/` — conda/pip trees, only ~8.6 GB but hundreds of
thousands of tiny files. Every session then died writing `.meta.json.tmp`:

    OSError [Errno 122] Disk quota exceeded

`/scratch` was already relocated and helped not at all, because scratch is the
byte-heavy component and this was a file-count failure. `packages_dir` was
hardcoded `base/packages` with no override, so the one component that mattered
was the one that could not move.

THE REJECTED ALTERNATIVE, pinned here because it is the tempting one: let
`MY_BOTAINER` itself point off $HOME. That relocates `shared-auth/` — the OAuth
credential — onto group-shared project space, trading a PROPERTY ("credentials
live in your private home") for a FILTER ("the permissions on that shared
directory must stay correct forever"). Per-component roots buy the inodes
without spending the guarantee. `test_moving_a_component_does_not_move_credentials`
is that decision, as a test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.refusal import Refused
from botainer.state.dir import ProjectPaths


def test_defaults_are_unchanged_when_no_root_is_declared():
    """The Mac/default case: one root, exactly as before."""
    p = ProjectPaths(base=Path("/state/u1"))
    assert p.packages_dir == Path("/state/u1/packages")
    assert p.home_dir == Path("/state/u1/home")


def test_packages_root_moves_only_packages():
    p = ProjectPaths(base=Path("/state/u1"),
                     packages_root=Path("/project/me/u1/packages"))
    assert p.packages_dir == Path("/project/me/u1/packages")
    assert p.home_dir == Path("/state/u1/home")
    assert p.scratch_dir == Path("/state/u1/scratch")
    assert p.data_dir == Path("/state/u1/data")


def test_home_root_moves_only_home():
    p = ProjectPaths(base=Path("/state/u1"),
                     home_root=Path("/project/me/u1/home"))
    assert p.home_dir == Path("/project/me/u1/home")
    assert p.packages_dir == Path("/state/u1/packages")
    assert p.data_dir == Path("/state/u1/data")


def test_all_three_can_move_independently():
    p = ProjectPaths(base=Path("/state/u1"),
                     scratch_root=Path("/vast/scratch/u1"),
                     packages_root=Path("/project/pkg/u1"),
                     home_root=Path("/project/home/u1"))
    assert p.scratch_dir == Path("/vast/scratch/u1")
    assert p.packages_dir == Path("/project/pkg/u1")
    assert p.home_dir == Path("/project/home/u1")
    # The precious third stays put.
    assert p.data_dir == Path("/state/u1/data")
    assert p.meta_path.parent == Path("/state/u1")


def test_moving_a_component_does_not_move_credentials():
    """THE DESIGN DECISION, as a test.

    The alternative on the table was relocating the whole state root, which
    would have taken `data/` — the per-project credential store — with it onto
    group-shared space. Whatever else moves, this must not.
    """
    p = ProjectPaths(base=Path("/home/me/.botainer/state/u1"),
                     packages_root=Path("/gpfs/project/shared/u1/packages"),
                     home_root=Path("/gpfs/project/shared/u1/home"),
                     scratch_root=Path("/vast/scratch/u1"))
    assert str(p.data_dir).startswith("/home/me/"), (
        f"credentials followed a relocated component to {p.data_dir} — this is "
        "the whole reason per-component roots were chosen over a movable "
        "state root")


# ── the containment guard: the regression that cost a working dispatcher ──


def _pkg_root_used_by_child_binds(proj_paths, state_root):
    """What root does the child-job guard check `packages` against?"""
    from botainer.hpc import jobs as _jobs
    return _jobs, proj_paths, state_root


def test_relocated_packages_is_not_refused_by_the_child_job_guard(tmp_path):
    """d734529, exactly: the guard checked every source against the STATE root.

    The first component to move therefore made a legitimate bind illegal, and
    the dispatcher refused EVERY cycle with [mount-source-denied] on a real
    cluster. That happened for scratch. Adding `packages_root` without
    teaching the same guard reproduces it verbatim — silently, at launch.
    """
    from botainer.hpc import jobs as _jobs

    state_root = tmp_path / "state"
    pkg_root = tmp_path / "project-vol" / "u1" / "packages"
    proj = tmp_path / "proj"
    for d in (state_root / "u1", pkg_root, proj):
        d.mkdir(parents=True)

    paths = ProjectPaths(base=state_root / "u1", packages_root=pkg_root)
    try:
        binds = _jobs.child_core_binds(proj, paths, state_root)
    except Refused as exc:
        pytest.fail(
            f"a relocated /packages was refused by the child-job containment "
            f"guard: {exc}. This is the d734529 regression — the guard must "
            f"check each source against ITS OWN root.")
    targets = {b.target for b in binds}
    assert "/packages" in targets


def test_default_packages_symlinking_out_of_the_state_root_is_refused(tmp_path):
    """The case where the guard has real work to do, and still does it.

    With NO packages_root, the source is `base/packages` — a path inside a
    directory the agent can write to across sessions. A symlink planted there
    is the umbrella-bind class, and must be refused. Relaxing where packages
    MAY live must not relax this.
    """
    from botainer.hpc import jobs as _jobs

    state_root = tmp_path / "state"
    base = state_root / "u1"
    outside = tmp_path / "elsewhere"
    proj = tmp_path / "proj"
    for d in (base, outside, proj):
        d.mkdir(parents=True)
    (base / "packages").symlink_to(outside)      # the planted escape

    with pytest.raises(Refused) as ei:
        _jobs.child_core_binds(proj, ProjectPaths(base=base), state_root)
    assert "outside" in str(ei.value)


def test_containment_is_VACUOUS_for_a_relocated_component_and_that_is_the_design(
        tmp_path):
    """Stated as a test so nobody mistakes this guard for more than it is.

    `packages_dir` RETURNS `packages_root` when set, so `resolved.relative_to(
    root)` compares a path to itself and can never fail. The same is true of
    scratch and home. The containment check therefore protects the DEFAULT
    layout only.

    That is not a hole, but it IS a different mechanism, and CLAUDE.md requires
    saying which: the protection for a relocated component is that its root
    comes from host-side config the launcher resolves — cluster.yaml, read
    before the container exists — and the caged agent has no path to set it.
    A trust boundary, not a filter.

    This test exists because the equivalent scratch test
    (test_scratch_root_containment.py) was written to assert an escape IS
    refused, and consequently SKIPPED on every run since it was written,
    blaming "platform cannot express this". It asserted nothing while looking
    like it pinned a security property.
    """
    from botainer.hpc import jobs as _jobs

    state_root = tmp_path / "state"
    base = state_root / "u1"
    pkg_root = tmp_path / "project-vol" / "u1"
    outside = tmp_path / "elsewhere"
    proj = tmp_path / "proj"
    for d in (base, pkg_root.parent, outside, proj):
        d.mkdir(parents=True)
    pkg_root.symlink_to(outside)          # root itself points anywhere

    # Accepted — and it SHOULD be, because the root is launcher-supplied.
    binds = _jobs.child_core_binds(proj, ProjectPaths(base=base,
                                                      packages_root=pkg_root),
                                   state_root)
    src = next(b.source for b in binds if b.target == "/packages")
    assert Path(src).resolve() == outside.resolve(), (
        "if this ever starts refusing, the guard changed meaning and the "
        "comment in composition.py about trusted host-side config is stale")


def test_compose_containment_derives_each_root_from_its_component():
    """The session path, not just the child-job path.

    They are two separate guards, and 'one of the pair was updated' is this
    project's most-repeated defect — d734529 was exactly that. If composition's
    table still pins a relocatable component to the state root, a session with
    that component relocated is refused at launch with [mount-source-denied],
    which is invisible until someone sets the template on a real cluster.

    Parsed with AST rather than matched as text: a substring is satisfied by a
    docstring, and this must fail if the TABLE changes, not if a comment does.
    """
    import ast
    import inspect

    from botainer.core import composition as _c

    tree = ast.parse(inspect.getsource(_c))
    table = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_containment"
                        for t in node.targets)
                and isinstance(node.value, ast.List)):
            table = node.value
            break
    assert table is not None, "composition._containment table not found"

    found = {}
    for elt in table.elts:
        assert isinstance(elt, ast.Tuple) and len(elt.elts) == 3
        label = elt.elts[0]
        rootexpr = elt.elts[2]
        assert isinstance(label, ast.Constant)
        # third item must be a call deriving the root from THIS component
        attrs = [n.attr for n in ast.walk(rootexpr)
                 if isinstance(n, ast.Attribute)]
        found[label.value] = attrs

    for component in ("packages", "home", "scratch"):
        assert component in found, f"{component} missing from the table"
        assert f"{component}_root" in found[component], (
            f"the containment row for {component!r} does not derive its root "
            f"from proj_paths.{component}_root (saw {found[component]}). A "
            f"relocated /{component} will be refused at session launch.")


# ── template resolution, the half that silently did nothing last time ──


def test_template_names_the_component_it_failed_for(tmp_path, monkeypatch, capsys):
    """Six shipped profiles had an unresolvable scratch template and fell back
    in silence. The warning that fixed it said 'scratch' — with three
    relocatable components it must say WHICH one, or it points at the wrong
    line of the user's cluster.yaml.
    """
    from botainer.state import dir as _d

    _d._SCRATCH_FALLBACK_WARNED.clear()
    _d._warn_component_fallback("packages", "/p/${NOPE}", "/p/${NOPE}",
                                "unresolved variable")
    err = capsys.readouterr().err
    assert "packages template not usable" in err, err
    assert "packages.template" in err, "did not name the field to edit"
    assert "INODE" in err, (
        "the packages warning must say WHICH quota it costs — that is the "
        "whole distinction that was missed")


def test_each_component_warns_separately(capsys):
    """Dedup is per template string; with three components one key must not
    swallow another's warning."""
    from botainer.state import dir as _d

    _d._SCRATCH_FALLBACK_WARNED.clear()
    _d._warn_component_fallback("scratch", "/same", "/same", "why")
    _d._warn_component_fallback("packages", "/same", "/same", "why")
    err = capsys.readouterr().err
    assert "scratch template not usable" in err
    assert "packages template not usable" in err, (
        "the packages warning was deduped away by the scratch one — same "
        "template string, different component")


# ── the migration's most likely misstep ──────────────────────────────────────


def test_a_state_root_outside_home_refuses_with_the_remedy(monkeypatch):
    """MY_BOTAINER on a big volume is the FIRST thing anyone tries when $HOME
    fills up, and it is the state the storage migration passes through if the
    launcher wrapper is not updated in the same breath as the code sync.

    It used to raise a bare ValueError — nine frames of click internals, from
    `botainer doctor` among others, i.e. from the command you would run to
    find out why. Same class as the disk-quota traceback (§4cv): a condition
    with a specific remedy, delivered as a crash.

    The remedy is NOT "pick another root". It is "move the bulky components
    and leave the credentials on private storage", so the message must say
    that or the user just tries a different volume.
    """
    from botainer.core.refusal import RefusalCategory, Refused
    from botainer.state import dir as _d

    monkeypatch.setenv("MY_BOTAINER", "/opt/definitely-not-home/.botainer")

    with pytest.raises(Refused) as ei:
        _d._resolve_state_root()

    assert ei.value.category == RefusalCategory.STATE_ROOT_NOT_ALLOWED
    msg = str(ei.value)
    for needed in ("packages:", "relocate-storage.sh", "shared-auth"):
        assert needed in msg, f"refusal does not mention {needed!r}: {msg}"


def test_doctor_reports_a_refusal_instead_of_crashing():
    """`doctor` was the ONLY command in the CLI without @handle_refusals, so
    any Refused raised while collecting findings became a traceback — in the
    one command whose entire job is explaining a broken install."""
    import inspect

    from botainer.cli import doctor as doctor_cli

    src = inspect.getsource(doctor_cli)
    # The decorator is applied to the command; check the object, not the text.
    assert getattr(doctor_cli.doctor, "callback", None) is not None
    wrapped = doctor_cli.doctor.callback
    assert getattr(wrapped, "__wrapped__", None) is not None, (
        "botainer doctor is not wrapped by @handle_refusals — a Refused "
        "raised during findings collection will print a traceback")
