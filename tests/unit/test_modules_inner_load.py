"""caps.modules_inner_load — in-container `module load`.

The alternate hpc-modules mode: bind the Lmod install tree +
MODULEPATH dirs into the container RO and inject env so the AGENT
can run `module load foo` dynamically inside the container (no host
`module load` needed at submit time; batch-job friendly).

Trust anchor: root-owned SitePolicy `mounts.cluster_lmod_root` +
`mounts.cluster_modulepath_roots`. Same SITE-ONLY discipline as
cluster_software_roots (#160) — the user-writable cluster.yaml / user
policy cannot widen the ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition
from botainer.core.policy import MountsPolicy, SitePolicy
from botainer.core.spec import HookSpec, SessionSpec


class _FakeInstalledPlugin:
    def __init__(self, name: str, plugin_dir: Path) -> None:
        self.name = name
        self.plugin_dir = plugin_dir


def _make_spec_with_plugin(
    tmp_path: Path, *, plugin_dir: Path, plugin_name: str = "hpc-modules"
) -> SessionSpec:
    state = tmp_path / "state"
    (state / "sessions" / "s1").mkdir(parents=True)
    return SessionSpec(
        session_id="s1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state),
        runtime="docker",
        image="img",
        plugins_enabled=(plugin_name,),
    )


def _write_manifest_with_cap(plugin_dir: Path, *, cap: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "botainer-plugin.yaml").write_text(
        f"""apiVersion: botainer-plugin-v1
name: hpc-modules
version: 0.1.0
description: test
license: MIT
maintainer: t
botainer_min_version: 0.1.0a0
tier: first-party
kind: policy-overlay
trust_required: hooked
capabilities:
  - {cap}
"""
    )


def test_compute_inner_load_off_when_ceiling_empty(tmp_path: Path) -> None:
    """The critical safe default: no cluster_lmod_root in SitePolicy → no
    binds, no env. The feature is OFF by construction."""
    plugin_dir = tmp_path / "plugins" / "hpc-modules"
    _write_manifest_with_cap(plugin_dir, cap="caps.modules_inner_load")
    spec = _make_spec_with_plugin(tmp_path, plugin_dir=plugin_dir)
    policy = SitePolicy(mounts=MountsPolicy())  # empty
    inst_by_name = {"hpc-modules": _FakeInstalledPlugin("hpc-modules", plugin_dir)}
    binds, env = composition._compute_inner_load_contribution(
        spec=spec, effective_policy=policy, inst_by_name=inst_by_name,
    )
    assert binds == []
    assert env == {}


def test_compute_inner_load_off_when_no_declaring_plugin(tmp_path: Path) -> None:
    """SitePolicy grants the ceiling but no enabled plugin declares the cap
    → OFF. The cap-plugin match is the second necessary condition."""
    plugin_dir = tmp_path / "plugins" / "some-other"
    # Note: this plugin declares a DIFFERENT cap; caps.modules_inner_load is
    # not in its manifest.
    _write_manifest_with_cap(plugin_dir, cap="caps.modules_env_override")
    spec = _make_spec_with_plugin(
        tmp_path, plugin_dir=plugin_dir, plugin_name="some-other",
    )
    policy = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/apps/lmod",
        cluster_modulepath_roots=["/apps/modulefiles"],
    ))
    inst_by_name = {"some-other": _FakeInstalledPlugin("some-other", plugin_dir)}
    binds, env = composition._compute_inner_load_contribution(
        spec=spec, effective_policy=policy, inst_by_name=inst_by_name,
    )
    assert binds == []
    assert env == {}


def test_compute_inner_load_binds_and_env_when_granted(tmp_path: Path) -> None:
    """Both conditions met → RO identity binds for Lmod + each MODULEPATH,
    plus LMOD_PKG/LMOD_DIR/LMOD_CMD/MODULEPATH/BASH_ENV env vars."""
    plugin_dir = tmp_path / "plugins" / "hpc-modules"
    _write_manifest_with_cap(plugin_dir, cap="caps.modules_inner_load")
    spec = _make_spec_with_plugin(tmp_path, plugin_dir=plugin_dir)
    policy = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/apps/lmod",
        cluster_modulepath_roots=["/apps/modulefiles", "/apps/site-modulefiles"],
    ))
    inst_by_name = {"hpc-modules": _FakeInstalledPlugin("hpc-modules", plugin_dir)}
    binds, env = composition._compute_inner_load_contribution(
        spec=spec, effective_policy=policy, inst_by_name=inst_by_name,
    )
    # Bind list: Lmod tree + 2 MODULEPATH roots, RO identity binds.
    bind_paths = {(b.source, b.target, b.mode.value) for b in binds}
    assert ("/apps/lmod", "/apps/lmod", "ro") in bind_paths
    assert ("/apps/modulefiles", "/apps/modulefiles", "ro") in bind_paths
    assert ("/apps/site-modulefiles", "/apps/site-modulefiles", "ro") in bind_paths
    # Env: the five keys.
    assert env == {
        "LMOD_PKG": "/apps/lmod",
        "LMOD_DIR": "/apps/lmod/libexec",
        "LMOD_CMD": "/apps/lmod/libexec/lmod",
        "MODULEPATH": "/apps/modulefiles:/apps/site-modulefiles",
        "BASH_ENV": "/apps/lmod/init/bash",
    }


def test_compute_inner_load_dedupes_modulepath_matching_lmod_root(
    tmp_path: Path,
) -> None:
    """If a MODULEPATH root duplicates the Lmod root (unusual but possible
    for admins who point MODULEPATH at the Lmod tree itself), the bind is
    NOT emitted twice — otherwise validate_mount_plan would raise a
    duplicate-target error."""
    plugin_dir = tmp_path / "plugins" / "hpc-modules"
    _write_manifest_with_cap(plugin_dir, cap="caps.modules_inner_load")
    spec = _make_spec_with_plugin(tmp_path, plugin_dir=plugin_dir)
    policy = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/apps/lmod",
        cluster_modulepath_roots=["/apps/lmod", "/apps/modulefiles"],
    ))
    inst_by_name = {"hpc-modules": _FakeInstalledPlugin("hpc-modules", plugin_dir)}
    binds, env = composition._compute_inner_load_contribution(
        spec=spec, effective_policy=policy, inst_by_name=inst_by_name,
    )
    bind_targets = [b.target for b in binds]
    assert bind_targets.count("/apps/lmod") == 1


def test_policy_intersect_takes_site_authoritative_inner_load_ceiling(
    tmp_path: Path,
) -> None:
    """Same SITE-ONLY discipline as #160's cluster_software_roots — the
    user policy can NOT widen (or narrow via set-intersect zeroing) the
    inner-load ceiling. Only the root-owned /etc/botainer/policy.yaml
    value flows through intersect."""
    from botainer.core import policy as policy_module
    site = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/apps/lmod",
        cluster_modulepath_roots=["/apps/modulefiles"],
    ))
    user = SitePolicy(mounts=MountsPolicy(
        # User tries to widen with a different root.
        cluster_lmod_root="/home/attacker/fake-lmod",
        cluster_modulepath_roots=["/home/attacker/evil-modulefiles"],
    ))
    effective = policy_module.intersect(site, user)
    # Site-only: user's value is ignored regardless of narrower/wider.
    assert effective.mounts.cluster_lmod_root == "/apps/lmod"
    assert effective.mounts.cluster_modulepath_roots == ["/apps/modulefiles"]


def test_policy_intersect_user_empty_preserves_site_inner_load_ceiling(
    tmp_path: Path,
) -> None:
    """The 'user has no policy → default empty' case must NOT zero the
    site's ceiling (would silently turn the feature OFF whenever a user
    doesn't have a personal user policy — the common case). Matches the
    #160 discipline recorded in DESIGN-160-module-binds.md."""
    from botainer.core import policy as policy_module
    site = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/apps/lmod",
        cluster_modulepath_roots=["/apps/modulefiles"],
    ))
    user = SitePolicy(mounts=MountsPolicy())  # empty defaults
    effective = policy_module.intersect(site, user)
    assert effective.mounts.cluster_lmod_root == "/apps/lmod"
    assert effective.mounts.cluster_modulepath_roots == ["/apps/modulefiles"]


def test_render_human_names_inner_load_fields() -> None:
    """`policy show` output surfaces cluster_lmod_root + modulepath roots
    with SITE-ONLY markers so users can see the ceiling."""
    from botainer.core import policy as policy_module
    p = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/apps/lmod",
        cluster_modulepath_roots=["/apps/modulefiles"],
    ))
    rendered = policy_module.render_human(p)
    assert "cluster_lmod_root" in rendered
    assert "cluster_modulepath_roots" in rendered
    assert "SITE-ONLY" in rendered  # both fields must carry the marker


def test_capability_registry_declares_inner_load() -> None:
    """caps.modules_inner_load is in the closed namespace and
    requires_hooked_plugin (only a hook-declaring plugin can be granted)."""
    from botainer.capabilities.registry import get_capability
    cap = get_capability("caps.modules_inner_load")
    assert cap is not None
    assert cap.requires_hooked_plugin is True
    assert cap.site_policy_field == "mounts.cluster_lmod_root"


def test_hpc_modules_manifest_declares_inner_load_cap() -> None:
    """The bundled hpc-modules plugin declares the new cap (so a site that
    grants the ceiling actually gets the feature — the cap-declaration
    match is the second necessary condition)."""
    from botainer.plugins.manifest import load_manifest
    repo = Path(__file__).resolve().parents[2]
    man = load_manifest(repo / "plugins" / "hpc-modules")
    assert "caps.modules_inner_load" in man.capabilities


# ─────────── Flow 2 (sbatch): plan fields + outer argv (HPC parity) ───────────


def _load_hpc_common():
    import importlib.util
    import sys as _sys
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_innerload",
        repo / "plugins" / "hpc-launcher" / "host_helper" / "_common.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["hpc_launcher_common_innerload"] = mod
    spec.loader.exec_module(mod)
    return mod


def _minimal_plan(common, tmp_path: Path, **overrides):
    state_root = tmp_path / "state-root"
    state_root.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        project_root=str(tmp_path / "proj"),
        project_uuid="12345678-1234-5234-9234-123456789012",
        state_root=state_root,
        apptainer_image="/abs/botainer-agent-claude.sif",
        agent_name="claude",
        profile="default",
        partition="",
        account="",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        submission_mode="submit",
        existing_jobid=None,
    )
    kwargs.update(overrides)
    return common.SubmissionPlan(**kwargs)


# NOTE (compose-at-submit, task #52): test_flow2_outer_argv_carries_lmod_binds_
# and_env was removed. to_apptainer_argv no longer BUILDS the Lmod/MODULEPATH
# binds + LMOD_*/BASH_ENV env; caps.modules_inner_load now flows through Flow-1
# (composition._compute_inner_load_contribution → spec.mount_plan + spec.env →
# ApptainerAdapter), the SAME path the direct apptainer flow uses. Flow-1's
# inner-load contribution is covered by the composition tests in this file
# (test_inner_load_*). The frozen-plan lmod_root/modulepath validation is still
# pinned by test_flow2_plan_rejects_hostile_lmod_root below.


def test_flow2_outer_argv_no_lmod_when_off(tmp_path: Path) -> None:
    """Empty lmod_root (feature OFF) → no Lmod binds, no LMOD_*/BASH_ENV env
    on the outer argv (the safe default is genuinely absent, not just empty
    strings)."""
    common = _load_hpc_common()
    plan = _minimal_plan(common, tmp_path)
    argv = plan.to_apptainer_argv()
    joined = " ".join(argv)
    assert "LMOD_PKG" not in joined
    assert "BASH_ENV" not in joined
    assert "MODULEPATH" not in joined


# NOTE (compose-at-submit, task #52): test_flow2_dedupes_binds_against_software_
# roots was removed. Bind dedup now happens in
# composition._compute_inner_load_contribution (which dedups the Lmod/MODULEPATH
# binds against the spec's existing mount-plan targets before the adapter renders
# them) — pinned by the composition inner-load tests. to_apptainer_argv no longer
# builds or dedups binds.


# ─── inner disclosure guard: permits deep /usr Lmod, refuses the never-legit ───


def test_flow1_inner_load_binds_pass_validate_mount_plan(tmp_path: Path) -> None:
    """Flow-1 regression (the wrong-provenance backstop refusal): the
    Lmod/MODULEPATH binds must use Provenance.PLUGIN so validate_mount_plan
    (run right after _compute_inner_load_contribution in
    run_host_pre_launch_hooks) accepts them. A SITE_POLICY provenance would
    be refused because /apps/lmod is not in extra_targets_allowlist — which
    would crash Flow 1 entirely. This exercises the real backstop, not just
    the bind shape."""
    from botainer.core.spec import Provenance
    from botainer.mount_plan.validation import validate as validate_mount_plan
    from botainer.core.policy import SitePolicy as _SP, MountsPolicy as _MP

    plugin_dir = tmp_path / "plugins" / "hpc-modules"
    _write_manifest_with_cap(plugin_dir, cap="caps.modules_inner_load")
    spec = _make_spec_with_plugin(tmp_path, plugin_dir=plugin_dir)
    policy = _SP(mounts=_MP(
        cluster_lmod_root="/usr/share/lmod/lmod",  # the /usr-based RPM default
        cluster_modulepath_roots=["/usr/share/modulefiles"],
    ))
    inst_by_name = {"hpc-modules": _FakeInstalledPlugin("hpc-modules", plugin_dir)}
    binds, _env = composition._compute_inner_load_contribution(
        spec=spec, effective_policy=policy, inst_by_name=inst_by_name,
    )
    assert binds, "feature should be ON"
    assert all(b.provenance == Provenance.PLUGIN for b in binds)
    # Build a mount plan and run the ACTUAL backstop — must not raise even
    # though /usr/share/... is not in the default extra_targets_allowlist.
    plan = spec.mount_plan.with_many(binds)
    validate_mount_plan(plan, policy=policy)  # no raise = regression fixed


# ─── shared source guard: 3-way parity (Flow 1 / Flow 2 / inner disclosure) ───


def test_shared_source_guard_classification() -> None:
    """The single guard both flows + disclosure use. Refuses /etc-class,
    sensitive-home, shallow-system; permits deep /usr Lmod trees."""
    from botainer.hpc.module_binds import is_unsafe_module_tree_source as g
    # Permit deep admin-named trees (the whole point).
    assert g("/usr/share/lmod/lmod")[0] is False
    assert g("/apps/lmod/lmod")[0] is False
    assert g("/opt/apps/lmod/lmod")[0] is False
    # Refuse /etc-class (admin typo defense-in-depth; parity with Flow 1's
    # validate_mount_plan DENYLISTED_SOURCES).
    assert g("/etc/ssh")[0] is True
    assert g("/proc/self")[0] is True
    # Refuse shallow system mount points (identity bind would shadow base image).
    assert g("/usr")[0] is True
    assert g("/lib")[0] is True
    assert g("/")[0] is True


def test_flow1_composition_refuses_etc_lmod_root(tmp_path: Path) -> None:
    """Flow 1: a /etc-class cluster_lmod_root is refused fail-closed at
    _compute_inner_load_contribution (before validate_mount_plan)."""
    from botainer.core.refusal import Refused
    plugin_dir = tmp_path / "plugins" / "hpc-modules"
    _write_manifest_with_cap(plugin_dir, cap="caps.modules_inner_load")
    spec = _make_spec_with_plugin(tmp_path, plugin_dir=plugin_dir)
    policy = SitePolicy(mounts=MountsPolicy(
        cluster_lmod_root="/etc/ssh",
        cluster_modulepath_roots=["/etc/modulefiles"],
    ))
    inst_by_name = {"hpc-modules": _FakeInstalledPlugin("hpc-modules", plugin_dir)}
    with pytest.raises(Refused):
        composition._compute_inner_load_contribution(
            spec=spec, effective_policy=policy, inst_by_name=inst_by_name,
        )


