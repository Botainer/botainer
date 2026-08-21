"""Exhaustive security-property tests for the #160 software-root bind
derivation (botainer.hpc.module_binds.derive_software_root_binds).

This is the decision-independent CRITICAL core: it must NEVER produce an
umbrella bind, NEVER bind a container-owned system dir, NEVER bind a
pre-existing (non-module-added) PATH entry, and must fail LOUD rather than
silently truncate. The wiring (composition + hpc-launcher) lands in follow-up
commits; these tests pin the algorithm regardless of how it is called.
"""

from __future__ import annotations

import pytest

from botainer.hpc.module_binds import (
    SoftwareRootBindError,
    derive_software_root_binds,
    derive_software_root_binds_verbose,
)

PREFIXES = ["/apps", "/vast/software"]


def _derive(baseline, loaded, prefixes=PREFIXES, **kw):
    return derive_software_root_binds(baseline, loaded, prefixes, **kw)


def _targets(binds):
    return sorted(b["target"] for b in binds)


# ───────────────────────── feature-OFF / fail-closed ─────────────────────


def test_empty_policy_prefixes_yields_no_binds() -> None:
    """The only safe default: no policy ceiling → feature OFF, even when
    module load added software dirs."""
    loaded = {"PATH": "/apps/python/3.11/bin"}
    assert _derive({}, loaded, prefixes=[]) == []
    assert _derive({}, loaded, prefixes=None) == []


def test_no_modules_added_yields_no_binds() -> None:
    """Baseline == loaded → nothing added → no binds."""
    env = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/apps/x/lib"}
    assert _derive(env, env) == []


# ───────────────────────── baseline-diff ─────────────────────────────────


def test_only_module_added_dirs_are_bound() -> None:
    """A pre-existing PATH entry (in baseline) is NEVER a candidate — only the
    dir the module load ADDED is bound."""
    baseline = {"PATH": "/apps/preexisting/bin:/usr/bin"}
    loaded = {"PATH": "/apps/python/3.11/bin:/apps/preexisting/bin:/usr/bin"}
    assert _targets(_derive(baseline, loaded)) == ["/apps/python/3.11/bin"]


def test_poisoned_preexisting_path_never_bound() -> None:
    """A poisoned pre-existing entry within the policy prefix is in the
    baseline, so it is excluded even though it would otherwise be acceptable."""
    baseline = {"PATH": "/apps/evil"}
    loaded = {"PATH": "/apps/evil"}
    assert _derive(baseline, loaded) == []


def test_added_across_multiple_path_vars() -> None:
    """bin (PATH), lib (LD_LIBRARY_PATH), include (CPATH) each arrive via their
    own var diff — the common case is covered without any coalescing."""
    baseline = {"PATH": "/usr/bin"}
    loaded = {
        "PATH": "/apps/gcc/13/bin:/usr/bin",
        "LD_LIBRARY_PATH": "/apps/gcc/13/lib64",
        "CPATH": "/apps/gcc/13/include",
    }
    assert _targets(_derive(baseline, loaded)) == [
        "/apps/gcc/13/bin",
        "/apps/gcc/13/include",
        "/apps/gcc/13/lib64",
    ]


def test_single_dir_vars_like_cuda_home() -> None:
    baseline: dict[str, str] = {}
    loaded = {"CUDA_HOME": "/apps/cuda/12.3", "JAVA_HOME": "/apps/jdk/21"}
    assert _targets(_derive(baseline, loaded)) == [
        "/apps/cuda/12.3",
        "/apps/jdk/21",
    ]


# ───────────────────── policy-prefix containment ─────────────────────────


def test_dir_outside_policy_prefix_dropped() -> None:
    """An added dir not under any policy prefix is dropped (not bound)."""
    loaded = {"PATH": "/opt/random/bin:/apps/ok/bin"}
    assert _targets(_derive({}, loaded)) == ["/apps/ok/bin"]


def test_home_dir_addition_dropped() -> None:
    """A module that adds a $HOME path is not within the cluster software
    ceiling → dropped."""
    loaded = {"PATH": "/home/victim/.local/bin"}
    assert _derive({}, loaded) == []


# ───────────────── never-umbrella / system-dir / min-depth ───────────────


@pytest.mark.parametrize(
    "sysdir",
    ["/usr/lib", "/usr/bin", "/lib64", "/etc/foo", "/bin", "/var/x", "/usr/local/lib"],
)
def test_container_owned_system_dirs_never_bound(sysdir: str) -> None:
    """Even if a (misconfigured) policy prefix would admit it, an identity bind
    over a container-owned FHS dir is refused — it would shadow the base image.
    Use prefix='/' to prove the system-root backstop, not the prefix, blocks it."""
    loaded = {"LD_LIBRARY_PATH": sysdir}
    assert _derive({}, loaded, prefixes=["/"]) == []


@pytest.mark.parametrize("sub", [".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"])
def test_sensitive_home_dirs_never_bound(sub: str, monkeypatch, tmp_path) -> None:
    """#160 S1 (adversarial-review): even if a foolish ceiling admits $HOME, a
    module-added path under a sensitive per-user dir (~/.ssh, ~/.aws, …) is
    NEVER bound. Closed at the shared derivation chokepoint so BOTH flows are
    covered. Matched against the realpath (symlink into ~/.ssh caught too)."""
    home = tmp_path / "home"
    (home / sub).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    loaded = {"PATH": f"{home}/{sub}/bin", "LD_LIBRARY_PATH": f"{home}/{sub}"}
    # Ceiling = HOME itself (maximally foolish admin) — still refused.
    assert _derive({}, loaded, prefixes=[str(home)]) == []


def test_drift_home_sensitive_matches_validation() -> None:
    """The derivation's home-sensitive denylist must stay in sync with the
    canonical mount-plan validator's SENSITIVE_USER_HOME_SUBPATHS (no
    standalone-mirror drift across the two enforcement points)."""
    from botainer.hpc.module_binds import _HOME_SENSITIVE_SUBDIRS
    from botainer.mount_plan.validation import SENSITIVE_USER_HOME_SUBPATHS
    assert set(_HOME_SENSITIVE_SUBDIRS) == set(SENSITIVE_USER_HOME_SUBPATHS)


def test_misconfigured_root_prefix_cannot_collapse_to_umbrella() -> None:
    """Admin foolishly sets prefix='/'. The system-root denylist + min-depth
    still prevent binding any container-owned dir; only genuine deep non-system
    software dirs the module added survive — never '/' itself, never /usr."""
    loaded = {
        "PATH": "/apps/x/bin",       # ok: deep, non-system
        "LD_LIBRARY_PATH": "/usr/lib:/lib",  # refused: system
    }
    assert _targets(_derive({}, loaded, prefixes=["/"])) == ["/apps/x/bin"]


@pytest.mark.parametrize("shallow", ["/apps", "/vast"])
def test_min_depth_floor_rejects_shallow_roots(shallow: str) -> None:
    """A bind root must clear the min-depth floor (default 2): the bare policy
    prefix itself can never become the bind (no umbrella)."""
    loaded = {"PATH": shallow}
    assert _derive({}, loaded, prefixes=[shallow]) == []


def test_min_depth_is_tunable() -> None:
    loaded = {"PATH": "/apps/x"}  # depth 2
    assert _targets(_derive({}, loaded, min_depth=2)) == ["/apps/x"]
    assert _derive({}, loaded, min_depth=3) == []  # now too shallow


# ───────────────────────── max-roots: fail LOUD ──────────────────────────


def test_exceeding_max_roots_raises_not_truncates() -> None:
    """A dropped root would be an unreachable binary with NO signal. Refuse."""
    loaded = {"PATH": ":".join(f"/apps/p{i}/bin" for i in range(10))}
    with pytest.raises(SoftwareRootBindError):
        _derive({}, loaded, max_roots=8)


def test_within_max_roots_ok() -> None:
    loaded = {"PATH": ":".join(f"/apps/p{i}/bin" for i in range(5))}
    assert len(_derive({}, loaded, max_roots=8)) == 5


# ───────────────────────── shape / mode invariants ───────────────────────


def test_binds_are_readonly_identity() -> None:
    loaded = {"PATH": "/apps/x/bin"}
    binds = _derive({}, loaded)
    assert binds == [{"source": "/apps/x/bin", "target": "/apps/x/bin", "mode": "ro"}]


def test_duplicate_dirs_deduped() -> None:
    """Same dir on PATH and LD_LIBRARY_PATH → one bind."""
    loaded = {"PATH": "/apps/x/lib", "LD_LIBRARY_PATH": "/apps/x/lib"}
    assert _targets(_derive({}, loaded)) == ["/apps/x/lib"]


def test_excluded_vars_never_contribute_binds() -> None:
    """PYTHONPATH / R_LIBS_USER / etc. are deliberately NOT diffed — a
    modulefile must not be able to smuggle a bind through package-routing
    vars (or redirect package routing)."""
    loaded = {
        "PYTHONPATH": "/apps/sneaky/site-packages",
        "PYTHONHOME": "/apps/sneaky/py",
        "R_LIBS_USER": "/apps/sneaky/rlibs",
        "JULIA_DEPOT_PATH": "/apps/sneaky/julia",
        "NODE_PATH": "/apps/sneaky/node",
    }
    assert _derive({}, loaded) == []


def test_relative_entries_ignored(monkeypatch, tmp_path) -> None:
    """A relative PATH entry is not bound — and (re-audit SEC-6) is rejected AT
    THE SOURCE (before realpath), so it can't be realpath'd against the
    launcher's CWD into a path that happens to sit inside the ceiling. Even with
    the ceiling set to the CWD, a relative entry never binds; only the absolute
    in-ceiling entry survives, and the relative entries aren't even candidates."""
    # Make CWD sit UNDER the ceiling — the exact case the old dead guard missed.
    (tmp_path / "ceil").mkdir()
    monkeypatch.chdir(tmp_path / "ceil")
    loaded = {"PATH": "relative/bin:./x:/apps/ok/bin"}
    ceil = str(tmp_path / "ceil")
    v = derive_software_root_binds_verbose({}, loaded, ["/apps", ceil])
    assert _targets(v.binds) == ["/apps/ok/bin"]   # only the absolute in-ceiling dir
    # Re-audit: assert the STRUCTURAL property, not just substrings
    # (the old './x' substring check could pass vacuously). Every candidate that
    # survived to `added` is an ABSOLUTE in-ceiling /apps dir; nothing resolved
    # against the CWD-under-ceiling (`ceil`) leaked in — proving the source-filter
    # ran before realpath, not that the strings merely happened to differ.
    assert all(a.startswith("/apps/") for a in v.added)
    assert all(a.startswith("/") for a in v.added)          # never a relative entry
    assert ceil not in v.added and not any(a.startswith(ceil) for a in v.added)


def test_symlink_farm_realpath(tmp_path, monkeypatch) -> None:
    """Symlink-farm hardening (DESIGN guardrail 10, adversarial-review T2 #11):
    on a cluster where /apps is a symlink to /vast, a candidate is realpath'd,
    bound at its REALPATH, and the ceiling matches against BOTH the literal
    prefix and the realpath. Previously zero coverage."""
    vast = tmp_path / "vast"
    (vast / "python" / "3.11" / "bin").mkdir(parents=True)
    farm = tmp_path / "apps"
    farm.symlink_to(vast)  # /apps -> /vast
    loaded = {"PATH": f"{farm}/python/3.11/bin"}  # module sets the symlink path

    # (a) ceiling lists the REALPATH target → matched via realpath; bound at realpath.
    v1 = derive_software_root_binds_verbose({}, loaded, [str(vast)])
    assert _targets(v1.binds) == [str((vast / "python/3.11/bin").resolve())]
    assert not v1.is_off

    # (b) ceiling lists the SYMLINK prefix (/apps) → matched via the literal
    # prefix; still bound at the realpath (the actual content).
    v2 = derive_software_root_binds_verbose({}, loaded, [str(farm)])
    assert _targets(v2.binds) == [str((vast / "python/3.11/bin").resolve())]
