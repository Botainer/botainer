"""Tests for the hpc-launcher plugin scripts.

The host_helper scripts are stand-alone Python; we import them as modules
and assert sbatch-script generation + flag rendering snapshot.
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
        "hpc_launcher_common", HPC_HOST_HELPER / "_common.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common"] = mod
    spec.loader.exec_module(mod)
    return mod


def _agent_argv(image: str = "botainer-claude.sif") -> tuple[str, ...]:
    """A minimal valid compute-node exec argv — what
    composition.compose_agent_exec_for_hpc stores on the plan. Passes the
    SubmissionPlan.__post_init__ chokepoint (apptainer exec + §4 cage flags +
    the image + an agent entrypoint, never `botainer`). Lets the plan-level
    render tests run without a full compose (composition-level parity is pinned
    by tests/unit/test_hpc_compose_at_submit.py)."""
    return (
        "apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
        "--drop-caps", "all", "--bind=/home/u/proj:/workspace", image,
        "/usr/local/bin/agent-claude-entrypoint",
    )


def test_sbatch_argv_includes_required_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common = _load_common()
    plan = common.SubmissionPlan(
        project_root=tmp_path,
        project_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        state_root=tmp_path / "state",
        profile="default",
        partition="day",
        account="prj1",
        time_minutes=240,
        cpus=2,
        memory_gb=8,
        gpus=1,
        gpu_type="a100",
        apptainer_image="botainer-claude.sif",
        submission_mode="submit",
        existing_jobid=None,
    )
    argv = plan.to_sbatch_argv()
    assert "--partition=day" in argv
    assert "--account=prj1" in argv
    assert "--cpus-per-task=2" in argv
    assert "--mem=8G" in argv
    assert "--gres=gpu:a100:1" in argv
    # Time formatted hh:mm:ss
    time_arg = next(a for a in argv if a.startswith("--time="))
    assert time_arg == "--time=04:00:00"


def test_sbatch_script_is_self_contained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    common = _load_common()
    plan = common.SubmissionPlan(
        project_root=tmp_path,
        project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=tmp_path / "state",
        profile="default",
        partition="day",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="botainer-claude.sif",
        submission_mode="submit",
        existing_jobid=None,
        agent_exec_argv=_agent_argv(),
    )
    script = plan.render_sbatch_script()
    assert script.startswith("#!/bin/bash")
    assert "#SBATCH --partition=day" in script
    assert "#SBATCH --time=01:00:00" in script
    assert "botainer-claude.sif" in script
    assert "exec apptainer" in script
    # Compose-at-submit: the container execs the AGENT, never botainer.
    assert "botainer start" not in script
    assert "--in-container" not in script
    assert "/usr/local/bin/agent-claude-entrypoint" in script
    # §A19: without nudge_enabled, no screen wrap.
    assert "screen -dmS" not in script


def test_sbatch_script_wraps_apptainer_in_screen_when_nudge_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§A19: with the nudge plugin enabled, the sbatch script wraps
    apptainer exec in `screen -dmS botainer-${SLURM_JOB_ID}` so
    `botainer nudge` can `srun --overlap` into the compute node and
    `screen -X stuff --` against that session."""
    common = _load_common()
    plan = common.SubmissionPlan(
        project_root=tmp_path,
        project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=tmp_path / "state",
        profile="default",
        partition="day",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="botainer-claude.sif",
        submission_mode="submit",
        existing_jobid=None,
        nudge_enabled=True,
        agent_exec_argv=_agent_argv(),
    )
    script = plan.render_sbatch_script()
    # T3-7: the screen wrap is GUARDED by `command -v screen` so a compute
    # node without screen degrades gracefully instead of killing the job
    # under `set -euo pipefail`.
    assert "if command -v screen >/dev/null 2>&1; then" in script
    # Screen session named after Slurm jobid (matches hpc-launcher
    # attach.py's `screen -r botainer-<jobid>` convention).
    assert "SCREEN_SID=\"botainer-${SLURM_JOB_ID" in script
    assert "screen -dmS \"$SCREEN_SID\"" in script
    # Wait loop keeps the sbatch step alive while the screen lives.
    assert "while screen -ls" in script
    # Fallback: when screen is absent, warn LOUDLY and exec the agent
    # directly so the job still runs (nudge/attach just unavailable).
    assert "not on this compute node" in script
    assert "exec apptainer" in script  # the else-branch fallback
    # apptainer exec is the inner command on both paths.
    assert "apptainer exec" in script
    # The generated script MUST be valid bash (the fallback echo has nested
    # quotes/backticks — regression-guard the shell syntax).
    import subprocess as _sp
    p = tmp_path / "job.sbatch"
    p.write_text(script)
    r = _sp.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, f"generated sbatch script has a shell syntax error: {r.stderr}"


def _attach_plan(common, tmp_path, *, nudge: bool):
    return common.SubmissionPlan(
        project_root=tmp_path, project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=tmp_path / "state", profile="default", partition="day",
        account="prj1", time_minutes=60, cpus=1, memory_gb=None, gpus=0,
        gpu_type=None, apptainer_image="botainer-claude.sif",
        submission_mode="attach", existing_jobid=None, nudge_enabled=nudge,
        agent_exec_argv=_agent_argv())


def test_attach_reattaches_to_batch_screen_when_nudge_enabled(tmp_path, capsys):
    # #56 + cluster-nudge: with nudge on, `hpc attach` REATTACHES to
    # the batch agent's `screen -r botainer-<jobid>` — the SAME session nudge
    # targets — so what you see IS the running (nudge-able) agent. (_load_submit
    # is defined later in this file and returns the submit module.)
    submit = _load_submit()
    common = _load_common()
    rc = submit._do_attach(_attach_plan(common, tmp_path, nudge=True),
                           "12345", dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "screen -r botainer-12345" in out
    assert "--overlap" in out and "--pty" in out


def test_attach_falls_back_to_fresh_agent_without_nudge(tmp_path, capsys):
    # No nudge/screen keep-alive → batch agent had no TTY and exited; attach runs
    # a fresh agent (unchanged behavior).
    submit = _load_submit()
    common = _load_common()
    submit._do_attach(_attach_plan(common, tmp_path, nudge=False),
                      "12345", dry_run=True)
    out = capsys.readouterr().out
    assert "screen -r" not in out
    assert "apptainer" in out  # fresh agent argv


def test_to_apptainer_argv_returns_composed_argv_and_chokepoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Compose-at-submit: to_apptainer_argv returns the composed
    agent_exec_argv verbatim, and __post_init__ is the never-regress
    chokepoint — the §4 cage flags must be present and NO element may be
    `botainer` (the compute-node container execs the AGENT). The authoritative
    argv comes from ApptainerAdapter now; here we pin the frozen-plan guard."""
    common = _load_common()
    plan = common.SubmissionPlan(
        project_root=tmp_path,
        project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=tmp_path / "state",
        profile="default",
        partition="my-part",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="x.sif",
        submission_mode="submit",
        existing_jobid=None,
        agent_exec_argv=_agent_argv("x.sif"),
    )
    argv = plan.to_apptainer_argv()
    assert argv == list(_agent_argv("x.sif"))  # verbatim passthrough
    assert argv[:2] == ["apptainer", "exec"]
    # §4 cage flags (single-sourced from the adapter, re-asserted at the plan).
    assert "--no-privs" in argv, "sbatch cage lost NoNewPrivs (§4 regression)"
    assert argv[argv.index("--drop-caps") + 1] == "all"
    assert "botainer" not in argv and "--in-container" not in argv
    # The chokepoint REFUSES an argv that lost a §4 flag or execs botainer.
    for bad in (
        ("apptainer", "exec", "--cleanenv", "x.sif", "agent"),           # no --no-privs
        ("apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
         "--drop-caps", "all", "x.sif", "botainer"),                     # execs botainer
        # review LOW-2: basename match catches an absolute path to botainer,
        # which the old exact-element check missed.
        ("apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
         "--drop-caps", "all", "x.sif", "/usr/bin/botainer", "start"),
    ):
        with pytest.raises(SystemExit):
            common.SubmissionPlan(**{**plan.__dict__, "agent_exec_argv": bad})
    # ...but the image name containing "botainer" is NOT a false positive (the
    # image sits BEFORE the exec line; its basename is not "botainer").
    ok = common.SubmissionPlan(**{
        **plan.__dict__, "apptainer_image": "botainer-agent-claude.sif",
        "agent_exec_argv": _agent_argv("botainer-agent-claude.sif"),
    })
    assert ok.to_apptainer_argv()[-1] == "/usr/local/bin/agent-claude-entrypoint"
    # review LOW-3: session_id is interpolated unquoted into the provenance echo;
    # a shell-metachar session_id is refused at the frozen plan.
    for bad_sid in ("$(touch /tmp/pwn)", "a b", "a`whoami`"):
        with pytest.raises(SystemExit):
            common.SubmissionPlan(**{**plan.__dict__, "session_id": bad_sid})


def test_to_apptainer_argv_never_binds_munge_or_slurmd(tmp_path: Path) -> None:
    """S2 / HPC audit Finding-4 DiD: the cage must NEVER bind the Slurm auth /
    spool sockets — binding them would hand a caged agent munge creds =
    uncaged sbatch/lateral movement (Class B escape). This makes the
    currently-implicit never-bind invariant explicit + regression-proof."""
    common = _load_common()
    plan = common.SubmissionPlan(
        project_root=tmp_path,
        project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=tmp_path / "state",
        profile="default",
        partition="my-part",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="x.sif",
        submission_mode="submit",
        existing_jobid=None,
        agent_exec_argv=_agent_argv("x.sif"),
    )
    argv = plan.to_apptainer_argv()
    forbidden = ("/run/munge", "/etc/munge", "/var/spool/slurmd", "/var/run/munge")
    for a in argv:
        if a.startswith("--bind="):
            src = a[len("--bind="):].split(":", 1)[0]
            assert not any(src == f or src.startswith(f + "/") or src.rstrip("/") == f
                           for f in forbidden), f"cage must never bind Slurm auth path: {a}"


def test_detect_cluster_returns_a_profile() -> None:
    common = _load_common()
    p = common.detect_cluster()
    assert p.name
    assert isinstance(p.default_time_minutes, int)


def test_bundled_yale_profiles_load_and_autodetect_roundtrip() -> None:
    """Pin that the shipped Yale profiles parse cleanly and that EACH profile's
    own hostname_patterns autodetect back to that profile (a profile that ships
    a pattern matching nothing real is a silent autodetect failure). Synthesizes
    a concrete hostname from each pattern by swapping `*` → `1` so the test
    carries no literal site-specific hostnames itself (cluster_profiles/ is the
    permitted channel for those, tests/ is not). Cluster-ease A5 adds Bouchet;
    A6 calls out that values rot — this is the drift gate."""
    from botainer.state.cluster_profile import list_bundled
    # Keyed by every name a profile answers to. Catalogue identifiers were
    # qualified  (`us-yale-grace`), but the SITE still calls
    # itself `grace` — which is what this test, and $SLURM_CLUSTER_NAME, use.
    bundled = {}
    for p in list_bundled():
        for n in p.match_names():
            bundled[n] = p
    # All four bundled Yale profiles must be present (cluster-ease A5 adds
    # McCleary + Milgram on top of Grace + Bouchet).
    for required in ("grace", "bouchet", "mccleary", "milgram"):
        assert required in bundled, (
            f"bundled profile {required!r} missing — cluster_profiles/"
            f"us-yale-{required}.yaml went unshipped, or lost its alias."
        )
    # Compare PROFILE IDENTITY, not the key we happened to look it up under:
    # `bundled` is keyed by every alias, so the same profile appears several
    # times and its catalogue name need not equal the alias.
    for prof in set(bundled.values()):
        for pat in prof.hostname_patterns:
            host = pat.replace("*", "1")
            hits = {p.name for p in list_bundled() if p.matches_hostname(host)}
            assert prof.name in hits, (
                f"profile {prof.name!r} ships hostname_pattern {pat!r} but "
                f"autodetect on the synthesized {host!r} does NOT match it "
                f"(got {hits}); the pattern is decorative."
            )
    # Bouchet's MPI partition is the cluster's main differentiator vs other YCRC
    # clusters — pin it so a future drift gets caught.
    assert "mpi" in {p.name for p in bundled["bouchet"].partitions}
    # H200 GPU partition is documented; pin to catch a yank.
    gpu_h200 = next(
        (p for p in bundled["bouchet"].partitions if p.name == "gpu_h200"), None,
    )
    assert gpu_h200 is not None
    assert "h200" in gpu_h200.gpu_types
    # McCleary differentiator: the `transfer` partition for data movement.
    assert "transfer" in {p.name for p in bundled["mccleary"].partitions}
    # Milgram is HIPAA-restricted; verify the description carries that signal
    # so a user picking by description knows the constraint.
    assert "HIPAA" in bundled["milgram"].description, (
        "Milgram is the HIPAA-restricted cluster; if the description loses "
        "that word, the auto-pick step would silently land users without the "
        "compliance warning the agent_hints preamble depends on."
    )


def test_user_cluster_yaml_v1_schema_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cluster.yaml in cluster-profile-v1 shape (as written by
    `botainer hpc setup`) is parsed by hpc-launcher.

    Codex 45#3: the two ends used to disagree on schema; the user's
    cluster.yaml was silently ignored. This test pins the contract.
    """
    common = _load_common()
    state = tmp_path / "state"
    state.mkdir()
    cluster_yaml = state / "cluster.yaml"
    cluster_yaml.write_text(
        "version: cluster-profile-v1\n"
        "cluster:\n"
        "  name: testlab-alpha\n"
        "  hostname_patterns: [alpha*, beta*]\n"
        "slurm:\n"
        "  default_partition: gpu\n"
        "  default_account: lab-pi\n"
        "  default_time_minutes: 120\n"
        "  partitions:\n"
        "    gpu: {max_time_minutes: 1440, gpu_types: [a100]}\n"
        "    cpu: {max_time_minutes: 1440}\n"
        "scratch: {template: /scratch/$USER, auto_cleanup_days: 7}\n"
        "apptainer: {cachedir: /scratch/$USER/cache}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_BOTAINER", str(state))
    profile = common._load_user_cluster_yaml()
    assert profile is not None
    assert profile.name == "testlab-alpha"
    assert profile.default_partition == "gpu"
    assert profile.default_account == "lab-pi"
    assert profile.default_time_minutes == 120


def test_user_cluster_yaml_v1_default_partition_falls_back_to_first_nonscavenge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common = _load_common()
    state = tmp_path / "state"
    state.mkdir()
    (state / "cluster.yaml").write_text(
        "version: cluster-profile-v1\n"
        "cluster: {name: testlab-bravo}\n"
        "slurm:\n"
        "  partitions:\n"
        "    scavenge: {max_time_minutes: 1440}\n"
        "    day:      {max_time_minutes: 1440}\n"
        "    gpu:      {max_time_minutes: 1440}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_BOTAINER", str(state))
    profile = common._load_user_cluster_yaml()
    assert profile is not None
    # First non-scavenge entry wins.
    assert profile.default_partition in {"day", "gpu"}
    assert profile.default_account is None


def test_user_cluster_yaml_v1_wrong_version_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common = _load_common()
    state = tmp_path / "state"
    state.mkdir()
    (state / "cluster.yaml").write_text(
        "version: cluster-profile-v0\nname: legacy\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_BOTAINER", str(state))
    assert common._load_user_cluster_yaml() is None


def test_detect_cluster_unknown_returns_no_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On an unknown cluster with no cluster.yaml, the profile has no
    defaults. Better a clean refusal than a surprise 60-minute job."""
    common = _load_common()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("MY_BOTAINER", str(state))
    # Task #164: hostname-based fallback was removed (no institution-specific
    # defaults in dist plugin). detect_cluster now always returns the
    # 'unknown' profile when no user cluster.yaml exists — no socket
    # monkeypatch needed.
    p = common.detect_cluster()
    assert p.name == "unknown"
    assert p.default_partition is None
    assert p.default_account is None
    assert p.default_time_minutes == 0


def _load_submit():
    """submit.py does `from _common import ...` (its sibling on disk).
    Stage _common in sys.modules under both names so the relative-style
    import resolves under test."""
    common = _load_common()
    sys.modules["_common"] = common
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "hpc_launcher_submit", HPC_HOST_HELPER / "submit.py"
    )
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    sys.modules["hpc_launcher_submit"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_submit_parse_time_accepts_minutes_and_slurm_forms() -> None:
    """Codex 45#2: --time accepts plain minutes, HH:MM:SS, HH:MM, 2h, 90m."""
    submit = _load_submit()
    assert submit._parse_time_to_minutes("120") == 120
    assert submit._parse_time_to_minutes("02:00:00") == 120
    assert submit._parse_time_to_minutes("2:00") == 120
    assert submit._parse_time_to_minutes("2h") == 120
    assert submit._parse_time_to_minutes("90m") == 90
    # Rounding up for partial minutes.
    assert submit._parse_time_to_minutes("00:01:30") == 2
    with pytest.raises(Exception):
        submit._parse_time_to_minutes("0")
    with pytest.raises(Exception):
        submit._parse_time_to_minutes("garbage")


def test_submit_parse_mem_accepts_int_and_slurm_forms() -> None:
    submit = _load_submit()
    assert submit._parse_mem_to_gb("16") == 16
    assert submit._parse_mem_to_gb("16G") == 16
    assert submit._parse_mem_to_gb("16GB") == 16
    assert submit._parse_mem_to_gb("16000M") == 16
    assert submit._parse_mem_to_gb("1T") == 1024
    with pytest.raises(Exception):
        submit._parse_mem_to_gb("garbage")


def test_submit_parse_gres_accepts_gpu_forms() -> None:
    submit = _load_submit()
    assert submit._parse_gres_to_gpus("gpu:2") == (2, None)
    assert submit._parse_gres_to_gpus("gpu:a100:2") == (2, "a100")
    with pytest.raises(Exception):
        submit._parse_gres_to_gpus("cpu:2")


def _valid_plan(common, **overrides):
    base = dict(
        project_root=Path("/tmp/p"),
        project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=Path("/tmp/state"),
        profile="default",
        partition="day",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="/tmp/state/images/botainer-agent-claude.sif",
        submission_mode="submit",
        existing_jobid=None,
    )
    base.update(overrides)
    return common.SubmissionPlan(**base)


def test_submit_account_is_optional_partition_required(tmp_path: Path) -> None:
    """The Slurm account is OPTIONAL — many clusters give each user a default,
    so `_do_submit` must NOT refuse when it's unset (to_sbatch_argv omits
    --account; Slurm applies the default). A partition is still required."""
    import contextlib
    import io
    submit = _load_submit()
    common = _load_common()

    def rc(part, acct):
        plan = _valid_plan(common, project_root=tmp_path, partition=part, account=acct)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            # spec is only used on the successful-submit jobid-record path, not
            # reached under dry_run; None is fine for this partition-gate test.
            return submit._do_submit(plan, None, dry_run=True)

    assert rc("scavenge", None) == 0   # no account → submits (account optional)
    assert rc("scavenge", "myacct") == 0
    assert rc(None, "myacct") == 4   # no partition → still refused


def test_proxy_auth_refused_on_hpc_submit(tmp_path: Path) -> None:
    """Codex Priority-A HIGH: proxy auth can't withhold creds from the RW
    state/<uuid> bind on HPC (and its socket isn't routed), so submit/here MUST
    refuse it. This is the PRIMARY control; the bind-skip below is defense-in-
    depth for any path that still renders a proxy argv (e.g. dry-run preview)."""
    submit = _load_submit()
    common = _load_common()
    proxy_plan = _valid_plan(
        common, project_root=tmp_path, agent_name="claude",
        plugins_enabled=("agent-claude-proxy",),
    )
    assert submit._refuse_proxy_on_hpc(proxy_plan) == 2
    # shared/isolated variants are allowed on HPC.
    for variant in ("agent-claude-shared", "agent-claude-isolated"):
        ok_plan = _valid_plan(
            common, project_root=tmp_path, agent_name="claude",
            plugins_enabled=(variant,),
        )
        assert submit._refuse_proxy_on_hpc(ok_plan) == 0


# NOTE (compose-at-submit restructure, task #52): the former
# test_proxy_mode_withholds_real_credentials_on_sbatch and
# test_git_consent_warning_absent_vs_guarded_on_hpc tests were removed. Both
# pinned the OLD hand-built to_apptainer_argv / _print_login_node_confirmation
# posture: (a) proxy credential-bind withholding is now moot — proxy is REFUSED
# on HPC by _refuse_proxy_on_hpc (test_proxy_auth_refused_on_hpc_submit) AND by
# composition._refuse_cross_node_binds; (b) the "GIT PARTIALLY PROTECTED (HPC)"
# warning is gone because the git .git overlay now ROUTES onto the OUTER argv
# (it's a pre_session bind the adapter renders), so HPC git protection == the
# direct path. The posture is disclosed by capability_summary on the
# composed spec. New behavior is pinned by tests/unit/test_hpc_compose_at_submit.py.


@pytest.mark.parametrize(
    "hostile",
    [
        "--bind=/etc:/etc",        # the AC7 flag-injection vector
        "--fakeroot",
        "-B/host:/host",
        "img with space",          # argv tokenization
        "img\nmalicious",
        "",                        # empty
    ],
)
def test_submission_plan_rejects_flaglike_image(hostile: str) -> None:
    """AC7 completeness gap: the frozen SubmissionPlan is the
    universal chokepoint. A flag-like / whitespace apptainer image must be
    refused at construction so NO path — make_plan, the `--image` override
    merge, the auto_yes rebuild — can hand it to `apptainer exec` as a
    flag-injectable positional. `_reject_flaglike_image` raises SystemExit."""
    common = _load_common()
    with pytest.raises(SystemExit):
        _valid_plan(common, apptainer_image=hostile)


def test_image_cli_override_merge_is_validated() -> None:
    """The exact submit.py idiom: `plan.__class__(**{**plan.__dict__,
    **overrides})` with overrides['apptainer_image'] = args.image. Before
    the __post_init__ backstop this merge bypassed _reject_flaglike_image
    entirely. A legitimate override still succeeds; a flag-like one is
    refused at the merge."""
    common = _load_common()
    plan = _valid_plan(common)
    # Legitimate --image override: accepted.
    ok = plan.__class__(**{**plan.__dict__, "apptainer_image": "/data/img.sif"})
    assert ok.apptainer_image == "/data/img.sif"
    # Hostile --image override: refused at the merge (frozen-dataclass guard).
    with pytest.raises(SystemExit):
        plan.__class__(**{**plan.__dict__, "apptainer_image": "--bind=/etc"})


@pytest.mark.parametrize(
    "hostile",
    [
        "aaaa\n#SBATCH --mail-user=attacker@evil\nrm -rf $HOME # ",  # the exploit
        "x\ninjected",      # newline
        "not-a-uuid",       # non-canonical, no injection char (still refused)
        "1234",             # numeric, non-canonical
    ],
)
def test_submission_plan_rejects_non_canonical_uuid(hostile: str) -> None:
    """AC7 validator-parity audit: project_uuid is interpolated
    UNQUOTED into the generated sbatch script. A tampered .botainer/project-id
    reaching `botainer hpc submit` was a confirmed HIGH sbatch-injection.
    SubmissionPlan.__post_init__ is the standalone-mirror chokepoint: any
    non-canonical uuid is refused at construction (SystemExit)."""
    common = _load_common()
    with pytest.raises(SystemExit):
        _valid_plan(common, project_uuid=hostile)


@pytest.mark.parametrize(
    "hostile",
    [
        "claude/../../../../.ssh",          # the audit exploit: traverse to ~/.ssh
        "../../../../etc",                  # leading traversal
        "a/b",                              # any slash
        "x..y",                             # embedded '..'
        "-fakeroot",                        # leading dash → flag
        "cl aude",                          # whitespace
        "x\ninjected",                      # newline / control char
    ],
)
def test_submission_plan_rejects_traversal_agent(hostile: str) -> None:
    """AUDIT (CRITICAL C1): agent_name is read raw from the
    git-shareable .botainer/config.yaml on the `hpc submit/attach/here` path
    (which never builds a ProjectConfig, so config.py's `agent:` field_validator
    is bypassed) and interpolated into apptainer `--bind` SOURCE/TARGET paths
    (state_root/shared-auth/agent-<v>, .../data/agent-<v>/profiles/...), RW +
    mkdir, on the compute node — a path that skips validate_mount_plan's
    ~/.ssh source denylist. SubmissionPlan.__post_init__ is the standalone-mirror
    chokepoint: a '/', '..', leading '-', or whitespace agent is refused at
    construction (SystemExit) on EVERY path (make_plan, the override merge)."""
    common = _load_common()
    with pytest.raises(SystemExit):
        _valid_plan(common, agent_name=hostile)


# NOTE (compose-at-submit, task #52): test_apptainer_argv_binds_site_policy_
# when_present / _no_site_policy_bind_when_absent were removed. The site policy
# is NO LONGER bound into the container — with compose-at-submit the session is
# composed ON THE LOGIN NODE where load_site_policy reads /etc/botainer/policy.yaml
# directly, so the admin ceiling is enforced at compose (before the argv is
# rendered), not re-read by an inner launcher on the compute node. The old H4
# bind existed only because the inner compose ran under --containall inside the
# container; there is no inner compose anymore. Ceiling enforcement is covered by
# the composition policy tests.


def test_empty_agent_name_is_allowed() -> None:
    """Parity with config.py: an empty agent_name (the "no agent" project) is
    NOT rejected — the per-agent bind block is guarded by `if self.agent_name`,
    so there is no traversal sink to defend, and make_plan defaults it to ''
    when BOTAINER_PROFILE/agent are unset."""
    common = _load_common()
    plan = _valid_plan(common, agent_name="")
    assert plan.agent_name == ""


def test_agent_cli_override_merge_is_validated() -> None:
    """The submit.py override idiom must also be guarded for agent_name: a
    legitimate flat name succeeds; a traversal one is refused at the merge
    (frozen-dataclass __post_init__ guard), so no sibling path reintroduces
    the bind traversal."""
    common = _load_common()
    plan = _valid_plan(common)
    ok = plan.__class__(**{**plan.__dict__, "agent_name": "codex"})
    assert ok.agent_name == "codex"
    with pytest.raises(SystemExit):
        plan.__class__(**{**plan.__dict__, "agent_name": "x/../../../.ssh"})


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../../../tmp/PWNED",   # the review PoC: normpath-collapses out
        "../../etc",                     # leading traversal
        "a/b",                           # any slash
        "x..y",                          # embedded '..'
        "-flag",                         # leading dash
        "Default",                       # uppercase (outside charset)
        "p rofile",                      # whitespace
        "",                              # empty (no flat token)
    ],
)
def test_submission_plan_rejects_traversal_profile(hostile: str) -> None:
    """AUDIT (C1 sibling, adversarial-review BYPASS lens):
    `profile` is interpolated into the SAME RW per-agent --bind source +
    on-host mkdir/chmod as agent_name (to_apptainer_argv / prepare_host_paths)
    on the standalone host_helper path that skips validate_mount_plan. A live
    PoC showed SubmissionPlan(profile='../../../../../../tmp/PWNED') emitted
    `--bind=.../profiles/../../../../../../tmp/PWNED:...:rw` (normpath →
    /tmp/PWNED). __post_init__ now refuses any non-flat-token profile."""
    common = _load_common()
    with pytest.raises(SystemExit):
        _valid_plan(common, profile=hostile)


# NOTE (compose-at-submit, task #52): test_valid_per_agent_binds_stay_within_
# state_root was removed — to_apptainer_argv no longer BUILDS per-agent binds
# (they now come from the composed spec / agent-*-shared pre_session hook, which
# the ApptainerAdapter renders). agent_name/profile are still validated at the
# frozen plan by __post_init__ (test_submission_plan_rejects_traversal_agent /
# _rejects_traversal_profile); the bind SOURCES are validated by the composition
# credential-hook + validate_mount_plan, pinned in test_hpc_compose_at_submit.py.


def test_submission_plan_allows_canonical_and_empty_uuid() -> None:
    """A canonical UUID and the empty placeholder (BOTAINER_PROJECT_UUID
    unset → make_plan defaults to '') both construct fine — no false reject."""
    common = _load_common()
    assert _valid_plan(
        common, project_uuid="11111111-1111-1111-1111-111111111111"
    ).project_uuid == "11111111-1111-1111-1111-111111111111"
    assert _valid_plan(common, project_uuid="").project_uuid == ""


def test_render_sbatch_script_has_no_injected_uuid_lines() -> None:
    """End-to-end: even if a malicious uuid somehow reached render (it can't —
    __post_init__ refuses first), the script must never contain the injected
    directive/shell lines. Confirms construction refuses BEFORE render."""
    common = _load_common()
    payload = "aaaa\n#SBATCH --mail-user=attacker@evil\nrm -rf $HOME # "
    with pytest.raises(SystemExit):
        # Construction fails here — render is unreachable with a hostile uuid.
        _valid_plan(common, project_uuid=payload).render_sbatch_script()
    # A canonical uuid renders cleanly with no stray lines above the shebang.
    plan = _valid_plan(
        common, project_uuid="11111111-1111-1111-1111-111111111111"
    )
    script = plan.render_sbatch_script()
    assert script.startswith("#!/bin/bash\n")
    assert "rm -rf" not in script
    assert "--mail-user=attacker" not in script


def test_render_sbatch_script_rejects_newline_in_state_root() -> None:
    """AC7 review parity: state_root is interpolated unquoted into
    `#SBATCH --output=...`. It is host-private (higher trust than project-id),
    but a control char would inject the same way — render_sbatch_script rejects
    NUL/newline/CR in state_root. (Not guarded in __post_init__: a path may
    legitimately contain '/', '.', spaces; only control chars are refused.)"""
    common = _load_common()
    from pathlib import Path
    plan = _valid_plan(
        common, state_root=Path("/tmp/state\n#SBATCH --mail-user=x")
    )
    with pytest.raises(ValueError, match="state_root"):
        plan.render_sbatch_script()


def test_sbatch_jobid_regex_extracts_id() -> None:
    """Codex 45#5: parse `Submitted batch job 12345` from sbatch stdout."""
    submit = _load_submit()
    m = submit._SBATCH_JOBID_RE.search("Submitted batch job 12345\n")
    assert m is not None and m.group(1) == "12345"
    m = submit._SBATCH_JOBID_RE.search(
        "Submitted batch job 99999 on cluster grace\n"
    )
    assert m is not None and m.group(1) == "99999"
    assert submit._SBATCH_JOBID_RE.search("error: garbage") is None


def test_autodetect_env_signal_match(monkeypatch) -> None:
    """cluster-ease B2: a compute node whose bare nodename
    doesn't match any login-node hostname pattern still autodetects via
    $SLURM_CLUSTER_NAME / $LMOD_SYSHOST matching a bundled profile's name."""
    import socket as _socket
    from botainer.state import cluster_profile as cp
    # Force a hostname that matches NO bundled profile pattern.
    monkeypatch.setattr(_socket, "gethostname", lambda: "c-node-9999")
    monkeypatch.delenv("SLURM_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("LMOD_SYSHOST", raising=False)
    assert cp.autodetect() is None  # no hostname + no env → no match
    # SLURM_CLUSTER_NAME names a bundled cluster (case-insensitive).
    # The env var carries the SITE's own name. Since the catalogue
    # identifier is qualified (`us-yale-grace`, because Texas A&M also runs a
    # Grace), so the match runs through `aliases` — assert the profile we get,
    # not the string, since the identifier is ours to change and the env var
    # is not.
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "GRACE")
    got = cp.autodetect()
    assert got is not None, "site-reported name no longer autodetects"
    assert "grace" in got.match_names() and got.name.endswith("grace")
    # LMOD_SYSHOST works too.
    monkeypatch.delenv("SLURM_CLUSTER_NAME", raising=False)
    monkeypatch.setenv("LMOD_SYSHOST", "bouchet")
    got = cp.autodetect()
    assert got is not None and "bouchet" in got.match_names()


def test_start_accepts_background_alias() -> None:
    """DN-028 documents `botainer start --background`; it must be an
    accepted alias of --detach (a user copy-pasting the doc shouldn't hit
    'No such option')."""
    from click.testing import CliRunner
    from botainer.cli.start import start as start_cmd
    # --help lists the option; --background must parse (we assert it's not
    # rejected as an unknown option — exercise parsing via --help which
    # enumerates params, plus a direct parse check).
    res = CliRunner().invoke(start_cmd, ["--help"])
    assert res.exit_code == 0
    # The param exists under the --detach declaration; invoking with
    # --background on a non-project dir should fail for a REASON OTHER THAN
    # 'no such option'.
    res2 = CliRunner().invoke(start_cmd, ["--background", "--dry-run"], catch_exceptions=True)
    assert "No such option" not in res2.output


def test_has_agent_credential_shared_and_isolated(tmp_path: Path) -> None:
    """recon T-D: the pre-submit credential gate detects a credential in
    EITHER shared-auth (shared mode) OR the per-project profile dir (isolated),
    and returns False only when the user never logged in."""
    common = _load_common()
    sr = tmp_path / "sr"
    uuid = "12345678-1234-5234-9234-123456789012"
    # No credential anywhere → False.
    assert common._has_agent_credential(sr, uuid, "claude", "default") is False
    # Shared-mode credential → True.
    shared = sr / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True)
    (shared / ".credentials.json").write_text('{"claudeAiOauth":{}}')
    assert common._has_agent_credential(sr, uuid, "claude", "default") is True
    # Isolated-mode credential (codex api_key) → True.
    sr2 = tmp_path / "sr2"
    iso = sr2 / "state" / uuid / "data" / "agent-codex" / "profiles" / "default"
    iso.mkdir(parents=True)
    (iso / "api_key").write_text("sk-test")
    assert common._has_agent_credential(sr2, uuid, "codex", "default") is True
    # Empty dir (dir exists but no non-empty file) → False (not a valid login).
    sr3 = tmp_path / "sr3"
    (sr3 / "shared-auth" / "agent-claude").mkdir(parents=True)
    assert common._has_agent_credential(sr3, uuid, "claude", "default") is False
    # No agent → no requirement → True.
    assert common._has_agent_credential(sr, uuid, "", "default") is True


def test_generated_script_runs_on_a_site_that_does_not_export_SLURM_TMPDIR(tmp_path):
    """EXECUTE the script, don't parse it. `bash -n` cannot catch this class.

    THE BUG. The script runs under `set -euo pipefail` and spliced a bare
    `--env "SLURM_TMPDIR=$SLURM_TMPDIR"`. Under `set -u` a bare reference to an
    unset variable is FATAL, so on any site that does not export SLURM_TMPDIR
    the job died at that line — before the agent started — leaving a one-line
    bash error in the Slurm output file and nothing else to go on.

    SLURM_TMPDIR is not a stock Slurm export. It appears only where the site
    configures job_container/tmpfs or an equivalent TmpFS setting, so "works on
    the cluster I tried" says nothing about the next one. Every other reference
    in this repo — including the design's own smoke scripts — already used the
    `${SLURM_TMPDIR:-/tmp}` form; this single splice did not, because it was
    added for nudge (#197) when nudge was its only consumer and later became
    unconditional.

    Why this test executes rather than greps: the sibling test above runs
    `bash -n`, which validates SYNTAX and passed throughout. An unbound-variable
    abort is a RUNTIME property, so only running it finds it. This is the
    "reading is not verification" rule applied to a test.
    """
    import os
    import subprocess as _sp

    common = _load_common()
    script = _attach_plan(common, tmp_path, nudge=False).render_sbatch_script()

    # Stub the tools the script would invoke, so we exercise OUR script rather
    # than the absence of apptainer.
    binstub = tmp_path / "bin"
    binstub.mkdir()
    for tool in ("apptainer", "screen", "srun"):
        t = binstub / tool
        t.write_text("#!/bin/sh\nexit 0\n")
        t.chmod(0o755)

    p = tmp_path / "job.sbatch"
    p.write_text(script)

    env = {k: v for k, v in os.environ.items() if not k.startswith("SLURM_")}
    env["PATH"] = f"{binstub}:{env.get('PATH', '')}"
    r = _sp.run(["bash", str(p)], capture_output=True, text=True, env=env)

    assert "unbound variable" not in r.stderr, (
        "the generated sbatch script aborts under `set -u` on a site that does "
        "not export this variable — the job dies before the agent starts:\n"
        f"{r.stderr.strip()}"
    )
    assert r.returncode == 0, (
        f"generated sbatch script exited {r.returncode} with no SLURM_* in the "
        f"environment:\n{r.stderr.strip()}"
    )
