"""Tests for the hpc-modules plugin + host_pre_launch wiring.

Covers:
- plugin manifest declares host_pre_launch hook + capability
- run_host_pre_launch_hooks fires hooks and merges env_file into spec
- Docker + Apptainer adapters emit --env-file
- the load_modules.py hook itself, mocked at the bash-subprocess boundary
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from botainer.core import composition
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

# ────────── manifest ──────────


def test_hpc_modules_manifest_loads() -> None:
    from botainer.plugins.manifest import load_manifest
    repo_plugins = Path(__file__).resolve().parents[2] / "plugins"
    m = load_manifest(repo_plugins / "hpc-modules")
    assert m.name == "hpc-modules"
    assert m.tier == "first-party"
    assert any(h.when == "host_pre_launch" for h in m.hooks)
    assert "caps.modules_env_override" in m.capabilities


# ────────── adapter rendering: --env-file ──────────


def _spec(**kw) -> SessionSpec:
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    defaults: dict = dict(
        session_id="hpc-1",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="img@sha256:" + "0" * 64,
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
    )
    defaults.update(kw)
    return SessionSpec(**defaults)


def test_docker_adapter_emits_env_file_flag() -> None:
    from botainer.adapters.docker import DockerAdapter

    spec = _spec(env_files=("/tmp/m1.env", "/tmp/m2.env"))
    argv = DockerAdapter().render_argv(spec)
    # Both --env-file paths should appear, in order.
    env_file_args = [argv[i + 1] for i, a in enumerate(argv) if a == "--env-file"]
    assert env_file_args == ["/tmp/m1.env", "/tmp/m2.env"]


def test_docker_adapter_env_file_before_minus_e() -> None:
    """env-file applied first; -e overrides per docker semantics."""
    from botainer.adapters.docker import DockerAdapter
    from botainer.core.spec import EnvSpec
    spec = _spec(
        env_files=("/tmp/m1.env",),
        env=EnvSpec(values={"KEY": "explicit"}),
    )
    argv = DockerAdapter().render_argv(spec)
    env_file_idx = argv.index("--env-file")
    e_idx = argv.index("-e")
    assert env_file_idx < e_idx


def test_apptainer_adapter_emits_env_file_flag() -> None:
    from botainer.adapters.apptainer import ApptainerAdapter

    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    spec = SessionSpec(
        session_id="hpc-2",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="apptainer",
        image="/path/img.sif",
        mount_plan=MountPlan(binds=(workspace_bind,)),
        # Task #88: apptainer refuses NONE; this test exercises env-file
        # rendering, not network policy.
        network=NetworkSpec(mode=NetworkMode.INTERNET),
        env_files=("/tmp/m.env",),
    )
    argv = ApptainerAdapter().render_argv(spec)
    assert "--env-file" in argv
    idx = argv.index("--env-file")
    assert argv[idx + 1] == "/tmp/m.env"


# ────────── run_host_pre_launch_hooks ──────────


def _make_executable(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_run_host_pre_launch_no_hooks_returns_unchanged(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="test1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))
    out = composition.run_host_pre_launch_hooks(spec)
    assert out is spec  # unchanged


def test_run_host_pre_launch_hook_contributes_env_file(tmp_path: Path) -> None:
    """A host_pre_launch hook that returns an env_file path gets merged
    into the spec's env_files field."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True)

    # Create a fake env-file the hook will "produce". AUDIT (H1):
    # it must live UNDER the session state dir (where the real hook writes it,
    # BOTAINER_SESSION_SCRATCH) or the new containment check refuses it.
    fake_env_file = session_dir / "fake.env"
    fake_env_file.write_text("FOO=bar\n")

    hook = tmp_path / "hook.py"
    hook.write_text(
        f"""#!/usr/bin/env python3
import json
print(json.dumps({{
    "version": "plugin-contribution-v1",
    "kind": "host_pre_launch",
    "env_file": {str(fake_env_file)!r},
}}))
"""
    )
    _make_executable(hook)

    spec = SessionSpec(
        session_id="test1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
        hooks=(
            HookSpec(
                plugin="hpc-modules",
                when="host_pre_launch",
                script_path=str(hook),
            ),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    out = composition.run_host_pre_launch_hooks(spec)
    assert out is not spec  # new spec
    assert str(fake_env_file.resolve()) in out.env_files


def _env_file_hook_spec(tmp_path: Path, env_file_path: str) -> "SessionSpec":
    """Build a spec whose host_pre_launch hook emits the given env_file path."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    hook = tmp_path / "hook.py"
    hook.write_text(
        f"""#!/usr/bin/env python3
import json
print(json.dumps({{
    "version": "plugin-contribution-v1",
    "kind": "host_pre_launch",
    "env_file": {env_file_path!r},
}}))
"""
    )
    _make_executable(hook)
    spec = SessionSpec(
        session_id="test1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
        hooks=(
            HookSpec(plugin="hpc-modules", when="host_pre_launch", script_path=str(hook)),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    return spec


def test_host_pre_launch_env_file_outside_state_dir_refused(tmp_path: Path) -> None:
    """AUDIT (H1): the env_file is passed verbatim to --env-file
    (crossing --cleanenv). It must be contained under the session state dir;
    a path outside (e.g. an attacker-pointed /tmp or co-tenant file) is
    refused — the comment-promised containment is now real."""
    outside = tmp_path / "elsewhere.env"
    outside.write_text("FOO=bar\n")
    spec = _env_file_hook_spec(tmp_path, str(outside))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="outside the session state dir"):
        composition.run_host_pre_launch_hooks(spec)


def test_host_pre_launch_env_file_refuses_exec_injection(tmp_path: Path) -> None:
    """AUDIT (H1): the HIGH was that ANY host_pre_launch plugin
    could inject LD_PRELOAD via env_file with no launcher gate. The launcher
    now refuses execution-injection vars in env_file contents regardless of
    the plugin's own self-filtering."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    ef.write_text("PATH=/apps/x/bin\nLD_PRELOAD=/tmp/evil.so\n")
    spec = _env_file_hook_spec(tmp_path, str(ef))
    from botainer.core.refusal import Refused, RefusalCategory
    with pytest.raises(Refused) as exc:
        composition.run_host_pre_launch_hooks(spec)
    assert exc.value.category == RefusalCategory.ENV_VAR_DENIED


@pytest.mark.parametrize("bad_line", [
    "GCONV_PATH=/tmp/evil",            # glibc charset .so load
    "GLIBC_TUNABLES=glibc.x=y",        # Looney-Tunables class
    "LD_PROFILE=x",                    # loader write-primitive
    "DYLD_INSERT_LIBRARIES=/tmp/x.dylib",  # macOS LD_PRELOAD analog
    "HOSTALIASES=/tmp/aliases",        # resolver redirect
])
def test_host_pre_launch_env_file_refuses_loader_hijacks(
    tmp_path: Path, bad_line: str
) -> None:
    """AUDIT (H1, adversarial-review HIGH): the exec-injection set
    must cover the glibc loader/resolver + macOS DYLD families, not just
    LD_PRELOAD — they force code load / redirect resolution and no module
    sets them."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    ef.write_text(f"PATH=/apps/x/bin\n{bad_line}\n")
    spec = _env_file_hook_spec(tmp_path, str(ef))
    from botainer.core.refusal import Refused, RefusalCategory
    with pytest.raises(Refused) as exc:
        composition.run_host_pre_launch_hooks(spec)
    assert exc.value.category == RefusalCategory.ENV_VAR_DENIED


def test_host_pre_launch_env_file_refuses_bash_func(tmp_path: Path) -> None:
    """AUDIT (H1, adversarial-review MEDIUM): a BASH_FUNC_<name>%%
    entry is imported as a shell function by a bash entrypoint and would
    override a command. The gate refuses non-identifier env names (the hook
    drops these by convention, but the gate must not rely on that)."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    ef.write_text("PATH=/apps/x/bin\nBASH_FUNC_id%%=() { id; }\n")
    spec = _env_file_hook_spec(tmp_path, str(ef))
    from botainer.core.refusal import Refused, RefusalCategory
    with pytest.raises(Refused) as exc:
        composition.run_host_pre_launch_hooks(spec)
    assert exc.value.category == RefusalCategory.ENV_VAR_DENIED


def test_host_pre_launch_env_file_refuses_credential(tmp_path: Path) -> None:
    """AUDIT (H1): module env must never carry secrets; the
    credential-leak check now runs on env_file contents too."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    ef.write_text("PATH=/apps/x/bin\nANTHROPIC_API_KEY=sk-leak\n")
    spec = _env_file_hook_spec(tmp_path, str(ef))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused):
        composition.run_host_pre_launch_hooks(spec)


def test_host_pre_launch_env_file_allows_module_path_vars(
    tmp_path: Path, monkeypatch
) -> None:
    """AUDIT (H1): legitimate module library/path-discovery vars
    (PATH, LD_LIBRARY_PATH) must still flow through the CONTENT validator —
    the gate refuses only execution-injection vars + credentials, not the
    feature itself. Includes TOKENIZERS_PARALLELISM (a common ML module var
    that the credential HEURISTIC would false-reject via the ^TOKEN pattern —
    exact-name matching lets it through; adversarial-review false-reject
    finding).

    Scope: this test asserts the CONTENT validator (`_validate_host_env_text`)
    permits these names. The Step-A path-clobber handler (#1) is a SEPARATE
    layer (refuses these path-list vars on direct docker/apptainer flows
    because `--env-file` SETs them — see test_module_env_file_path_clobber_*
    in test_module_binds_wiring.py); opt out here so the two layers can be
    tested independently. Default-flow refusal is covered in those tests.
    """
    # Step-A escape valve so this content-validator test isolates from the
    # path-clobber layer. The path-clobber-default-refuses test asserts the
    # default-refuse behavior separately; this test scopes to the content gate.
    monkeypatch.setenv("BOTAINER_ALLOW_PATH_CLOBBER", "1")
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    # PYTHONPATH is NOT here — it's a botainer-managed route, refused at the gate
    # (see test_host_pre_launch_env_file_refuses_botainer_managed_route below).
    ef.write_text(
        "PATH=/apps/x/bin\nLD_LIBRARY_PATH=/apps/x/lib\nCPATH=/apps/x/include\n"
        "TOKENIZERS_PARALLELISM=false\n"
    )
    spec = _env_file_hook_spec(tmp_path, str(ef))
    out = composition.run_host_pre_launch_hooks(spec)
    assert str(ef.resolve()) in out.env_files


@pytest.mark.parametrize("var", ["PYTHONPATH", "PIP_TARGET", "NODE_PATH", "GOPATH"])
def test_host_pre_launch_env_file_refuses_botainer_managed_route(
    tmp_path: Path, var: str
) -> None:
    """#160 (adversarial-review T2): the launcher — not just the untrusted hook —
    refuses botainer-managed package-routing vars in a module env_file. The
    image points these at /packages/*; a module clobbering them would silently
    redirect the agent's pip/python/node/go routing. PATH/LD_LIBRARY_PATH still
    flow (the documented residual); these do not."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    ef.write_text(f"PATH=/apps/x/bin\n{var}=/apps/x/evil\n")
    spec = _env_file_hook_spec(tmp_path, str(ef))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="botainer-managed"):
        composition.run_host_pre_launch_hooks(spec)


def test_host_pre_launch_env_file_refuses_newline_forged_value(tmp_path: Path) -> None:
    """#160 (adversarial-review T2): a value containing a newline could forge an
    extra KEY=VALUE past the line parser — refused at the gate."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True, exist_ok=True)
    ef = session_dir / "module-env.env"
    # CPATH value carries an embedded NL forging a PIP_TARGET line.
    ef.write_text("CPATH=/apps/x/include\\nPIP_TARGET=/evil\n".replace("\\n", "\n"))
    spec = _env_file_hook_spec(tmp_path, str(ef))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused):
        composition.run_host_pre_launch_hooks(spec)


def test_run_host_pre_launch_hook_refuses_missing_env_file(tmp_path: Path) -> None:
    """If the hook returns an env_file path that doesn't exist, refuse."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True)

    hook = tmp_path / "hook.py"
    hook.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({
    "version": "plugin-contribution-v1",
    "kind": "host_pre_launch",
    "env_file": "/no/such/path.env",
}))
"""
    )
    _make_executable(hook)

    spec = SessionSpec(
        session_id="test1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
        hooks=(
            HookSpec(
                plugin="hpc-modules",
                when="host_pre_launch",
                script_path=str(hook),
            ),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="env_file"):
        composition.run_host_pre_launch_hooks(spec)


def test_run_host_pre_launch_hook_skips_other_whens(tmp_path: Path) -> None:
    """Only `host_pre_launch` hooks fire here; pre_session is left for later."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "test1"
    session_dir.mkdir(parents=True)

    marker = tmp_path / "marker.txt"
    hook = tmp_path / "hook.py"
    hook.write_text(
        f"#!/usr/bin/env python3\nopen({str(marker)!r}, 'w').write('fired')\n"
    )
    _make_executable(hook)

    spec = SessionSpec(
        session_id="test1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
        hooks=(
            HookSpec(plugin="other", when="pre_session", script_path=str(hook)),
        ),
    )
    sr.write(session_dir, sr.from_spec(spec))
    composition.run_host_pre_launch_hooks(spec)
    assert not marker.exists()


# ────────── load_modules.py hook (no real Lmod) ──────────


def _setup_hook_env(
    tmp_path: Path, modules: list[str], bootstrap: str | None = None
) -> dict[str, str]:
    """Create a project root with .botainer/config.yaml + session scratch.

    Returns the env dict to pass to subprocess.run.
    """
    project_root = tmp_path / "proj"
    (project_root / ".botainer").mkdir(parents=True)
    cfg: dict = {"plugins": {"hpc-modules": {"modules": modules}}}
    (project_root / ".botainer" / "config.yaml").write_text(
        json.dumps(cfg)  # YAML is a superset of JSON
    )
    session_scratch = tmp_path / "scratch"
    session_scratch.mkdir()
    env = {
        "BOTAINER_PROJECT_ROOT": str(project_root),
        "BOTAINER_SESSION_SCRATCH": str(session_scratch),
        "BOTAINER_SESSION_ID": "sid",
        "BOTAINER_HOOK_WHEN": "host_pre_launch",
        "BOTAINER_PLUGIN": "hpc-modules",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    # AUDIT C2: the bootstrap path is operator/host-controlled,
    # never project config. Tests inject it via the operator override env.
    if bootstrap is not None:
        env["BOTAINER_LMOD_BOOTSTRAP"] = bootstrap
    return env


HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "hpc-modules"
    / "hooks"
    / "load_modules.py"
)


def test_load_modules_no_modules_emits_empty_contribution(tmp_path: Path) -> None:
    env = _setup_hook_env(tmp_path, modules=[])
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["env_file"] is None
    assert payload["module_list"] == []


def test_load_modules_refuses_no_bootstrap_when_modules_requested(
    tmp_path: Path,
) -> None:
    # Bootstrap autodetect will fail in this clean env (no /etc/profile.d/lmod.sh).
    env = _setup_hook_env(tmp_path, modules=["python/3.11"])
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    # Either bootstrap not found (rc=1) or we're on a host that DOES have
    # Lmod and the module isn't found there (rc=2). Both are valid refusal
    # paths; both should be nonzero.
    assert proc.returncode != 0, (
        f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_load_modules_ignores_project_config_bootstrap_script(tmp_path: Path) -> None:
    """AUDIT (CRITICAL C2): a `bootstrap_script:` in the
    git-shareable project config must NOT be sourced — it was an arbitrary
    code-execution hole on the login/compute node. Here a malicious config
    points bootstrap_script at a script that, if sourced, writes a marker
    file; the hook must ignore it (and, with no operator override and no real
    Lmod in this clean env, refuse for lack of a bootstrap) so the marker
    never appears.
    """
    project_root = tmp_path / "proj"
    (project_root / ".botainer").mkdir(parents=True)
    marker = tmp_path / "PWNED"
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/bash\ntouch {shlex.quote(str(marker))}\n")
    evil.chmod(0o755)
    cfg = {
        "plugins": {
            "hpc-modules": {
                "modules": ["python/3.11"],
                "bootstrap_script": str(evil),  # attacker-controlled, must be ignored
            }
        }
    }
    (project_root / ".botainer" / "config.yaml").write_text(json.dumps(cfg))
    session_scratch = tmp_path / "scratch"
    session_scratch.mkdir()
    env = {
        "BOTAINER_PROJECT_ROOT": str(project_root),
        "BOTAINER_SESSION_SCRATCH": str(session_scratch),
        "BOTAINER_SESSION_ID": "sid",
        "BOTAINER_HOOK_WHEN": "host_pre_launch",
        "BOTAINER_PLUGIN": "hpc-modules",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        # No BOTAINER_LMOD_BOOTSTRAP, no LMOD_PKG/LMOD_CMD → no bootstrap.
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert not marker.exists(), (
        "project-config bootstrap_script was sourced (RCE) — it must be ignored"
    )
    # With no operator bootstrap available, the hook refuses rather than
    # silently proceeding.
    assert proc.returncode != 0, (
        f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_load_modules_refuses_world_writable_bootstrap(tmp_path: Path) -> None:
    """AUDIT (C2 hardening): even an operator-supplied bootstrap is
    refused if it is world-writable (any local user could swap in code that
    runs as us — the multi-tenant HPC threat model)."""
    bootstrap = tmp_path / "loose-lmod.sh"
    bootstrap.write_text("#!/bin/bash\n: # noop\n")
    bootstrap.chmod(0o757)  # world-writable
    env = _setup_hook_env(tmp_path, modules=["python/3.11"], bootstrap=str(bootstrap))
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1, (
        f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "world-writable" in proc.stderr


def test_load_modules_with_fake_bootstrap_writes_env_file(tmp_path: Path) -> None:
    """End-to-end with a synthetic bootstrap script that fakes the
    `module` function. Verifies env-file is written and structured
    contribution is emitted."""
    # Fake bootstrap defining `module` as a no-op that exports a few vars
    # and dumps a fake `module list` to stderr.
    bootstrap = tmp_path / "fake-lmod.sh"
    bootstrap.write_text(
        """#!/bin/bash
module() {
    case "$1" in
        purge) ;;
        load)
            shift
            for m in "$@"; do
                case "$m" in
                    python/3.11)
                        export PATH="/fake/python/3.11/bin:$PATH"
                        export PYTHONPATH="/fake/python/3.11/lib"
                        ;;
                    cuda/12.3)
                        export CUDA_HOME="/fake/cuda/12.3"
                        export LD_LIBRARY_PATH="/fake/cuda/12.3/lib64:$LD_LIBRARY_PATH"
                        ;;
                    *) echo "MODULE_MISSING: $m" >&2 ;;
                esac
            done
            ;;
        --terse)
            shift
            if [ "$1" = "list" ]; then
                echo "Currently Loaded Modulefiles:" >&2
                echo "python/3.11" >&2
                echo "cuda/12.3" >&2
            fi
            ;;
    esac
}
export -f module
"""
    )
    bootstrap.chmod(0o755)

    env = _setup_hook_env(
        tmp_path, modules=["python/3.11", "cuda/12.3"], bootstrap=str(bootstrap)
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout)
    assert payload["env_file"] is not None
    env_file = Path(payload["env_file"])
    assert env_file.exists()
    content = env_file.read_text()
    # PATH passes through (module's python on PATH).
    assert "PATH=" in content
    assert "/fake/python/3.11/bin" in content
    # CUDA_HOME + LD_LIBRARY_PATH pass through (trusted module-env vars).
    assert "CUDA_HOME=/fake/cuda/12.3" in content
    assert "LD_LIBRARY_PATH=" in content
    # PYTHONPATH is botainer-managed (Dockerfile sets it to /packages/pip);
    # module env value should NOT override.
    assert "PYTHONPATH=" not in content
    # Module list captured.
    names = [m["name"] for m in payload["module_list"]]
    assert "python" in names
    assert "cuda" in names


def test_load_modules_refuses_missing_module_when_fail_on_missing(
    tmp_path: Path,
) -> None:
    """Bootstrap reports a missing module; with fail_on_missing=True
    (default), the hook refuses. Real Lmod returns nonzero on missing
    modules; our fake mimics this so the hook's `module load X || ...`
    fallback fires.
    """
    bootstrap = tmp_path / "fake-lmod.sh"
    bootstrap.write_text(
        """#!/bin/bash
module() {
    case "$1" in
        purge) ;;
        load)
            shift
            # Real Lmod returns nonzero when modules aren't found.
            return 1
            ;;
        --terse) ;;
    esac
}
export -f module
"""
    )
    bootstrap.chmod(0o755)
    env = _setup_hook_env(
        tmp_path, modules=["python/9.99"], bootstrap=str(bootstrap)
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "modules not found" in proc.stderr


def test_load_modules_does_not_clobber_botainer_managed_vars(
    tmp_path: Path,
) -> None:
    """Module-set PIP_TARGET should NOT make it into the env-file (the
    Dockerfile sets PIP_TARGET=/packages/pip and modules must not override)."""
    bootstrap = tmp_path / "fake-lmod.sh"
    bootstrap.write_text(
        """#!/bin/bash
module() {
    case "$1" in
        purge) ;;
        load)
            export PIP_TARGET="/oops/host/path"
            export PATH="/fake/bin:$PATH"
            ;;
        --terse) echo "x/1" >&2 ;;
    esac
}
export -f module
"""
    )
    bootstrap.chmod(0o755)
    env = _setup_hook_env(
        tmp_path, modules=["x/1"], bootstrap=str(bootstrap)
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    env_file = Path(payload["env_file"])
    content = env_file.read_text()
    assert "PIP_TARGET" not in content, (
        "module-set PIP_TARGET must be stripped (botainer-managed routing)"
    )
    # But PATH passes through.
    assert "PATH=" in content


# ────────── #160: software_root_env double-capture + drift ──────────


def _fake_lmod_with_paths(tmp_path: Path) -> Path:
    bootstrap = tmp_path / "fake-lmod.sh"
    bootstrap.write_text(
        """#!/bin/bash
module() {
    case "$1" in
        purge) ;;
        load)
            shift
            for m in "$@"; do
                case "$m" in
                    python/3.11)
                        export PATH="/fake/python/3.11/bin:$PATH"
                        export PYTHONPATH="/fake/python/3.11/lib"
                        ;;
                    cuda/12.3)
                        export CUDA_HOME="/fake/cuda/12.3"
                        export LD_LIBRARY_PATH="/fake/cuda/12.3/lib64:$LD_LIBRARY_PATH"
                        ;;
                esac
            done
            ;;
        --terse) shift; [ "$1" = list ] && { echo "python/3.11" >&2; echo "cuda/12.3" >&2; } ;;
    esac
}
export -f module
"""
    )
    bootstrap.chmod(0o755)
    return bootstrap


def test_load_modules_emits_software_root_env(tmp_path: Path) -> None:
    """#160: the hook captures env TWICE (post-purge baseline + post-load) and
    emits both PATH-var subsets in `software_root_env`, restricted to the
    location vars (PYTHONPATH excluded). Feeding the result to the SAME
    derivation composition uses yields the expected RO roots."""
    bootstrap = _fake_lmod_with_paths(tmp_path)
    env = _setup_hook_env(
        tmp_path, modules=["python/3.11", "cuda/12.3"], bootstrap=str(bootstrap)
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    sw = payload["software_root_env"]
    # loaded carries the module-added location vars...
    assert "/fake/python/3.11/bin" in sw["loaded"]["PATH"]
    assert sw["loaded"].get("CUDA_HOME") == "/fake/cuda/12.3"
    assert "/fake/cuda/12.3/lib64" in sw["loaded"].get("LD_LIBRARY_PATH", "")
    # baseline (post-purge, pre-load) does NOT have the /fake additions.
    assert "/fake/python/3.11/bin" not in sw["baseline"].get("PATH", "")
    # PYTHONPATH is a package-routing var — EXCLUDED from software_root_env so a
    # modulefile can't smuggle a bind / redirect package routing.
    assert "PYTHONPATH" not in sw["loaded"]

    from botainer.hpc.module_binds import derive_software_root_binds
    binds = derive_software_root_binds(sw["baseline"], sw["loaded"], ["/fake"])
    targets = sorted(b["target"] for b in binds)
    assert targets == [
        "/fake/cuda/12.3",          # CUDA_HOME
        "/fake/cuda/12.3/lib64",    # LD_LIBRARY_PATH add
        "/fake/python/3.11/bin",    # PATH add
    ]
    assert all(b["mode"] == "ro" and b["source"] == b["target"] for b in binds)


def test_load_modules_derive_only_does_not_abort_on_missing(tmp_path: Path) -> None:
    """#160 B3: with BOTAINER_MODBINDS_DERIVE_ONLY=1, a missing module must NOT
    abort (exit 2) — the plan-time discovery pass loads what resolves and emits
    software_root_env for it, so a login-node-invisible module doesn't block
    submission. (Without the flag, the same setup returns exit 2.)"""
    bootstrap = tmp_path / "fake-lmod.sh"
    bootstrap.write_text(
        """#!/bin/bash
module() {
    case "$1" in
        purge) ;;
        load)
            shift
            for m in "$@"; do
                case "$m" in
                    python/3.11) export PATH="/fake/python/3.11/bin:$PATH" ;;
                    *) echo "MODULE_MISSING: $m" >&2; return 1 ;;
                esac
            done
            ;;
        --terse) shift; [ "$1" = list ] && echo "python/3.11" >&2 ;;
    esac
}
export -f module
"""
    )
    bootstrap.chmod(0o755)
    env = _setup_hook_env(
        tmp_path, modules=["python/3.11", "ghost/9.9"], bootstrap=str(bootstrap)
    )
    env["BOTAINER_MODBINDS_DERIVE_ONLY"] = "1"
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    # The resolvable module's root is still captured for binding.
    assert "/fake/python/3.11/bin" in payload["software_root_env"]["loaded"]["PATH"]


def _load_hook_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hpc_modules_hook_drift", HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_pathvars_superset_of_module_binds() -> None:
    """DRIFT GUARD (the standalone-HPC-mirror meta-pattern): the hook's
    _SOFTWARE_ROOT_PATH_VARS MUST be a superset of the canonical
    module_binds.PATH_VARS. The hook is stdlib-only and can't import the
    canonical set; if a new PATH_VAR is added to the derivation but not the
    hook, that var's dirs would never be emitted and silently become
    unreachable. This test fails the instant they drift."""
    from botainer.hpc.module_binds import PATH_VARS
    hook = _load_hook_module()
    assert PATH_VARS <= hook._SOFTWARE_ROOT_PATH_VARS, (
        "module_binds.PATH_VARS has vars the hook doesn't emit: "
        f"{sorted(PATH_VARS - hook._SOFTWARE_ROOT_PATH_VARS)}"
    )


def test_managed_routes_pinned_to_hook() -> None:
    """#160 (adversarial-review T2): the launcher chokepoint's
    _BOTAINER_MANAGED_ROUTES must stay in sync with the hook's _BOTAINER_MANAGED
    (the comment + CAPABILITY-SURFACE §4g claim a drift test pins them — this is
    it). The launcher set must BACKSTOP everything the hook strips."""
    from botainer.core.composition import _BOTAINER_MANAGED_ROUTES
    hook = _load_hook_module()
    # SYMMETRIC, and it was not. `hook <= routes` only asks whether the
    # launcher backstops what the hook strips. The direction that mattered is
    # the other one: a var in ROUTES and missing from the hook's strip set ends
    # up IN the env-file, and the launcher then refuses the session. HOME was
    # exactly that from 2026-07-13 to 2026-09-03 — in routes, not in the hook —
    # and this assertion could not see it by construction.
    assert set(hook._BOTAINER_MANAGED) == set(_BOTAINER_MANAGED_ROUTES), (
        "the hook's strip set and the launcher's route set have drifted.\n"
        f"  in the hook but not backstopped: "
        f"{sorted(set(hook._BOTAINER_MANAGED) - set(_BOTAINER_MANAGED_ROUTES))}\n"
        f"  in ROUTES but NOT STRIPPED by the hook: "
        f"{sorted(set(_BOTAINER_MANAGED_ROUTES) - set(hook._BOTAINER_MANAGED))}\n"
        "  The second list is the fatal one: those vars survive into the "
        "env-file,\n  and _validate_host_env_file refuses any env-file that "
        "sets a route var,\n  so the hook succeeds and every session is "
        "refused."
    )


def test_hpc_modules_manifest_declares_software_roots_cap() -> None:
    """#160: the manifest must declare caps.modules_software_roots (the
    composition cap-grant gate loads this; without it the binds are refused)."""
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(HPC_MODULES_DIR)
    assert "caps.modules_software_roots" in m.capabilities


HPC_MODULES_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hpc-modules"


def test_env_file_refuses_policy_denylisted_ca_bundle() -> None:
    """Re-audit (audit 2): the env-file chokepoint enforces the policy
    env_var_denylist (except curated location vars) — a hostile modulefile
    setting SSL_CERT_FILE/REQUESTS_CA_BUNDLE (TLS-trust hijack) is refused,
    while PATH/LD_LIBRARY_PATH still flow even if denylisted."""
    from botainer.core.composition import _validate_host_env_text
    from botainer.core.refusal import Refused
    dl = {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PYTHONHOME",
          "LD_LIBRARY_PATH", "PATH"}
    for v in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PYTHONHOME"):
        with pytest.raises(Refused, match="env_var_denylist|TLS"):
            _validate_host_env_text(f"{v}=/apps/evil\n", plugin="x", denylist=dl)
    # Curated location vars flow even when denylisted (else modules break).
    _validate_host_env_text(
        "PATH=/apps/bin\nLD_LIBRARY_PATH=/apps/lib\n", plugin="x", denylist=dl
    )


def test_the_hooks_real_env_file_passes_the_real_validator(tmp_path) -> None:
    """THE COMPOSITION NOBODY RAN, and the reason a broken plugin shipped.

    Two tests already existed and both were green: one asserts this hook writes
    an env-file, another asserts `_validate_host_env_file` refuses an env-file
    that sets HOME. Neither ran the FIRST's output through the SECOND. Between
    them sat a live defect — every env-file this hook produced carried HOME, so
    the hook succeeded and the launcher then refused the session, on every host,
    from 5cc06e8 (2026-07-13) until 2026-09-03.

    Measured A/B on the fix:
        without: keys [CUDA_HOME, HOME, LD_LIBRARY_PATH, PATH, PWD, USER]
                 -> [env-var-denied] ... sets botainer-managed ... 'HOME'
        with:    keys [CUDA_HOME, LD_LIBRARY_PATH, PATH, PWD, USER]
                 -> accepted
    """
    import subprocess
    import sys

    from botainer.core import composition as _c

    bootstrap = tmp_path / "fake-lmod.sh"
    bootstrap.write_text(
        '#!/bin/bash\n'
        'module() { case "$1" in\n'
        '    purge) ;;\n'
        '    load) shift; for m in "$@"; do case "$m" in\n'
        '        cuda/12.3) export CUDA_HOME=/fake/cuda;'
        ' export LD_LIBRARY_PATH=/fake/cuda/lib;; esac; done ;;\n'
        '    --terse) shift; [ "$1" = list ] && echo "cuda/12.3" >&2 ;; esac; }\n'
        'export -f module\n',
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "version: botainer-project-v1\n"
        "plugins:\n  hpc-modules:\n    modules: [cuda/12.3]\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "BOTAINER_PROJECT_ROOT": str(proj),
            "BOTAINER_SESSION_SCRATCH": str(scratch),
            "BOTAINER_LMOD_BOOTSTRAP": str(bootstrap),
        },
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"

    env_file = scratch / "module-env.env"
    assert env_file.is_file(), (
        f"the hook wrote no env-file; scratch holds "
        f"{sorted(p.name for p in scratch.iterdir())}"
    )
    keys = [ln.split("=", 1)[0] for ln in
            env_file.read_text(encoding="utf-8").splitlines() if "=" in ln]
    assert "CUDA_HOME" in keys, (
        f"guard: the fixture's module did not take effect, so this test is not "
        f"exercising a real env diff (keys: {keys})"
    )
    assert "HOME" not in keys, (
        f"the env-file carries HOME (keys: {keys}). The launcher refuses any "
        f"env-file that sets a botainer-managed route var, so hpc-modules "
        f"would succeed and every session would then be refused."
    )

    # And the real validator, called the way composition.py calls it.
    _c._validate_host_env_file(env_file, plugin="hpc-modules")
