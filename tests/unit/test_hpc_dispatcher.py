"""#54 P3: dispatcher daemon core — validate → caged sbatch → out/, fail-closed."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from botainer.core.config import JobProfile
from botainer.core.policy import JobPolicy
from botainer.core.refusal import Refused
from botainer.hpc import dispatcher, jobs

VALID_ID = "abcdef0123456789"


@pytest.fixture
def mb(tmp_path: Path) -> jobs.JobMailbox:
    m = jobs.JobMailbox(
        root=tmp_path, in_dir=tmp_path / "in",
        out_dir=tmp_path / "out", run_dir=tmp_path / "run",
    )
    for d in (m.in_dir, m.out_dir, m.run_dir):
        d.mkdir()
    return m


def _profiles() -> dict[str, JobProfile]:
    return {"gpu": JobProfile(partition="gpu", time="04:00:00", cpus=8,
                              memory="32G", gpus=1, gpu_type="a100",
                              account="pi_x", max_concurrent=1)}


def _drop(mb: jobs.JobMailbox, job_id: str, profile: str, command: list[str]) -> None:
    (mb.in_dir / f"{job_id}.json").write_text(json.dumps(
        {"version": "botainer-job-v1", "id": job_id, "profile": profile,
         "command": command, "submitted_at": "now"}))


def test_valid_request_queues_a_caged_job(mb) -> None:
    _drop(mb, VALID_ID, "gpu", ["python", "train.py"])
    calls = []
    res = dispatcher.process_inbox_once(
        mb, _profiles(), JobPolicy(), "botainer-child.sif",
        sbatch=lambda p, _prof='': calls.append(p) or "59123456", now="now",
    )
    assert len(res) == 1 and res[0].state == "queued" and res[0].slurm_job_id == "59123456"
    status = json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())
    assert status["state"] == "queued" and status["slurm_job_id"] == "59123456"
    # The generated sbatch is a §4-caged apptainer exec of the pinned image…
    sb = (mb.run_dir / f"{VALID_ID}.sbatch").read_text()
    assert "apptainer exec" in sb and "--drop-caps all" in sb and "botainer-child.sif" in sb
    # …with --output in the HOST-ONLY run/ dir (S1 discipline), never the mailbox
    # the agent can write.
    assert f"--output={mb.run_dir}" in sb.replace("'", "")
    assert str(mb.in_dir) not in sb and str(mb.out_dir) not in sb


def test_unknown_profile_refused_with_reason(mb) -> None:
    _drop(mb, "1111111111111111", "nope", ["x"])
    dispatcher.process_inbox_once(mb, _profiles(), JobPolicy(), "img.sif",
                                  sbatch=lambda p, _prof='': "1")
    rec = json.loads((mb.out_dir / "1111111111111111.status.json").read_text())
    assert rec["state"] == "refused" and "profile" in rec["reason"].lower()


def test_ceiling_exceeded_refused(mb) -> None:
    _drop(mb, "2222222222222222", "gpu", ["x"])
    # policy caps GPUs at 0; the gpu profile requests 1 → refuse.
    dispatcher.process_inbox_once(mb, _profiles(), JobPolicy(max_gpus_per_job=0),
                                  "img.sif", sbatch=lambda p, _prof='': "1")
    assert json.loads((mb.out_dir / "2222222222222222.status.json").read_text())["state"] == "refused"


def test_command_that_is_a_runtime_refused(mb) -> None:
    _drop(mb, "3333333333333333", "gpu", ["docker", "run", "x"])
    dispatcher.process_inbox_once(mb, _profiles(), JobPolicy(), "img.sif",
                                  sbatch=lambda p, _prof='': "1")
    assert json.loads((mb.out_dir / "3333333333333333.status.json").read_text())["state"] == "refused"


def test_already_processed_request_skipped(mb) -> None:
    _drop(mb, VALID_ID, "gpu", ["x"])
    (mb.out_dir / f"{VALID_ID}.status.json").write_text('{"id":"'+VALID_ID+'","state":"running"}')
    calls = []
    res = dispatcher.process_inbox_once(mb, _profiles(), JobPolicy(), "img.sif",
                                        sbatch=lambda p, _prof='': calls.append(p) or "1")
    assert res == [] and calls == []  # not re-submitted


def test_symlink_in_inbox_is_refused_not_followed(mb) -> None:
    """INV-1: the inbox is agent-writable; a symlink at the request path must be
    refused (O_NOFOLLOW), not followed to some host file."""
    target = mb.root / "secret.json"
    target.write_text('{"id":"'+VALID_ID+'","profile":"gpu","command":["x"]}')
    os.symlink(target, mb.in_dir / f"{VALID_ID}.json")
    with pytest.raises(Refused):
        dispatcher.read_request_safely(mb.in_dir, f"{VALID_ID}.json")


def test_read_refuses_non_regular_file(mb) -> None:
    os.mkfifo(mb.in_dir / f"{VALID_ID}.json")  # a FIFO, not a regular file
    with pytest.raises(Refused):
        dispatcher.read_request_safely(mb.in_dir, f"{VALID_ID}.json")


def test_dispatcher_cli_group(monkeypatch, tmp_path) -> None:
    """The `botainer hpc dispatcher` group loads and `once` no-ops cleanly on a
    project with no job_profiles (no sbatch called)."""
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc
    from botainer.core import config as cfgm
    from botainer.core import identity

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    r = CliRunner()
    assert r.invoke(hpc, ["dispatcher", "--help"]).exit_code == 0
    proj = tmp_path / "p"
    proj.mkdir()
    cfgm.write_initial_config(proj, agent="claude", force=False)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    res = r.invoke(hpc, ["dispatcher", "once", "--project", str(proj)])
    assert res.exit_code == 0 and "0 newly submitted" in res.output


def test_poll_advances_states_and_copies_logs(mb) -> None:
    (mb.out_dir / f"{VALID_ID}.status.json").write_text(json.dumps(
        {"id": VALID_ID, "state": "queued", "slurm_job_id": "999"}))
    # still in the queue → queued becomes running.
    dispatcher.poll_running(mb, {"999"})
    assert json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())["state"] == "running"
    # left the queue → completed + logs copied from the host-only run/ into out/.
    (mb.run_dir / f"{VALID_ID}.out").write_text("job output here")
    dispatcher.poll_running(mb, set())
    rec = json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())
    assert rec["state"] == "completed"
    assert (mb.out_dir / f"{VALID_ID}.stdout").read_text() == "job output here"


# ── #68 MPI: multi-node/task rendering + policy cap ──


def test_mpi_profile_renders_nodes_ntasks_and_srun(mb) -> None:
    prof = JobProfile(partition="day", time="01:00:00", cpus=4, nodes=2, ntasks=8)
    script = dispatcher.render_child_sbatch(
        "a" * 16, prof, mb, ["apptainer", "exec", "img.sif", "./mpi_prog"])
    assert "#SBATCH --nodes=2" in script
    assert "#SBATCH --ntasks=8" in script
    # MPI job: the caged exec is launched under srun so SLURM spawns the ranks.
    assert "exec srun apptainer exec" in script


def test_ntasks_per_node_also_triggers_srun(mb) -> None:
    prof = JobProfile(partition="day", time="01:00:00", cpus=2, ntasks_per_node=4)
    script = dispatcher.render_child_sbatch(
        "c" * 16, prof, mb, ["apptainer", "exec", "img.sif", "prog"])
    assert "#SBATCH --ntasks-per-node=4" in script
    assert "exec srun " in script


def test_single_task_profile_uses_plain_exec_no_srun(mb) -> None:
    prof = JobProfile(partition="day", time="01:00:00", cpus=4)  # nodes=1, no ntasks
    script = dispatcher.render_child_sbatch(
        "b" * 16, prof, mb, ["apptainer", "exec", "img.sif", "prog"])
    assert "--nodes=" not in script and "srun" not in script
    assert "exec apptainer exec" in script


def test_exclusive_profile_emits_exclusive_directive(mb) -> None:
    prof = JobProfile(partition="day", time="01:00:00", cpus=4, nodes=2, exclusive=True)
    script = dispatcher.render_child_sbatch(
        "e" * 16, prof, mb, ["apptainer", "exec", "img.sif", "prog"])
    assert "#SBATCH --exclusive" in script
    # a profile WITHOUT exclusive must NOT emit it
    plain = dispatcher.render_child_sbatch(
        "f" * 16, JobProfile(partition="day"), mb,
        ["apptainer", "exec", "img.sif", "prog"])
    assert "--exclusive" not in plain


def test_per_profile_image_overrides_session_image_on_submit(mb) -> None:
    # #68 MPI: a profile with `image:` runs its jobs in THAT .sif, not the session
    # image passed to submit_request. The composed argv must reference the profile's.
    profs = {"mpi": JobProfile(partition="day", time="01:00:00", cpus=4,
                               image="/apps/mpi-agent.sif")}
    _drop(mb, "d" * 16, "mpi", ["./mpi_prog"])
    captured: dict = {}

    def _sbatch(path: Path, _prof: str = "") -> str:
        captured["script"] = path.read_text()
        return "12345"

    res = dispatcher.submit_request(
        mb, "d" * 16 + ".json", profs, JobPolicy(),
        image="/session/agent.sif", sbatch=_sbatch)
    assert res.state == "queued"
    # the profile image, not the session image, is the apptainer operand
    assert "/apps/mpi-agent.sif" in captured["script"]
    assert "/session/agent.sif" not in captured["script"]


def test_profile_image_leading_dash_rejected_as_argv_injection() -> None:
    # A leading '-' image would be parsed as an apptainer FLAG (cage-bypass vector).
    with pytest.raises(Exception):
        JobProfile(partition="day", image="--fakeroot")
    with pytest.raises(Exception):
        JobProfile(partition="day", image="img\nx.sif")  # control char


def test_child_core_binds_expose_project_dirs(tmp_path) -> None:
    # #68: every dispatched job must see the project — /workspace rw, /packages ro,
    # /scratch rw. Before this jobs got ZERO binds and couldn't see their own code.
    from botainer.state.dir import ProjectPaths

    state = tmp_path / "state"
    proj_paths = ProjectPaths(base=state / "uuid123")
    proj_paths.packages_dir.mkdir(parents=True)
    proj_paths.scratch_dir.mkdir(parents=True)
    project = tmp_path / "myproj"
    project.mkdir()
    binds = jobs.child_core_binds(project, proj_paths, state)
    by_target = {b.target: b for b in binds}
    assert by_target["/workspace"].mode.value == "rw"
    assert by_target["/packages"].mode.value == "rw"   # jobs may compile/install
    assert by_target["/scratch"].mode.value == "rw"
    assert by_target["/workspace"].source == str(project.resolve())
    # SECURITY: /workspace/.botainer must be masked so a caged job can't read/
    # write the real config.yaml (which /workspace RW would otherwise expose).
    assert "/workspace/.botainer" in by_target
    assert by_target["/workspace/.botainer"].mode.value == "null-bind"
    # the mask source is an EMPTY anchor, never the project's real .botainer
    assert "null-bind-anchor" in by_target["/workspace/.botainer"].source


def test_child_job_cannot_see_real_botainer_config(tmp_path, mb) -> None:
    # end-to-end: the rendered sbatch must bind an empty anchor over
    # /workspace/.botainer AFTER /workspace, so config.yaml is not exposed.
    from botainer.state.dir import ProjectPaths

    state = tmp_path / "state"
    pp = ProjectPaths(base=state / "u")
    pp.packages_dir.mkdir(parents=True)
    pp.scratch_dir.mkdir(parents=True)
    proj = tmp_path / "p"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text("job_profiles: {}\n")
    child_binds = jobs.child_core_binds(proj, pp, state)
    _drop(mb, "b" * 16, "gpu", ["python", "x.py"])
    cap = {}
    dispatcher.submit_request(
        mb, "b" * 16 + ".json", _profiles(), JobPolicy(), "img.sif", child_binds,
        sbatch=lambda p, _prof='': cap.setdefault("s", p.read_text()) or "1")
    script = cap["s"]
    # the mask bind targets /workspace/.botainer and does NOT source the real one
    assert ":/workspace/.botainer:ro" in script
    assert f"{proj.resolve()}/.botainer:" not in script


def test_child_cluster_contribution_binds_and_env() -> None:
    # #68 cluster layer: RO identity binds for the software tree + Lmod +
    # modulefiles, plus MODULEPATH/LMOD env so `module` works inside a job.
    from botainer.core.policy import SitePolicy

    pol = SitePolicy.model_validate({"mounts": {
        "cluster_software_roots": ["/apps/", "/opt/apps"],
        "cluster_lmod_root": "/apps/lmod/lmod",
        "cluster_modulepath_roots": ["/apps/modulefiles"]}})
    binds, env = jobs.child_cluster_contribution(pol)
    targets = [b.target for b in binds]
    assert targets == ["/apps", "/opt/apps", "/apps/lmod/lmod",
                       "/apps/modulefiles"]  # deduped, order-stable, slash-normalized
    assert all(b.source == b.target for b in binds)      # identity binds
    assert all(b.mode.value == "ro" for b in binds)      # read-only
    assert env["MODULEPATH"] == "/apps/modulefiles"
    assert env["LMOD_CMD"] == "/apps/lmod/lmod/libexec/lmod"
    assert env["BASH_ENV"] == "/apps/lmod/lmod/init/bash"


def _pp_for(tmp_path):
    from botainer.state.dir import ProjectPaths
    pp = ProjectPaths(base=tmp_path / "state" / "u")
    pp.packages_dir.mkdir(parents=True)
    pp.scratch_dir.mkdir(parents=True)
    return pp, tmp_path / "state"


def test_multinode_exclusive_no_time_warns_on_submit(mb) -> None:
    # A backfill-hostile multi-node config records a plain-English warning so a
    # slow job explains itself up front, not an error.
    import json as _json
    jid = "abcdef0123456789"
    profs = {"mpi": JobProfile(partition="day", nodes=4, ntasks=16,
                               exclusive=True)}  # multi-node + exclusive + no time
    _drop(mb, jid, "mpi", ["mpirun", "prog"])
    dispatcher.submit_request(mb, jid + ".json", profs, JobPolicy(),
                              "img.sif", sbatch=lambda p, _prof='': "1")
    rec = _json.loads((mb.out_dir / (jid + ".status.json")).read_text())
    assert rec["state"] == "queued"
    warns = " ".join(rec.get("warnings", []))
    assert "exclusive" in warns and "time limit" in warns


def test_pending_job_counts_toward_max_concurrent(mb) -> None:
    # sharp-edges #1: a PENDING job still holds a slot — it MUST count in the
    # max_concurrent throttle, else an agent floods past the cap (pend → slot
    # frees → next submits → repeat).
    import json as _json
    assert "pending" in dispatcher._ACTIVE_STATES
    (mb.out_dir / "a1.status.json").write_text(_json.dumps(
        {"id": "a1", "state": "pending", "profile": "gpu", "slurm_job_id": "1"}))
    counts = dispatcher._active_counts(mb)
    assert counts.get("gpu", 0) == 1


def test_poll_running_records_pending_reason(mb) -> None:
    # A PENDING job (in queue but state code PD) must be shown as `pending` WITH
    # its scheduler reason, not mislabeled `running` (visibility fix).
    import json as _json
    (mb.out_dir / "j1.status.json").write_text(_json.dumps(
        {"id": "j1", "state": "queued", "slurm_job_id": "99"}))
    dispatcher.poll_running(mb, {"99"}, {"99": ("PD", "PartitionNodeLimit")})
    rec = _json.loads((mb.out_dir / "j1.status.json").read_text())
    assert rec["state"] == "pending"
    assert rec["squeue_reason"] == "PartitionNodeLimit"
    # once it starts running, reason is cleared
    dispatcher.poll_running(mb, {"99"}, {"99": ("R", "None")})
    rec = _json.loads((mb.out_dir / "j1.status.json").read_text())
    assert rec["state"] == "running" and "squeue_reason" not in rec


def test_hpc_jobs_status_shows_queue_from_host(tmp_path, monkeypatch) -> None:
    # The host-side view of the dispatch queue (outside the container).
    import json as _json

    from click.testing import CliRunner

    from botainer.cli.hpc import hpc
    from botainer.state import dir as _sd
    proj = tmp_path / "p"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    from botainer.core import identity as _identity
    uid = _identity.read_project_id(proj)
    paths = _sd.ensure_user_state_dir(create_if_missing=True)
    mbx = jobs.mailbox_for(paths, uid)
    for d in (mbx.in_dir, mbx.out_dir, mbx.run_dir):
        d.mkdir(parents=True, exist_ok=True)
    (mbx.out_dir / "j1.status.json").write_text(_json.dumps(
        {"id": "job-1", "state": "pending", "profile": "gpu",
         "slurm_job_id": "555", "squeue_reason": "Resources",
         "submitted_at": "2026-07-09T00:00:00Z"}))
    res = CliRunner().invoke(hpc, ["jobs-status", "--project", str(proj)])
    assert res.exit_code == 0
    # The filename owns the mailbox identity, even if the body disagrees.
    assert "j1" in res.output and "pending" in res.output
    assert "job-1" not in res.output
    assert "gpu" in res.output and "Resources" in res.output


def test_hpc_jobs_explain_shows_the_job_surface(tmp_path, monkeypatch) -> None:
    # Transparency: `jobs-explain` composes the caged job (no submit) and shows
    # binds/modules/resources + what's frozen/not-exposed.
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc
    proj = tmp_path / "p"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n")
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\njob_profiles:\n  gpu:\n    partition: GPU\n"
        "    cpus: 8\n    gpus: 1\n    modules: [cuda/12.4]\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    res = CliRunner().invoke(hpc, ["jobs-explain", "gpu", "--project", str(proj)])
    assert res.exit_code == 0
    assert "/workspace" in res.output and "rw" in res.output
    assert "cuda/12.4" in res.output           # per-profile module
    assert "masked" in res.output              # .botainer frozen
    assert "NOT exposed" in res.output and "credentials" in res.output
    # unknown profile → clean refusal
    r2 = CliRunner().invoke(hpc, ["jobs-explain", "nope", "--project", str(proj)])
    assert r2.exit_code == 2


def test_child_git_frozen_ro(tmp_path) -> None:
    # sharp-edges F2/F3: a caged job with /workspace RW must NOT be able to plant
    # a host-executed git hook or poison config — the whole .git is frozen RO.
    pp, state = _pp_for(tmp_path)
    proj = tmp_path / "p"
    (proj / ".git" / "hooks").mkdir(parents=True)
    (proj / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    by_t = {b.target: b for b in jobs.child_core_binds(proj, pp, state)}
    assert by_t["/workspace/.git"].mode.value == "ro"


@pytest.mark.parametrize("cfg", [
    "[core]\n\thooksPath = /tmp/evil\n",          # sectioned core.hooksPath
    "[core]\n\tpager = evil-payload\n",           # F1: was MISSED by substring check
    '[credential "https://x"]\n\thelper = !sh\n',  # subsection key
    "[include]\n\tpath = /tmp/evil\n",            # F1: external-config indirection
    '[filter "lfs"]\n\tclean = evil\n',
])
def test_child_git_dangerous_config_refused(tmp_path, cfg) -> None:
    # F1: the shared section-aware scanner catches sectioned + include + subsection
    # keys the old substring check silently missed.
    pp, state = _pp_for(tmp_path)
    proj = tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    (proj / ".git" / "config").write_text(cfg)
    with pytest.raises(Refused):
        jobs.child_core_binds(proj, pp, state)


def test_child_git_gitdir_pointer_refused(tmp_path) -> None:
    # F2: .git as a FILE (worktree/submodule gitdir pointer) → fail CLOSED.
    pp, state = _pp_for(tmp_path)
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".git").write_text("gitdir: /elsewhere/realgit\n")  # pointer, not a dir
    with pytest.raises(Refused):
        jobs.child_core_binds(proj, pp, state)


def test_child_git_safe_fsmonitor_boolean_ok(tmp_path) -> None:
    # A boolean core.fsmonitor is safe and must NOT be refused (parity w/ plugin).
    pp, state = _pp_for(tmp_path)
    proj = tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    (proj / ".git" / "config").write_text("[core]\n\tfsmonitor = true\n")
    jobs.child_core_binds(proj, pp, state)  # no raise


def test_child_cluster_contribution_empty_is_off() -> None:
    # No site config → feature OFF (safe default), not a crash.
    from botainer.core.policy import SitePolicy

    binds, env = jobs.child_cluster_contribution(SitePolicy())
    assert binds == () and env == {}


def test_child_cluster_env_rendered_as_apptainer_env_flags() -> None:
    # The module env must reach the caged job as `--env KEY=VAL` (survives
    # --cleanenv), never as a shell mutation of the workload argv.
    from botainer.core.policy import SitePolicy

    pol = SitePolicy.model_validate({"mounts": {
        "cluster_lmod_root": "/apps/lmod/lmod",
        "cluster_modulepath_roots": ["/apps/modulefiles"]}})
    binds, env = jobs.child_cluster_contribution(pol)
    argv = jobs.compose_child_job_argv("img.sif", ("python", "t.py"), binds, env)
    assert "--env" in argv
    assert "MODULEPATH=/apps/modulefiles" in argv
    # env flags precede the image; workload stays a clean argv tail
    assert argv[-2:] == ["python", "t.py"]


def test_child_env_rejects_control_char_value() -> None:
    with pytest.raises(Refused):
        jobs.compose_child_job_argv("img.sif", ("bash",), (),
                                    {"MODULEPATH": "/apps\n--evil"})


def test_preload_modules_wraps_argv_verbatim() -> None:
    # #68: `module load` runs before the workload, but the agent's command is
    # preserved as positional params (exec "$@") — NOT interpolated into shell.
    env = {"BASH_ENV": "/apps/lmod/lmod/init/bash"}
    argv = jobs.compose_child_job_argv(
        "img.sif", ("python", "train.py; rm -rf /"), (), env,
        preload_modules=("cuda/12.3", "openmpi/4.1"))
    tail = argv[argv.index("img.sif") + 1:]
    assert tail == ["bash", "-c", 'module load cuda/12.3 openmpi/4.1 && exec "$@"',
                    "bash", "python", "train.py; rm -rf /"]
    # the malicious-looking arg stays a single argv token (not shell-reparsed)
    assert tail[-1] == "train.py; rm -rf /"


@pytest.mark.parametrize("bad", ["x\n", "x\ntouch", "a b", "cuda;rm", "\tx"])
def test_preload_modules_rejects_injection_tokens(bad) -> None:
    # sharp-edges F1: `^…$` admitted a trailing \n (Python quirk); a module name
    # ending in newline would break out of the `module load` preamble line. The
    # \A…\Z + control-char guard must reject any such token at compose.
    env = {"BASH_ENV": "/apps/lmod/lmod/init/bash"}
    with pytest.raises(Refused):
        jobs.compose_child_job_argv("img.sif", ("python", "x.py"), (), env,
                                    preload_modules=(bad,))


@pytest.mark.parametrize("bad", ["x\n", "x\ntouch /tmp/p", "a b"])
def test_profile_modules_rejects_newline_injection(bad) -> None:
    # Same F1 defense at config parse (belt-and-suspenders with compose).
    from botainer.core.config import JobProfile
    with pytest.raises(Exception):
        JobProfile(partition="gpu", modules=[bad])


def test_preload_modules_fail_closed_without_module_system() -> None:
    # A profile that lists modules but no cluster module system is exposed must
    # be refused (not silently run `module load` where module is undefined).
    with pytest.raises(Refused):
        jobs.compose_child_job_argv("img.sif", ("python", "x.py"), (), {},
                                    preload_modules=("cuda",))


def test_profile_modules_charset_validated() -> None:
    from botainer.core.config import JobProfile
    JobProfile(partition="gpu", modules=["cuda/12.3", "gcc/13"])  # ok
    with pytest.raises(Exception):
        JobProfile(partition="gpu", modules=["cuda; rm -rf /"])   # shell injection
    with pytest.raises(Exception):
        JobProfile(partition="gpu", modules=["a b"])              # space


def test_child_core_binds_symlink_escape_refused(tmp_path) -> None:
    # A swapped packages symlink pointing outside the state dir must be refused.
    from botainer.state.dir import ProjectPaths

    state = tmp_path / "state"
    base = state / "uuid123"
    base.mkdir(parents=True)
    (tmp_path / "evil").mkdir()
    (base / "packages").symlink_to(tmp_path / "evil")  # escapes state dir
    (base / "scratch").mkdir()
    with pytest.raises(Refused):
        jobs.child_core_binds(tmp_path / "myproj", ProjectPaths(base=base), state)


def test_submitted_job_script_binds_workspace(tmp_path, mb) -> None:
    # end-to-end: a queued child job's sbatch script must --bind /workspace.
    from botainer.state.dir import ProjectPaths

    state = tmp_path / "state"
    pp = ProjectPaths(base=state / "u")
    pp.packages_dir.mkdir(parents=True)
    pp.scratch_dir.mkdir(parents=True)
    proj = tmp_path / "p"
    proj.mkdir()
    child_binds = jobs.child_core_binds(proj, pp, state)
    _drop(mb, "a" * 16, "gpu", ["python", "train.py"])
    captured = {}
    dispatcher.submit_request(
        mb, "a" * 16 + ".json", _profiles(), JobPolicy(), "img.sif", child_binds,
        sbatch=lambda p, _prof='': captured.setdefault("s", p.read_text()) or "1")
    assert f"--bind {proj.resolve()}:/workspace" in captured["s"]
    assert "/packages" in captured["s"]  # bound (rw now — jobs may build)


def test_image_plus_warm_pool_rejected() -> None:
    # A warm worker runs the session image; combining with a per-profile image
    # would silently run warm tasks in the wrong .sif — refuse the combo.
    with pytest.raises(Exception):
        JobProfile(partition="day", image="/apps/mpi.sif", warm_pool_size=2)


def test_max_nodes_per_job_ceiling() -> None:
    from botainer.core.policy import JobPolicy, check_profile_against_ceiling
    with pytest.raises(Refused):
        check_profile_against_ceiling("mpi", "day", None, 0, 1,
                                      JobPolicy(max_nodes_per_job=2), nodes=4)
    # within the cap → no raise
    check_profile_against_ceiling("mpi", "day", None, 0, 1,
                                  JobPolicy(max_nodes_per_job=4), nodes=4)


def test_site_caps_on_cpu_mem_time() -> None:
    """jobs v2 (audit follow-up): the resolved cpus/mem/time are bounded by the
    site JobPolicy, not just the profile max — the 'agent ≤ profile ≤ site' claim
    now holds for every dimension. None arg or None cap = skip."""
    from botainer.core.policy import JobPolicy, check_profile_against_ceiling
    base = dict(profile_name="p", partition="day", account=None, gpus=0,
                max_concurrent=1)
    # cpus over the cap → refuse; within → ok.
    with pytest.raises(Refused):
        check_profile_against_ceiling(**base, jobs_policy=JobPolicy(max_cpus_per_job=8),
                                      cpus=16)
    check_profile_against_ceiling(**base, jobs_policy=JobPolicy(max_cpus_per_job=16),
                                  cpus=16)
    # memory (MB) over the cap → refuse.
    with pytest.raises(Refused):
        check_profile_against_ceiling(**base, jobs_policy=JobPolicy(max_mem_mb_per_job=1024),
                                      mem_mb=4096)
    # walltime (seconds) over the cap → refuse.
    with pytest.raises(Refused):
        check_profile_against_ceiling(**base,
                                      jobs_policy=JobPolicy(max_time_seconds_per_job=3600),
                                      time_seconds=7200)
    # None arg (caller didn't resolve the dim) never trips a set cap.
    check_profile_against_ceiling(**base, jobs_policy=JobPolicy(max_cpus_per_job=1),
                                  cpus=None)


def test_submit_refuses_resolved_cpus_over_site_cap(mb) -> None:
    """End-to-end at the dispatcher: an override the profile allows but the SITE
    policy forbids is refused (resolved cpus re-checked against the ceiling)."""
    profs = {"big": JobProfile(partition="day", cpus=4, max_cpus=32,
                               memory="16G", max_memory="128G")}
    (mb.in_dir / f"{VALID_ID}.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": VALID_ID, "profile": "big",
        "command": ["python", "x.py"], "resources": {"cpus": 32, "memory": "128G"}}))
    # Profile allows 32 cpus, but the site caps at 8 → refuse at submit.
    dispatcher.process_inbox_once(mb, profs, JobPolicy(max_cpus_per_job=8),
                                  "botainer-child.sif", sbatch=lambda p, _prof='': "7", now="now")
    st = json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())
    assert st["state"] == "refused" and "cpus" in st["reason"] and "site policy" in st["reason"]


# ── #68 hot routing: dispatcher → idle warm worker (else cold fallback) ──


def test_hot_request_routes_to_an_idle_worker(mb) -> None:
    import time as _time

    from botainer.hpc import pool as _pool
    wid = _pool.new_worker_id("abcdef012345")
    _pool.ensure_worker_dirs(mb, wid)
    _pool.write_pool_state(mb, [_pool.WorkerRec(wid, "999", "gpu", 300, "now")])
    _pool.write_beat(mb, wid, "idle", _time.time())
    _drop(mb, VALID_ID, "gpu", ["python", "train.py"])
    # add the hot flag to the dropped request
    reqp = mb.in_dir / f"{VALID_ID}.json"
    r = json.loads(reqp.read_text()); r["hot"] = True
    reqp.write_text(json.dumps(r))
    submitted = []
    res = dispatcher.process_inbox_once(
        mb, _profiles(), JobPolicy(), "botainer-child.sif",
        sbatch=lambda p, _prof='': submitted.append(p) or "1", now="now")
    assert submitted == []                      # NOT cold-submitted
    assert res and res[0].state == "assigned"
    st = json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())
    assert st["state"] == "assigned" and st["worker"] == wid
    # task landed in the worker's inbox, and left the main inbox
    assert any(_pool.worker_in_dir(mb, wid).glob("*.json"))
    assert not reqp.exists()


def test_hot_request_falls_back_to_cold_when_no_idle_worker(mb) -> None:
    _drop(mb, VALID_ID, "gpu", ["python", "train.py"])
    reqp = mb.in_dir / f"{VALID_ID}.json"
    r = json.loads(reqp.read_text()); r["hot"] = True
    reqp.write_text(json.dumps(r))
    submitted = []
    res = dispatcher.process_inbox_once(
        mb, _profiles(), JobPolicy(), "botainer-child.sif",
        sbatch=lambda p, _prof='': submitted.append(p) or "42", now="now")
    assert len(submitted) == 1                  # cold sbatch fallback
    assert res and res[0].state == "queued"


# ── jobs v2: per-request resource overrides (bounded by profile max) ──


def test_submit_with_override_renders_requested_value(mb) -> None:
    profs = {"big": JobProfile(partition="day", cpus=4, max_cpus=32,
                               memory="16G", max_memory="128G")}
    (mb.in_dir / f"{VALID_ID}.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": VALID_ID, "profile": "big",
        "command": ["python", "x.py"], "resources": {"cpus": 16, "memory": "64G"}}))
    dispatcher.process_inbox_once(mb, profs, JobPolicy(), "botainer-child.sif",
                                  sbatch=lambda p, _prof='': "7", now="now")
    sb = (mb.run_dir / f"{VALID_ID}.sbatch").read_text()
    assert "--cpus-per-task=16" in sb and "--mem=64G" in sb


def test_submit_override_beyond_max_refused(mb) -> None:
    profs = {"big": JobProfile(partition="day", cpus=4, max_cpus=8)}
    (mb.in_dir / f"{VALID_ID}.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": VALID_ID, "profile": "big",
        "command": ["python", "x.py"], "resources": {"cpus": 32}}))
    dispatcher.process_inbox_once(mb, profs, JobPolicy(), "botainer-child.sif",
                                  sbatch=lambda p, _prof='': "7", now="now")
    st = json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())
    assert st["state"] == "refused" and "cpus" in st["reason"]


def test_override_on_fixed_resource_refused(mb) -> None:
    profs = {"fixed": JobProfile(partition="day", cpus=4)}  # no max_cpus
    (mb.in_dir / f"{VALID_ID}.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": VALID_ID, "profile": "fixed",
        "command": ["python", "x.py"], "resources": {"cpus": 8}}))
    dispatcher.process_inbox_once(mb, profs, JobPolicy(), "botainer-child.sif",
                                  sbatch=lambda p, _prof='': "7", now="now")
    st = json.loads((mb.out_dir / f"{VALID_ID}.status.json").read_text())
    assert st["state"] == "refused" and "fixed" in st["reason"]


def test_poison_typed_json_override_refuses_not_crashes(mb) -> None:
    """A caged agent writing raw JSON straight into in/ can make memory/time a
    NON-string. Before the fix that raised AttributeError past every catcher, so
    the dispatcher died and the request (never given a status → never processed)
    crash-looped forever. Now it must get a `refused` status and be skipped on
    the next cycle (audit HIGH)."""
    profs = {"big": JobProfile(partition="day", cpus=4, max_cpus=32,
                               memory="16G", max_memory="128G",
                               time="01:00:00", max_time="04:00:00")}
    for jid, res in (("0000000000000001", {"memory": 1}),
                     ("0000000000000002", {"memory": [1]}),
                     ("0000000000000003", {"time": 1}),
                     ("0000000000000004", {"time": {"a": 1}})):
        (mb.in_dir / f"{jid}.json").write_text(json.dumps({
            "version": "botainer-job-v1", "id": jid, "profile": "big",
            "command": ["python", "x.py"], "resources": res}))
    # Must NOT raise; every poison request gets a refused status.
    dispatcher.process_inbox_once(mb, profs, JobPolicy(), "botainer-child.sif",
                                  sbatch=lambda p, _prof='': "7", now="now")
    for jid in ("0000000000000001", "0000000000000002",
                "0000000000000003", "0000000000000004"):
        st = json.loads((mb.out_dir / f"{jid}.status.json").read_text())
        assert st["state"] == "refused", jid
    # And a second cycle skips them (already processed → no replay/re-crash).
    res2 = dispatcher.process_inbox_once(mb, profs, JobPolicy(),
                                         "botainer-child.sif", sbatch=lambda p, _prof='': "7")
    assert res2 == []


def test_profiles_manifest_scrubs_control_chars_in_notes() -> None:
    """notes/description come from untrusted config; write_profiles_manifest must
    strip terminal-control/ANSI chars before the agent reads them via
    `botainer-job profiles` (audit MEDIUM)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        m = jobs.JobMailbox(root=Path(td), in_dir=Path(td) / "in",
                            out_dir=Path(td) / "out", run_dir=Path(td) / "run")
        for d in (m.in_dir, m.out_dir, m.run_dir):
            d.mkdir()
        profs = {"x": JobProfile(
            partition="day",
            description="ok\x1b[31mred\x1b[0m",
            notes="line1\x1b]0;pwned\x07\nline2\x00nul")}
        jobs.write_profiles_manifest(m, profs)
        man = json.loads((m.out_dir / "profiles.json").read_text())
        note = man["profiles"]["x"]["notes"]
        desc = man["profiles"]["x"]["description"]
        assert "\x1b" not in note and "\x07" not in note and "\x00" not in note
        assert "\x1b" not in desc
        assert "line1" in note and "line2" in note  # content preserved


def test_max_concurrent_is_enforced_with_defer(mb) -> None:
    """max_concurrent must actually THROTTLE submits (was declared-only): at cap,
    extra requests are DEFERRED and picked up when a slot frees."""
    profs = {"cpu": JobProfile(partition="day", account="a", cpus=2, max_concurrent=1)}
    ids = ["bbbbbbbbbbbbbbb1", "bbbbbbbbbbbbbbb2", "bbbbbbbbbbbbbbb3"]
    for i in ids:
        _drop(mb, i, "cpu", ["echo", "hi"])
    n = [0]
    def _sb(p, _prof=""):
        n[0] += 1
        return f"600{n[0]}"
    res = dispatcher.process_inbox_once(mb, profs, JobPolicy(), "img.sif",
                                        sbatch=_sb, now="t0")
    assert n[0] == 1                                      # only 1 sbatch (cap=1)
    assert sorted(r.state for r in res) == ["deferred", "deferred", "queued"]
    # free the slot → a deferred request now submits
    q = next(i for i in ids if json.loads(
        (mb.out_dir / f"{i}.status.json").read_text())["state"] == "queued")
    rec = json.loads((mb.out_dir / f"{q}.status.json").read_text())
    rec["state"] = "completed"
    (mb.out_dir / f"{q}.status.json").write_text(json.dumps(rec))
    dispatcher.process_inbox_once(mb, profs, JobPolicy(), "img.sif", sbatch=_sb, now="t1")
    assert n[0] == 2                                      # one more submitted
    assert sum(1 for i in ids if json.loads(
        (mb.out_dir / f"{i}.status.json").read_text())["state"] == "deferred") == 1


# ── constraint: operator-fixed hardware pinning (user request) ──


def test_constraint_renders_and_is_not_agent_requestable(mb) -> None:
    """`constraint` pins node features (e.g. CPU generation) for reproducible runs.

    Partitions can mix processor generations. A Slurm constraint narrows
    the hardware features requested for comparable runs.

    The trust shape is the point: this value goes into an `#SBATCH` directive, so
    it must never be agent-influenced. The protection is STRUCTURAL — there is no
    `max_constraint` and no `botainer-job submit --constraint`, so no agent
    request can reach the directive. This test pins BOTH halves: it renders, and
    the override door does not exist.
    """
    prof = JobProfile(partition="day", time="01:00:00", cpus=4,
                      constraint="cascadelake")
    script = dispatcher.render_child_sbatch(
        "d" * 16, prof, mb, ["apptainer", "exec", "img.sif", "prog"])
    assert "#SBATCH --constraint=cascadelake" in script

    # No agent-facing override for this field, unlike cpus/memory/gpus/nodes/time.
    assert not hasattr(prof, "max_constraint"), (
        "adding max_constraint would make an sbatch directive agent-steerable")

    # Slurm's OR operator survives the charset validator.
    prof2 = JobProfile(partition="day", time="01:00:00",
                       constraint="icelake|cascadelake")
    assert "#SBATCH --constraint=icelake|cascadelake" in dispatcher.render_child_sbatch(
        "e" * 16, prof2, mb, ["apptainer", "exec", "img.sif", "prog"])


def test_constraint_absent_emits_no_directive(mb) -> None:
    prof = JobProfile(partition="day", time="01:00:00", cpus=4)
    script = dispatcher.render_child_sbatch(
        "f" * 16, prof, mb, ["apptainer", "exec", "img.sif", "prog"])
    assert "--constraint" not in script


def test_constraint_refuses_sbatch_injection() -> None:
    """Backs up the operator's own typo / a config.yaml edited by someone else.

    Not the primary boundary (that's structural — see above), but a directive
    is a directive: anything that could terminate the line or start a shell
    command must not parse.
    """
    import pytest as _pytest
    for hostile in ("a b", "x$(id)", "a;b", "day\n#SBATCH --partition=gpu", "a[2]"):
        with _pytest.raises(Exception) as exc:
            JobProfile(partition="day", constraint=hostile)
        # Assert the REFUSAL, not merely that something raised: a bare
        # raises(Exception) stays green if the validator is deleted and pydantic
        # happens to complain about something else.
        assert "sbatch-injection defense" in str(exc.value), (
            f"{hostile!r} was rejected, but not by the constraint validator: "
            f"{exc.value}")

    # ...and a legitimate value with Slurm's operators is NOT refused, so the
    # charset isn't just rejecting everything.
    assert JobProfile(partition="day",
                      constraint="icelake|cascadelake").constraint == "icelake|cascadelake"
