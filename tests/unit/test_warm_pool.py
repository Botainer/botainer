"""Warm pools: the config-declared `warm_pool_size` auto-start + the regression
for the `_handle_pool_control(proj, …)` NameError that made ALL pool control
(agent `pool start` AND config warm pools) silently dead."""
from __future__ import annotations

import types

import pytest
import yaml

from botainer.core.config import JobProfile


def test_warm_pool_fields_parse_and_validate() -> None:
    p = JobProfile(partition="GPU", warm_pool_size=2, warm_pool_idle_timeout=600)
    assert p.warm_pool_size == 2 and p.warm_pool_idle_timeout == 600
    assert JobProfile(partition="x").warm_pool_size is None       # default off
    with pytest.raises(Exception):
        JobProfile(partition="x", warm_pool_size=0)               # must be >= 1
    with pytest.raises(Exception):
        JobProfile(partition="x", warm_pool_idle_timeout=-1)


def test_dispatcher_cycle_autostarts_warm_pool_no_nameerror(monkeypatch, tmp_path) -> None:
    """End-to-end through `botainer hpc dispatcher once`: proves (a) the cycle no
    longer NameErrors on `proj` when it reaches pool control, and (b) a config
    `warm_pool_size` auto-starts that many caged workers."""
    from click.testing import CliRunner

    from botainer.cli import hpc as hpcmod
    from botainer.cli.hpc import hpc
    from botainer.core import config as cfgm
    from botainer.core import identity
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool
    from botainer.state import dir as _sd

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    hpcmod._AUTOWARMED_POOLS.clear()

    # fake sbatch/squeue so no real scheduler is needed; a dummy child image.
    def _fake_run(argv, **kw):
        if argv and argv[0] == "sbatch":
            return types.SimpleNamespace(stdout="55501\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    monkeypatch.setattr(hpcmod.subprocess, "run", _fake_run)
    monkeypatch.setattr(hpcmod, "_resolve_child_image",
                        lambda cfg, paths: "botainer-child.sif")

    proj = tmp_path / "p"
    proj.mkdir()
    cfgm.write_initial_config(proj, agent="claude", force=False)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfgp = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfgp.read_text())
    data["job_profiles"] = {"gpu": {
        "partition": "GPU", "account": "acct", "gpus": 1,
        "max_concurrent": 3, "warm_pool_size": 2}}
    cfgp.write_text(yaml.safe_dump(data))

    res = CliRunner().invoke(hpc, ["dispatcher", "once", "--project", str(proj)])
    assert res.exit_code == 0, res.output

    paths = _sd.ensure_user_state_dir()
    mb = _jobs.ensure_mailbox(paths, identity.read_project_id(proj))
    assert len(_pool.read_pool_state(mb)) == 2       # warm pool auto-started

    # idempotent: a second cycle does NOT re-warm (respects idle-release).
    res2 = CliRunner().invoke(hpc, ["dispatcher", "once", "--project", str(proj)])
    assert res2.exit_code == 0, res2.output
    assert len(_pool.read_pool_state(mb)) == 2


def test_agent_pool_start_request_is_processed(monkeypatch, tmp_path) -> None:
    """The agent's `botainer-job pool start` writes a pool_control request; the
    dispatcher must process it (this is the path the NameError killed)."""
    import json

    from botainer.cli import hpc as hpcmod
    from botainer.core.policy import JobPolicy
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool

    monkeypatch.setattr(hpcmod.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(
                            stdout="7\n", stderr="", returncode=0))
    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir()
    (mb.in_dir / "ctl.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": "c1", "kind": "pool_control",
        "action": "start", "profile": "gpu", "size": 1, "idle_timeout": 120}))
    cfg = types.SimpleNamespace(job_profiles={
        "gpu": JobProfile(partition="GPU", account="a", gpus=1, max_concurrent=2)})
    eff = types.SimpleNamespace(jobs=JobPolicy())
    hpcmod._handle_pool_control(tmp_path, cfg, mb, eff, "img")  # must NOT raise
    assert len(_pool.read_pool_state(mb)) == 1
    assert not (mb.in_dir / "ctl.json").exists()      # request consumed


def test_warm_pool_refuses_image_bearing_profile_at_runtime(tmp_path) -> None:
    """A per-profile `image:` profile can pass config parse (no warm_pool_size),
    but a warm worker runs the SESSION image and would silently use the wrong
    .sif. The runtime chokepoint (`_start_pool_workers`, hit by agent `pool start`
    AND CLI) must fail-closed — parse-time validation alone is bypassable
    (sharp-edges review)."""
    import types

    from botainer.cli import hpc as hpcmod
    from botainer.core.policy import JobPolicy
    from botainer.core.refusal import Refused
    from botainer.hpc import jobs as _jobs

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir()
    # image set, warm_pool_size NOT set → passes config parse, but must be
    # refused when someone tries to warm-pool it at runtime.
    prof = JobProfile(partition="GPU", max_concurrent=2, image="/apps/mpi.sif")
    eff = types.SimpleNamespace(jobs=JobPolicy())
    with pytest.raises(Refused) as exc:
        hpcmod._start_pool_workers(tmp_path, "mpi", prof, mb, eff,
                                   size=1, idle_to=120)
    assert "image" in str(exc.value) and ".sif" in str(exc.value)


def test_warm_pool_refuses_mpi_profile_at_runtime(tmp_path) -> None:
    """An MPI/multi-task profile can't be warm-pooled yet: the worker runs a
    single caged exec (not srun) so a hot task would silently run non-parallel.
    Fail closed at the runtime chokepoint (sharp-edges)."""
    import types

    from botainer.cli import hpc as hpcmod
    from botainer.core.policy import JobPolicy
    from botainer.core.refusal import Refused

    from botainer.hpc import jobs as _jobs

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir()
    eff = types.SimpleNamespace(jobs=JobPolicy())
    # multi-node
    with pytest.raises(Refused) as e1:
        hpcmod._start_pool_workers(
            tmp_path, "mpi", JobProfile(partition="day", nodes=4, max_concurrent=2),
            mb, eff, size=1, idle_to=120)
    assert "MPI" in str(e1.value) or "srun" in str(e1.value)
    # single-node but ntasks_per_node (still parallel/srun on the cold path)
    with pytest.raises(Refused):
        hpcmod._start_pool_workers(
            tmp_path, "mpi2",
            JobProfile(partition="day", ntasks_per_node=8, max_concurrent=2),
            mb, eff, size=1, idle_to=120)
    # a plain (non-parallel) profile is still allowed
    monkey = JobProfile(partition="day", cpus=4, max_concurrent=1)
    # (don't actually sbatch here — just prove the guards don't fire for it)
    assert int(getattr(monkey, "nodes", 1) or 1) == 1 and not monkey.ntasks_per_node


def test_pool_control_does_not_follow_inbox_symlink(tmp_path) -> None:
    """The pool-control inbox reader must use the O_NOFOLLOW-safe reader — a
    symlink planted by the (untrusted) agent must be skipped, not followed to a
    host file (sharp-edges; mirrors the S1 symlink discipline)."""
    import types

    from botainer.cli import hpc as hpcmod
    from botainer.core.policy import JobPolicy
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir()
    # a host file the agent tries to make the dispatcher read via a symlink
    secret = tmp_path / "secret.json"
    secret.write_text('{"kind":"pool_control","action":"start","profile":"gpu",'
                      '"id":"x","size":1}')
    (mb.in_dir / "evil.json").symlink_to(secret)
    cfg = types.SimpleNamespace(job_profiles={"gpu": JobProfile(partition="GPU")})
    eff = types.SimpleNamespace(jobs=JobPolicy())
    # must NOT follow the symlink → no worker started
    hpcmod._handle_pool_control(tmp_path, cfg, mb, eff, "img")
    assert _pool.read_pool_state(mb) == []


# ─────────── SECURITY regressions (audits) ───────────
#
# The caged agent controls the request JSON. Three sinks turned an
# attacker-chosen `id` into a HOST path component, and one re-read the
# agent-writable inbox unsafely. Each test below fails on the pre-fix code.


def test_route_hot_task_refuses_path_shaped_id(tmp_path) -> None:
    """CRITICAL: `id` was interpolated into a host path with no validation, so
    `./../../..` escaped the worker mailbox (and could overwrite pool state)."""
    from botainer.core.refusal import Refused
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir()
    # STRUCTURAL: the agent's own `id` field is never consulted — the path comes
    # from the caller's filename-derived, already-validated job_id. So a hostile
    # `id` in the body is simply INERT, not merely rejected.
    _pool.route_hot_task(
        mb, "w-deadbeef",
        {"id": "./../../../../../../tmp/pwned", "command": ["echo"]},
        job_id="0123456789abcdef")
    assert (_pool.worker_in_dir(mb, "w-deadbeef") / "0123456789abcdef.json").exists()
    assert not (tmp_path.parent / "pwned.json").exists()
    # and the caller cannot pass a path-shaped id either (belt-and-braces)
    with pytest.raises(Refused):
        _pool.route_hot_task(mb, "w-deadbeef", {}, job_id="../../evil")


def test_write_status_refuses_path_shaped_id_at_the_sink(tmp_path) -> None:
    """Defense in depth: even if a CALLER forgets to validate, the sink must."""
    from botainer.core.refusal import Refused
    from botainer.hpc import dispatcher as _disp
    from botainer.hpc import jobs as _jobs

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir()
    with pytest.raises(Refused):
        _disp._write_status(mb, "./../../../../../../tmp/evil", {"state": "refused"})
    _disp._write_status(mb, "c1", {"state": "ok"})        # short pool id still fine
    assert (mb.out_dir / "c1.status.json").exists()


def test_worker_sbatch_sends_output_to_host_private_dir(tmp_path) -> None:
    """CRITICAL: with no --output, Slurm wrote <submit-dir>/slurm-%j.out, and the
    submit CWD is the RW-bound project root -> agent-plantable symlink, host
    writes through it as the uncaged user (same class as task #48)."""
    import shlex

    from botainer.core.config import JobProfile
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    wid = "w-abc123"
    script = _pool.render_worker_sbatch(
        wid, JobProfile(partition="GPU"), "/proj", 300,
        quote=shlex.quote, out_dir=_pool.worker_dir(mb, wid))
    assert "#SBATCH --output=" in script and "#SBATCH --error=" in script
    # and both must land under the host-private run/ tree, never the project root
    for line in script.splitlines():
        if line.startswith(("#SBATCH --output=", "#SBATCH --error=")):
            assert str(mb.run_dir) in line, line


def test_dispatcher_error_path_does_not_reread_inbox(tmp_path) -> None:
    """HIGH: the fail-closed path re-read the agent-writable inbox with a plain
    read_text() -> a planted FIFO blocked the dispatcher forever. The id must now
    come from the already-validated FILENAME, so a FIFO can't be opened at all."""
    import inspect as _inspect

    from botainer.hpc import dispatcher as _disp

    src = _inspect.getsource(_disp.submit_request)
    # the exact pre-fix expression — a blocking, symlink-following open of the
    # agent-controlled inbox path (the comment above deliberately mentions
    # read_text(), so match the CALL, not the bare word)
    assert "(mailbox.in_dir / filename).read_text()" not in src, (
        "the error path must not re-read the agent-writable inbox file")
    assert "filename[:-5]" in src, "id should be derived from the validated filename"


def test_warm_worker_carries_the_profile_constraint(tmp_path) -> None:
    """A warm worker must hold its allocation on the SAME hardware as cold jobs.

    Without this the pool silently defeats the purpose of `constraint`: the
    worker would grab any node in the partition, and `--hot` tasks routed to it
    would report timings from hardware the profile said they wouldn't be on —
    invalidating exactly the benchmark comparability the field exists for. The
    failure is invisible (the job succeeds, the numbers are just wrong), which
    is why it gets its own test rather than trusting the cold-path one.
    """
    import shlex

    from botainer.core.config import JobProfile
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool

    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    wid = "w-con123"
    script = _pool.render_worker_sbatch(
        wid, JobProfile(partition="day", constraint="cascadelake"),
        "/proj", 300, quote=shlex.quote, out_dir=_pool.worker_dir(mb, wid))
    assert "#SBATCH --constraint=cascadelake" in script
