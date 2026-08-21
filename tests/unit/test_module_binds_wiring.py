"""#160 WIRING tests — the software-root bind derivation hooked into the two
real launch flows + the policy field, complementing the pure-core tests in
test_module_binds.py.

Covered:
- Flow 1 (composition.run_host_pre_launch_hooks): a host_pre_launch hook's
  `software_root_env` contribution is derived into RO-identity binds, gated by
  the plugin's caps.modules_software_roots declaration + the root-owned
  SitePolicy ceiling. Fail-closed when the cap is missing / plugin
  un-introspectable; OFF when the ceiling is empty; never an umbrella.
- Flow 2 (hpc-launcher host_helper): to_apptainer_argv emits the OUTER
  --bind=<r>:<r>:ro; the frozen-plan validator rejects injection; the derive
  helper is gated (disabled plugin / empty ceiling → no binds).
- Policy: MountsPolicy.cluster_software_roots is SitePolicy-AUTHORITATIVE in
  intersect() (a default-empty user policy can't zero it; a user policy can't
  widen it).
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import types
from pathlib import Path

import pytest

from botainer.core import composition
from botainer.core.policy import MountsPolicy, SitePolicy, intersect
from botainer.core.refusal import Refused
from botainer.core.spec import (
    AgentRendering,
    Bind,
    BindMode,
    HookSpec,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    SessionSpec,
)
from botainer.state import session_record as sr

REPO = Path(__file__).resolve().parents[2]
HPC_MODULES_DIR = REPO / "plugins" / "hpc-modules"
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


# ───────────────────────────── helpers ──────────────────────────────


def _make_executable(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _sw_env_hook(tmp_path: Path, baseline: dict, loaded: dict) -> Path:
    """A fake host_pre_launch hook that emits a software_root_env contribution."""
    hook = tmp_path / "swhook.py"
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({{'version':'plugin-contribution-v1',"
        f"'kind':'host_pre_launch',"
        f"'software_root_env':{{'baseline':{baseline!r},'loaded':{loaded!r}}}}}))\n"
    )
    _make_executable(hook)
    return hook


def _spec_with_hook(tmp_path: Path, hook: Path) -> SessionSpec:
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "swsess"
    session_dir.mkdir(parents=True, exist_ok=True)
    workspace_bind = Bind(
        source=str(tmp_path / "ws"),
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    spec = SessionSpec(
        session_id="swsess",
        project_uuid="u",
        project_root=str(tmp_path / "ws"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img@sha256:" + "0" * 64,
        mount_plan=MountPlan(binds=(workspace_bind, Bind(
            source="/state/anchor", target="/workspace/.botainer",
            mode=BindMode.NULL_BIND, provenance=Provenance.CORE,
            nested_under="/workspace"))),  # RW /workspace requires the mask
        network=NetworkSpec(mode=NetworkMode.INTERNET),
        hooks=(
            HookSpec(plugin="hpc-modules", when="host_pre_launch", script_path=str(hook)),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    return spec


def _patch_policy(monkeypatch, *, ceiling: list[str]) -> None:
    """Force the effective SitePolicy ceiling composition reads."""
    site = SitePolicy(mounts=MountsPolicy(cluster_software_roots=list(ceiling)))
    user = SitePolicy()
    monkeypatch.setattr(composition.policy_module, "load_site_policy", lambda: site)
    monkeypatch.setattr(composition.policy_module, "load_user_policy", lambda: user)


def _patch_installed(monkeypatch, *, plugin_dir: Path, name: str = "hpc-modules") -> None:
    inst = types.SimpleNamespace(name=name, plugin_dir=plugin_dir)
    monkeypatch.setattr(composition, "list_installed", lambda: [inst])


def _sw_targets(spec: SessionSpec) -> list[str]:
    return sorted(
        b.target for b in spec.mount_plan.binds
        if b.self_test == "SELFTEST_MODULE_SOFTWARE_BIND"
    )


# ─────────────────── Flow 1: composition wiring ────────────────────


def test_composition_derives_software_root_binds(monkeypatch, tmp_path) -> None:
    """Happy path: hook emits baseline/loaded, real manifest declares the cap,
    ceiling lists /apps → composition adds RO-identity binds for the added
    dir."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)  # real manifest (cap declared)
    hook = _sw_env_hook(tmp_path, baseline={}, loaded={"PATH": "/apps/python/3.11/bin"})
    spec = _spec_with_hook(tmp_path, hook)

    out = composition.run_host_pre_launch_hooks(spec)
    assert _sw_targets(out) == ["/apps/python/3.11/bin"]
    b = next(x for x in out.mount_plan.binds if x.self_test == "SELFTEST_MODULE_SOFTWARE_BIND")
    assert b.mode == BindMode.RO
    assert b.source == b.target  # identity
    assert b.provenance == Provenance.PLUGIN


def test_composition_baseline_diff_excludes_preexisting(monkeypatch, tmp_path) -> None:
    """A dir already in the post-purge baseline (a poisoned pre-existing PATH)
    is NOT bound — only what `module load` ADDED."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = _sw_env_hook(
        tmp_path,
        baseline={"PATH": "/apps/evil/bin"},
        loaded={"PATH": "/apps/evil/bin:/apps/python/3.11/bin"},
    )
    spec = _spec_with_hook(tmp_path, hook)
    out = composition.run_host_pre_launch_hooks(spec)
    assert _sw_targets(out) == ["/apps/python/3.11/bin"]  # /apps/evil/bin excluded


def test_composition_empty_ceiling_off(monkeypatch, tmp_path) -> None:
    """Empty SitePolicy ceiling → feature OFF (no software-root binds)."""
    _patch_policy(monkeypatch, ceiling=[])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = _sw_env_hook(tmp_path, baseline={}, loaded={"PATH": "/apps/python/3.11/bin"})
    spec = _spec_with_hook(tmp_path, hook)
    out = composition.run_host_pre_launch_hooks(spec)
    assert _sw_targets(out) == []


def test_composition_outside_ceiling_dropped(monkeypatch, tmp_path) -> None:
    """A module-added dir outside the ceiling is dropped (not bound)."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = _sw_env_hook(
        tmp_path, baseline={}, loaded={"PATH": "/opt/rogue/bin:/apps/python/3.11/bin"}
    )
    spec = _spec_with_hook(tmp_path, hook)
    out = composition.run_host_pre_launch_hooks(spec)
    assert _sw_targets(out) == ["/apps/python/3.11/bin"]  # /opt/rogue/bin not in ceiling


def test_composition_never_binds_system_root(monkeypatch, tmp_path) -> None:
    """Even with a foolish ceiling that would admit /etc, derive's system-root
    denylist refuses it — no host /etc shadow."""
    _patch_policy(monkeypatch, ceiling=["/"])  # maximally foolish admin
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = _sw_env_hook(
        tmp_path, baseline={}, loaded={"PATH": "/etc:/apps/python/3.11/bin"}
    )
    spec = _spec_with_hook(tmp_path, hook)
    out = composition.run_host_pre_launch_hooks(spec)
    assert "/etc" not in _sw_targets(out)


def test_composition_never_binds_sensitive_home(monkeypatch, tmp_path) -> None:
    """#160 S1: even with a foolish ceiling=[$HOME], a module-added path under
    ~/.ssh is refused by the shared derivation — the composition flow inherits
    the central denylist (no per-flow drift)."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _patch_policy(monkeypatch, ceiling=[str(home)])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = _sw_env_hook(
        tmp_path, baseline={},
        loaded={"PATH": f"{home}/.ssh/bin:/somethingelse"},
    )
    spec = _spec_with_hook(tmp_path, hook)
    out = composition.run_host_pre_launch_hooks(spec)
    assert _sw_targets(out) == []


def test_composition_warns_when_silently_off(monkeypatch, tmp_path, capsys) -> None:
    """#160 (adversarial-review T1): when `module load` added software dirs but
    the ceiling excludes them all (silently-OFF), composition WARNS so the user
    isn't left thinking module software is reachable when it isn't."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = _sw_env_hook(tmp_path, baseline={}, loaded={"PATH": "/opt/rogue/bin"})
    spec = _spec_with_hook(tmp_path, hook)
    out = composition.run_host_pre_launch_hooks(spec)
    assert _sw_targets(out) == []  # nothing bound
    err = capsys.readouterr().err
    assert "UNREACHABLE" in err and "/opt/rogue/bin" in err
    assert "cluster_software_roots" in err  # actionable remediation


def test_composition_cap_not_declared_refused(monkeypatch, tmp_path) -> None:
    """Fail-closed: a plugin whose manifest does NOT declare
    caps.modules_software_roots cannot contribute software-root binds."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    # manifest without the cap
    monkeypatch.setattr(
        "botainer.plugins.manifest.load_manifest",
        lambda _d: types.SimpleNamespace(capabilities=["caps.modules_env_override"]),
    )
    hook = _sw_env_hook(tmp_path, baseline={}, loaded={"PATH": "/apps/python/3.11/bin"})
    spec = _spec_with_hook(tmp_path, hook)
    with pytest.raises(Refused, match="caps.modules_software_roots"):
        composition.run_host_pre_launch_hooks(spec)


def test_composition_plugin_not_installed_refused(monkeypatch, tmp_path) -> None:
    """Fail-closed: contributing plugin not in the installed list → refuse
    (cannot verify the grant)."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    monkeypatch.setattr(composition, "list_installed", lambda: [])  # none installed
    hook = _sw_env_hook(tmp_path, baseline={}, loaded={"PATH": "/apps/python/3.11/bin"})
    spec = _spec_with_hook(tmp_path, hook)
    with pytest.raises(Refused, match="not in the installed plugin list"):
        composition.run_host_pre_launch_hooks(spec)


def test_composition_malformed_contribution_refused(monkeypatch, tmp_path) -> None:
    """A non-dict baseline/loaded is malformed → refuse (don't crash derive)."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    hook = tmp_path / "badhook.py"
    hook.write_text(
        "#!/usr/bin/env python3\nimport json\n"
        "print(json.dumps({'version':'plugin-contribution-v1','kind':'host_pre_launch',"
        "'software_root_env':{'baseline':'NOTADICT','loaded':{}}}))\n"
    )
    _make_executable(hook)
    spec = _spec_with_hook(tmp_path, hook)
    with pytest.raises(Refused, match="must both be objects"):
        composition.run_host_pre_launch_hooks(spec)


def test_composition_too_many_roots_refused(monkeypatch, tmp_path) -> None:
    """Exceeding max_roots RAISES in derive → composition refuses loudly
    (never silent truncation)."""
    _patch_policy(monkeypatch, ceiling=["/apps"])
    _patch_installed(monkeypatch, plugin_dir=HPC_MODULES_DIR)
    many = ":".join(f"/apps/pkg{i}/bin" for i in range(20))  # > max_roots (8)
    hook = _sw_env_hook(tmp_path, baseline={}, loaded={"PATH": many})
    spec = _spec_with_hook(tmp_path, hook)
    with pytest.raises(Refused, match="exceeding max_roots|derivation refused"):
        composition.run_host_pre_launch_hooks(spec)


# ─────────────────── Flow 2: sbatch host_helper ────────────────────


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_wiring", HPC_HOST_HELPER / "_common.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_wiring"] = mod
    spec.loader.exec_module(mod)
    return mod


def _plan(common, tmp_path, **kw):
    defaults = dict(
        project_root=tmp_path,
        project_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        state_root=tmp_path / "state",
        profile="default",
        partition="day",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="/img.sif",
        submission_mode="submit",
        existing_jobid=None,
    )
    defaults.update(kw)
    return common.SubmissionPlan(**defaults)


# NOTE (compose-at-submit, task #52): test_sbatch_to_apptainer_argv_emits_
# software_root_binds was removed. to_apptainer_argv no longer BUILDS the
# software-root --bind lines from the plan; the #160 software-root binds now
# flow via Flow-1 (composition.run_host_pre_launch_hooks →
# derive_software_root_binds → spec.mount_plan → ApptainerAdapter) — the SAME
# path the direct apptainer flow uses. Flow-1 derivation is covered above
# (test_composition_derives_software_root_binds). The frozen-plan syntactic
# guard (_reject_unsafe_software_root) is still pinned below.


def _state_with_manifest(tmp_path):
    state_root = tmp_path / "state"
    plugin_dir = state_root / "plugins" / "hpc-modules"
    plugin_dir.mkdir(parents=True)
    import shutil
    shutil.copy(HPC_MODULES_DIR / "botainer-plugin.yaml", plugin_dir / "botainer-plugin.yaml")
    return state_root


# ─────────────── sbatch module-ENV propagation (PATH prepend) ──────


def test_module_env_file_path_clobber_default_refuses(
    tmp_path, capsys, monkeypatch
) -> None:
    """ACKNOWLEDGED-RISK #1 (Step A): default behavior on direct
    docker / direct-apptainer flow is to REFUSE when a host_pre_launch env_file
    contributes PATH-list vars (PATH, LD_LIBRARY_PATH, …). The previous warn-
    and-continue produced confusing `<agent>: not found` agent-launch failures
    because `--env-file` SETs path-list vars under `--cleanenv`, dropping the
    container's own /opt/conda/bin from PATH. Refusing at compose time surfaces
    the constraint when the user can do something about it.

    Sbatch flow is unaffected (it never reaches this code; the HPC compose path
    forces module_env_delivery="inner-prepend", so the env_file is SPLIT into a
    scalar-only --env-file + PATH-list PREPEND pairs — no clobber to refuse).
    """
    from botainer.core.composition import _handle_module_env_path_clobber
    from botainer.core.refusal import Refused, RefusalCategory

    # Make sure no env opt-out is set.
    monkeypatch.delenv("BOTAINER_ALLOW_PATH_CLOBBER", raising=False)
    monkeypatch.delenv("BOTAINER_USE_INNER_PREPEND", raising=False)

    # PATH-list var present → refuse on docker + apptainer.
    ef = tmp_path / "mod.env"
    ef.write_text("PATH=/apps/python/3.11/bin\nCUDA_HOME=/apps/cuda/12.3\n")
    for rt in ("docker", "apptainer"):
        with pytest.raises(Refused) as excinfo:
            _handle_module_env_path_clobber(ef, rt, "hpc-modules")
        assert excinfo.value.category == RefusalCategory.ENV_VAR_DENIED, rt
        msg = str(excinfo.value)
        # Refusal message is actionable: it must NAME the offending vars + the
        # three concrete next steps (sbatch / module unload / opt-in escape).
        assert "PATH" in msg
        assert "sbatch" in msg
        assert "module unload" in msg
        assert "BOTAINER_ALLOW_PATH_CLOBBER" in msg

    # Scalar-only env_file → no refusal (SET is correct for CUDA_HOME/JAVA_HOME).
    ef2 = tmp_path / "scalar.env"
    ef2.write_text("CUDA_HOME=/apps/cuda/12.3\nJAVA_HOME=/apps/java/21\n")
    _handle_module_env_path_clobber(ef2, "apptainer", "hpc-modules")  # no raise

    # Mock runtime (not a real adapter — e.g. tests' MockAdapter) → no refusal,
    # so test-suite compositions don't have to set BOTAINER_ALLOW_PATH_CLOBBER.
    _handle_module_env_path_clobber(ef, "mock", "hpc-modules")  # no raise


def test_module_env_file_path_clobber_opt_out_warns(
    tmp_path, capsys, monkeypatch
) -> None:
    """BOTAINER_ALLOW_PATH_CLOBBER=1 reproduces the previous warn-and-continue
    as an emergency escape valve. The warning names the offending vars + the
    runtime + tells the user the agent may fail to start."""
    from botainer.core.composition import _handle_module_env_path_clobber
    monkeypatch.setenv("BOTAINER_ALLOW_PATH_CLOBBER", "1")
    monkeypatch.delenv("BOTAINER_USE_INNER_PREPEND", raising=False)

    ef = tmp_path / "mod.env"
    ef.write_text("PATH=/apps/python/3.11/bin\nLD_LIBRARY_PATH=/apps/python/3.11/lib\n")
    _handle_module_env_path_clobber(ef, "apptainer", "hpc-modules")
    err = capsys.readouterr().err
    assert "BOTAINER_ALLOW_PATH_CLOBBER=1" in err
    assert "PATH" in err
    assert "LD_LIBRARY_PATH" in err
    assert "may fail" in err


# NOTE (compose-at-submit, task #52): test_drift_inner_path_list_vars_subset_of_
# module_binds was removed — start.py's `_MODULE_PATH_LIST_VARS` copy is deleted
# along with the whole in-container path. There is no longer a SECOND copy to
# drift from: the single source is botainer.hpc.module_binds.PATH_LIST_VARS,
# consumed directly by composition._split_module_env_for_inner_prepend and the
# adapter trampoline.


# NOTE (compose-at-submit, task #52): test_sbatch_to_apptainer_argv_emits_
# module_env_file and test_sbatch_rendered_script_carries_binds_and_env_file
# were removed. The captured module env is no longer delivered via an inner
# `--module-env-file` flag (there is no inner botainer); it flows through
# Flow-1's env_file → the adapter's `--env-file` (scalars) + the inner-prepend
# trampoline (PATH-list vars), rendered ON the sbatch script. The #160 binds
# likewise route via Flow-1 → ApptainerAdapter. The compose-level behavior is
# pinned by tests/unit/test_hpc_compose_at_submit.py; Flow-1 derivation above.


def test_render_agent_files_reflects_post_hook_binds(tmp_path) -> None:
    """Audit T10: render_agent_files writes the agent files from the POST-hook
    spec, so a hook-added bind (e.g. a #160 software-root bind) appears in
    AGENT_ACCESS.txt — the agent is told what it actually has, not the
    understated compose-time view."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "swsess"
    session_dir.mkdir(parents=True)
    binds = (
        Bind(source=str(tmp_path / "ws"), target="/workspace", mode=BindMode.RW,
             provenance=Provenance.CORE, provenance_detail="ws",
             agent_rendering=AgentRendering.SHOWN, self_test="SELFTEST_WORKSPACE_RW"),
        Bind(source="/apps/python/3.11/bin", target="/apps/python/3.11/bin",
             mode=BindMode.RO, provenance=Provenance.PLUGIN,
             provenance_detail="hpc-modules software-root (#160)",
             agent_rendering=AgentRendering.SHOWN,
             self_test="SELFTEST_MODULE_SOFTWARE_BIND"),
        Bind(source="/state/anchor", target="/workspace/.botainer",
             mode=BindMode.NULL_BIND, provenance=Provenance.CORE,
             nested_under="/workspace"),  # RW /workspace requires the mask
    )
    spec = SessionSpec(
        session_id="swsess", project_uuid="u", project_root=str(tmp_path / "ws"),
        state_dir=str(state_dir), runtime="apptainer", image="/img.sif",
        mount_plan=MountPlan(binds=binds),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
    )
    composition.render_agent_files(spec)
    aas = (session_dir / "AGENT_ACCESS.txt").read_text()
    assert "/apps/python/3.11/bin" in aas  # the post-hook #160 bind is disclosed


# ─────────────── B1: --in-container skips host_pre_launch ──────────


def test_in_container_refuses_unenforceable_network(monkeypatch, tmp_path) -> None:
    """Audit T4: the sbatch --in-container path must refuse network modes
    apptainer can't enforce (none / endpoint-ip-allowlist) — matching the direct
    apptainer adapter — instead of silently running on the host network."""
    from click.testing import CliRunner
    from botainer.cli.start import start as start_cmd

    project_root = tmp_path / "proj"
    (project_root / ".botainer").mkdir(parents=True)
    (project_root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.chdir(project_root)
    workspace_bind = Bind(
        source="/host/ws", target="/workspace", mode=BindMode.RW,
        provenance=Provenance.CORE, provenance_detail="ws",
        agent_rendering=AgentRendering.SHOWN, self_test="SELFTEST_WORKSPACE_RW",
    )
    fake_spec = SessionSpec(
        session_id="incontainer-net", project_uuid="u", project_root="/workspace",
        state_dir=str(tmp_path / "state"), runtime="apptainer", image="/img.sif",
        mount_plan=MountPlan(binds=(workspace_bind, Bind(
            source="/state/anchor", target="/workspace/.botainer",
            mode=BindMode.NULL_BIND, provenance=Provenance.CORE,
            nested_under="/workspace"))),  # RW /workspace requires the mask
        network=NetworkSpec(mode=NetworkMode.NONE),  # apptainer can't enforce
        entrypoint_wraps=((("/bin/true",),)),
    )
    monkeypatch.setattr(composition, "compose_session", lambda *a, **k: fake_spec)
    monkeypatch.setattr(composition, "run_pre_session_hooks", lambda spec: spec)
    reached = []
    import os as _os
    monkeypatch.setattr(_os, "execvp", lambda p, a: reached.append(p))
    result = CliRunner().invoke(
        start_cmd,
        ["--in-container", "--runtime", "apptainer", "--accept-identity-change", "--yes"],
        catch_exceptions=True,
    )
    assert reached == [], "must refuse before exec, not run on host network"
    assert result.exit_code != 0


# ─────────────── S2: login-node confirmation surfaces binds ────────
#
# NOTE (compose-at-submit, task #52): the four tests that pinned
# _print_login_node_confirmation's SECURITY-POSTURE disclosure (software-root
# binds, plugins/network/auth posture, the network-unenforceable caveat, the
# MOUNT-vs-PROXY credential line) were removed. With compose-at-submit, submit.py
# holds the ACTUAL composed spec and delegates the posture disclosure to
# capability_summary.print_and_maybe_confirm(spec) — a spec-derived
# surface that replaces the old hand-mirrored approximation. The trimmed
# _print_login_node_confirmation now shows ONLY the SLURM scheduler resources.
# Posture disclosure is covered by the capability_summary tests + the composed
# argv/bind assertions in tests/unit/test_hpc_compose_at_submit.py. The
# _refuse_unenforceable_network fast-fail (a pre-compose gate) is kept below.


def test_refuse_unenforceable_network_helper(tmp_path) -> None:
    """Re-audit #9: _refuse_unenforceable_network refuses none/allowlist (the
    compute-node launcher refuses them too — fail fast at submit) and passes
    internet/empty through."""
    import importlib.util as _ilu
    sys.path.insert(0, str(HPC_HOST_HELPER))
    try:
        spec = _ilu.spec_from_file_location("hpc_submit_net2", HPC_HOST_HELPER / "submit.py")
        submit = _ilu.module_from_spec(spec)
        spec.loader.exec_module(submit)
        common = _load_common()
        for bad in ("none", "endpoint-ip-allowlist"):
            assert submit._refuse_unenforceable_network(
                _plan(common, tmp_path, network_mode=bad)) != 0
        for ok in ("", "internet"):
            assert submit._refuse_unenforceable_network(
                _plan(common, tmp_path, network_mode=ok)) == 0
    finally:
        sys.path.remove(str(HPC_HOST_HELPER))


# ─────────────────── policy: SitePolicy-authoritative ──────────────


def test_cluster_software_roots_site_authoritative_user_empty() -> None:
    """A default-empty user policy must NOT zero a site-set ceiling (the bug a
    naive isect_list would cause)."""
    site = SitePolicy(mounts=MountsPolicy(cluster_software_roots=["/apps", "/software"]))
    user = SitePolicy()  # default empty
    eff = intersect(site, user)
    assert eff.mounts.cluster_software_roots == ["/apps", "/software"]


def test_cluster_software_roots_user_cannot_widen() -> None:
    """A user policy cannot ADD a software root (self-grant defense): the site
    value is taken verbatim, ignoring the user list entirely."""
    site = SitePolicy(mounts=MountsPolicy(cluster_software_roots=["/apps"]))
    user = SitePolicy(mounts=MountsPolicy(cluster_software_roots=["/home/attacker"]))
    eff = intersect(site, user)
    assert eff.mounts.cluster_software_roots == ["/apps"]
    assert "/home/attacker" not in eff.mounts.cluster_software_roots


def test_render_human_shows_cluster_software_roots() -> None:
    from botainer.core.policy import render_human
    site = SitePolicy(mounts=MountsPolicy(cluster_software_roots=["/apps"]))
    assert "cluster_software_roots" in render_human(site)
