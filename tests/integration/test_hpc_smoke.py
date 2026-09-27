"""Wiring guardrail for the HPC submit flow.

Same shape as `test_session_smoke.py` but for the apptainer / Slurm
side: walk the contract every wiring point in `bot1 hpc submit` →
sbatch script → `apptainer exec` → inner `botainer start --runtime
apptainer` relies on, and assert each is correctly threaded.

Some assertions in this file are marked `xfail` — they document
KNOWN gaps in the HPC flow that didn't get fixed in the night session,
captured here so they don't regress further and so any future fix
flips xfail to xpass loudly.

Bug classes this catches (or pins as known-broken):

  PASSES:
  - make_plan honors BOTAINER_STATE_ROOT (canonical env-var)
  - make_plan falls back to MY_BOTAINER if STATE_ROOT absent
  - to_sbatch_argv uses state_root in --output path
  - sbatch script has the project_uuid in --job-name + comment

  XFAIL (known gaps, tracked internally):
  - render_sbatch_script preserves MY_BOTAINER/BOTAINER_STATE_ROOT
    for the inner apptainer invocation (currently --cleanenv scrubs
    everything; the inner botainer can't find host state)
  - to_apptainer_argv binds the state dir so the inner botainer start
    can read shared-auth credentials / installed.lock / plugins tree
    (currently only project_root is bound)

The point of pinning the known gaps as xfail rather than skipping
them entirely: any fix to either gap immediately surfaces as xpass
and forces removal of the marker, ensuring the test moves to a real
passing assertion.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_smoke", HPC_HOST_HELPER / "_common.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_smoke"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_plan(common, *, state_root: Path, uuid: str = "1" * 32) -> object:
    return common.SubmissionPlan(
        project_root=state_root.parent / "proj",
        project_uuid=uuid,
        state_root=state_root,
        profile="default",
        partition="day",
        account="prj1",
        time_minutes=120,
        cpus=2,
        memory_gb=8,
        gpus=0,
        gpu_type=None,
        apptainer_image="botainer-claude.sif",
        submission_mode="submit",
        existing_jobid=None,
        # Compose-at-submit: the composed compute-node argv (what
        # composition.compose_agent_exec_for_hpc produces). Minimal valid form
        # for the render tests; composition parity is pinned in
        # tests/unit/test_hpc_compose_at_submit.py.
        agent_exec_argv=(
            "apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
            "--drop-caps", "all", "--bind=/home/u/proj:/workspace",
            "botainer-claude.sif", "/usr/local/bin/agent-claude-entrypoint",
        ),
    )


# ── env-var canonical wiring ───────────────────────────────────────────


def test_hpc_make_plan_uses_canonical_state_root_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """make_plan must read BOTAINER_STATE_ROOT (the launcher-resolved
    canonical name). Bug class: hooks reading MY_BOTAINER directly while
    the launcher passes only BOTAINER_STATE_ROOT — same shape as the
    bug that hit pre_session.py in commit 7d7ae39."""
    common = _load_common()
    target = tmp_path / "my-state"
    target.mkdir()
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(target))
    monkeypatch.delenv("MY_BOTAINER", raising=False)
    # Canonical-form uuid: SubmissionPlan.__post_init__ now rejects a
    # non-uuid project_uuid (AC7 sbatch-injection fix). "a"*32 is valid hex
    # and parses as a UUID; this test only exercises state-root resolution.
    monkeypatch.setenv("BOTAINER_PROJECT_UUID", "a" * 32)
    # Project root won't exist on disk; load_plugin_config tolerates that.
    plan = common.make_plan(tmp_path / "proj-doesnt-exist")
    assert plan.state_root == target.resolve() or plan.state_root == target, (
        f"plan.state_root must come from BOTAINER_STATE_ROOT; got "
        f"{plan.state_root!r} expected {target!r}"
    )


def test_hpc_make_plan_falls_back_to_my_botainer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """For backward-compat, MY_BOTAINER is still honored when
    BOTAINER_STATE_ROOT isn't set (standalone testing, scripts that
    didn't get migrated). This guards the fallback ladder in the
    canonical reader: STATE_ROOT > MY_BOTAINER > ~/.botainer."""
    common = _load_common()
    target = tmp_path / "alt-state"
    target.mkdir()
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.setenv("MY_BOTAINER", str(target))
    # Canonical-form uuid: SubmissionPlan.__post_init__ now rejects a
    # non-uuid project_uuid (AC7 sbatch-injection fix). "a"*32 is valid hex
    # and parses as a UUID; this test only exercises state-root resolution.
    monkeypatch.setenv("BOTAINER_PROJECT_UUID", "a" * 32)
    plan = common.make_plan(tmp_path / "proj")
    # _common.py:272 reads BOTAINER_STATE_ROOT; falls back to ~/.botainer.
    # MY_BOTAINER falls back via _resolve_state_root in the launcher, NOT
    # in the plugin script. So this test passes only if the plugin script
    # explicitly checks MY_BOTAINER as a fallback.
    assert plan.state_root == target.resolve() or plan.state_root == target, (
        f"plan.state_root must fall back to MY_BOTAINER when STATE_ROOT "
        f"is absent; got {plan.state_root!r}"
    )


# ── sbatch script correctness ──────────────────────────────────────────


def test_hpc_sbatch_argv_output_path_uses_state_root(tmp_path: Path) -> None:
    """`--output=<state_root>/hpc-job-outputs/<uuid>/slurm-%j.out` must
    interpolate state_root correctly (bug class: a stale env-var read →
    output path that doesn't exist on the compute node) AND live in the
    host-only dir, NOT the container-bound state/<uuid>/ subtree
    (security-audit Finding 1)."""
    common = _load_common()
    state = tmp_path / "state"
    plan = _make_plan(common, state_root=state)
    argv = plan.to_sbatch_argv()
    out_flag = next(a for a in argv if a.startswith("--output="))
    assert str(state) in out_flag, (
        f"sbatch --output must reference state_root; got {out_flag!r}"
    )
    assert plan.project_uuid in out_flag
    # Finding 1: NOT under the container-bound state/<uuid>/ subtree.
    assert f"{state}/state/{plan.project_uuid}" not in out_flag
    assert "hpc-job-outputs" in out_flag


def test_hpc_sbatch_script_records_project_uuid(tmp_path: Path) -> None:
    """The generated sbatch script must carry the project UUID in both
    the comment header (for human debugging) and the --job-name (for
    `squeue -n` filtering). If either is wrong, the user can't find
    their job by name in the queue."""
    common = _load_common()
    plan = _make_plan(common, state_root=tmp_path / "state", uuid="b" * 32)
    script = plan.render_sbatch_script()
    assert "project_uuid: " + ("b" * 32) in script
    # job-name truncates uuid to first 8 chars
    assert "botainer-bbbbbbbb" in script


# ── apptainer invocation: known gaps documented as xfail ───────────────


# NOTE (compose-at-submit, task #52): test_hpc_apptainer_binds_only_this_
# projects_state and test_hpc_apptainer_shared_auth_agent_subdir_is_rw were
# removed — to_apptainer_argv no longer BUILDS binds (it returns the composed
# agent_exec_argv verbatim). The narrowing invariants they pinned (no
# state_root umbrella; only the active agent's shared-auth, RW) are now enforced
# by the ApptainerAdapter + the composition credential hook and pinned by
# tests/unit/test_hpc_compose_at_submit.py (parity + no-umbrella + shared-auth
# present). The Codex Priority-A HIGH is CLOSED harder: the whole state/<uuid>
# subtree is no longer bound at all.


def test_hpc_apptainer_propagates_slurm_tmpdir(tmp_path: Path) -> None:
    """Per codex review HIGH #4: nudge requires SLURM_TMPDIR for the
    apptainer runtime; --cleanenv strips it. Submit must re-inject.

    Task #197: SLURM_TMPDIR is no longer in to_apptainer_argv() because
    shlex.quote() inside render_sbatch_script() would single-quote the
    \\${SLURM_TMPDIR} placeholder and prevent bash expansion. It's now
    spliced UNQUOTED into the rendered sbatch script. Check the script
    rather than the argv list.

 — THIS ASSERTION USED TO PIN THE BUG. It required the literal
    `SLURM_TMPDIR=$SLURM_TMPDIR`, i.e. the UNGUARDED form, which under the
    script's `set -euo pipefail` aborts the whole job on any site that does not
    export the variable (it is not a stock Slurm export). So the test passed
    for as long as the defect existed and would have failed on the fix.

    The lesson is about the assertion's SHAPE, not the typo: pinning an exact
    rendered string makes a test agree with whatever the code currently emits.
    It now asserts the two things actually required — the variable is
    propagated, AND the reference is guarded — and
    test_hpc_launcher.py::test_generated_script_runs_on_a_site_that_does_not_
    export_SLURM_TMPDIR covers the behaviour by executing the script, which is
    the only way to catch a `set -u` abort at all.
    """
    common = _load_common()
    plan = _make_plan(common, state_root=tmp_path / "state")
    script = plan.render_sbatch_script()
    assert "SLURM_TMPDIR=" in script, (
        f"SLURM_TMPDIR must be propagated for nudge cross-node delivery; "
        f"got sbatch script: {script}"
    )
    assert "SLURM_TMPDIR=$SLURM_TMPDIR" not in script, (
        "the SLURM_TMPDIR reference is UNGUARDED. The script runs under "
        "`set -u`, so this aborts the job before the agent starts on any site "
        "that does not export it. Use ${SLURM_TMPDIR:-/tmp}.\n"
        f"got sbatch script: {script}"
    )


def test_hpc_resolve_apptainer_image_honors_lock_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per codex review HIGH #2: image build + submit must agree on the
    same image. The build writes `apptainer:sha256:<hex>:<path>` to
    installed.lock; the resolver must parse it back out."""
    common = _load_common()
    state = tmp_path / "state"
    plugins_dir = state / "plugins"
    plugins_dir.mkdir(parents=True)
    expected_sif = state / "images" / "botainer-agent-claude.sif"
    expected_sif.parent.mkdir()
    expected_sif.touch()
    # Synthesize an installed.lock entry as `bot1 image build` would write.
    lock = plugins_dir / "installed.lock"
    lock.write_text(
        '{"name":"agent-claude","version":"0.1.0","source":"bundled:agent-claude",'
        '"tree_sha":"sha256:0","image_digest":"apptainer:sha256:abcd:' + str(expected_sif) + '",'
        '"installed_at":"2026-05-18T00:00:00+00:00","tier":"first-party"}\n',
        encoding="utf-8",
    )
    resolved = common._resolve_apptainer_image({}, tmp_path / "proj", state, "claude")
    assert resolved == str(expected_sif), (
        f"resolver should pull the .sif path from installed.lock; "
        f"got {resolved!r}"
    )


def test_hpc_resolve_apptainer_image_top_level_image_wins(
    tmp_path: Path,
) -> None:
    """Top-level `image:` in project config takes precedence over the
    lock-recorded image — same as docker mode."""
    common = _load_common()
    state = tmp_path / "state"
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\n"
        "agent: claude\n"
        "image: /custom/path/to/agent.sif\n",
        encoding="utf-8",
    )
    resolved = common._resolve_apptainer_image({}, proj, state, "claude")
    assert resolved == "/custom/path/to/agent.sif"


def test_hpc_resolve_apptainer_image_falls_back_to_convention(
    tmp_path: Path,
) -> None:
    """When no config + no lock entry, resolver falls back to the
    conventional path that `bot1 image build --runtime apptainer`
    actually writes to."""
    common = _load_common()
    state = tmp_path / "state"
    resolved = common._resolve_apptainer_image({}, tmp_path / "proj", state, "claude")
    assert resolved == str(state / "images" / "botainer-agent-claude.sif")


def test_hpc_prepare_host_paths_creates_bind_sources(tmp_path: Path) -> None:
    """Per codex review HIGH #2: apptainer refuses on missing bind
    sources. prepare_host_paths must create them on the login-node
    side BEFORE apptainer exec runs."""
    common = _load_common()
    state = tmp_path / "state"
    plan = _make_plan(common, state_root=state, uuid="b" * 32)
    plan = plan.__class__(**{**plan.__dict__, "agent_name": "claude"})
    # Before: nothing exists.
    per_proj = state / "state" / ("b" * 32)
    assert not per_proj.exists()
    plan.prepare_host_paths()
    # After: per-project state dir AND per-agent profile dir exist.
    assert per_proj.is_dir()
    per_agent = per_proj / "data" / "agent-claude" / "profiles" / "default"
    assert per_agent.is_dir(), (
        "per-agent profile dir must be pre-created so the apptainer "
        "/home/agent/.<name> bind doesn't refuse"
    )


# NOTE (compose-at-submit, task #52): test_hpc_apptainer_propagates_state_root_env
# and test_hpc_apptainer_propagates_my_botainer_env were removed — and their
# invariant INVERTED. Those vars existed ONLY so the inner `botainer start
# --in-container` could locate host state; there is no inner botainer anymore,
# so BOTAINER_STATE_ROOT / MY_BOTAINER are deliberately NOT propagated into the
# container (narrowing — parity with docker/direct, which never exposed them).
# test_hpc_compose_at_submit.py::test_hpc_argv_has_no_state_umbrella_bind pins
# their ABSENCE.


# ── apptainer build is now first-class via `bot1 image build` ──────────


def test_bot1_image_build_supports_apptainer_runtime_choice() -> None:
    """`bot1 image build --runtime apptainer` is a real code path,
    not a "use apptainer build directly" workaround. CLAUDE.md
    mandates HPC parity; this test pins it."""
    from click.testing import CliRunner

    from botainer.cli.image import build
    runner = CliRunner()
    result = runner.invoke(build, ["--help"])
    assert result.exit_code == 0, result.output
    assert "apptainer" in result.output.lower(), (
        "`bot1 image build --help` must mention apptainer as a runtime "
        "choice. CLAUDE.md: HPC parity is non-negotiable."
    )


def test_resolve_build_runtime_picks_apptainer_when_docker_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-runtime must pick apptainer on a host where docker isn't
    installed but apptainer is (HPC login nodes). Without this, every
    `botainer image build` on such a host would refuse."""
    import botainer.cli.image as image_mod

    def fake_which(name: str):
        return None if name == "docker" else f"/usr/bin/{name}"

    monkeypatch.setattr(image_mod.shutil, "which", fake_which)
    chosen = image_mod._resolve_build_runtime("auto")
    assert chosen == "apptainer", (
        f"auto-runtime must pick apptainer when docker is absent and "
        f"apptainer is present (HPC login-node case); got {chosen!r}"
    )


# ── compute-node container execs the AGENT, never botainer ─────────────


def test_hpc_compute_node_execs_agent_not_botainer(tmp_path: Path) -> None:
    """Compose-at-submit (task #52) INVERTS the old HPC-IMPL #3 contract: the
    compute-node container must exec the AGENT entrypoint directly, NEVER
    `botainer start --in-container` — the agent .sif bundles no botainer CLI,
    which is the FATAL this restructure fixes. The element after the image is
    the agent entrypoint (or the module trampoline `bash`), and `botainer` /
    `--in-container` appear nowhere in the exec line."""
    common = _load_common()
    plan = _make_plan(common, state_root=tmp_path / "state")
    argv = plan.to_apptainer_argv()
    assert plan.apptainer_image in argv
    inner = argv[argv.index(plan.apptainer_image) + 1:]
    assert inner, "no entrypoint after the image"
    assert inner[0] != "botainer"
    assert "botainer" not in inner, inner
    assert "--in-container" not in argv, argv
    # The rendered sbatch script must not exec botainer either.
    script = plan.render_sbatch_script()
    assert "botainer start" not in script
    assert "--in-container" not in script
