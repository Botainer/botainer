"""A warm worker that never started told nobody why, and the commands were worse.

MEASURED SHAPE, in two rounds. What the first review found: a worker whose
image cannot be resolved dies before its first heartbeat, and its three traces were
each unreachable —

    run/pool/<id>/worker.err   the real reason, read by NO command
    out/pool.json (the AGENT)  "state": "?"
    botainer hpc pool status   state=?      <-- WRONG, and the refuting review of
                                                the first fix measured the truth:

`pool status` did not print `state=?` in that case at all. It printed
`Error: child-job image not found for 'agent-claude'` and exited 1, because
`_pool_ctx` resolved a child image that `pool status` then discarded — so in the
one failure this whole diagnosis exists for, the command died before printing any
worker line. `pool stop` died the same way, which left a user whose image was
broken unable to release live GPU allocations through the product at all.

Slurm writes that stderr file because botainer asks it to, into the host-private
pool tree — deliberately, so a caged agent cannot pre-plant it (the #48 class). And
then nothing ever looked at it.

"?" was the wrong answer three ways: it did not distinguish NEVER STARTED from a
heartbeat that WENT STALE, and it did not distinguish either from the DESIGNED end
of a warm worker's life. A worker that idles out writes `exiting` and stops; the
first version of this fix aged that into "stale" and sent the user to `squeue` for
a finished job — the permanent steady state of every warm pool that goes quiet.

WHY THIS FILE EXISTS AT ALL: the warm-pool path was reached by NO test. The one
end-to-end dispatcher test monkeypatches the image resolver away, so `_pool_ctx`
had never been exercised. Every test here drives the real functions against a real
mailbox on disk, and the two command tests deliberately do NOT create a `.sif`.
"""
from __future__ import annotations

import json

import pytest

from botainer.hpc import jobs as _jobs
from botainer.hpc import pool as _pool
from botainer.state import dir as state_dir


@pytest.fixture
def mb(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    return _jobs.ensure_mailbox(paths, "11111111-2222-3333-4444-555555555555")


def _rec(worker_id="w0000001", profile="gpu"):
    return _pool.WorkerRec(worker_id=worker_id, slurm_job_id="55501",
                           profile=profile, idle_timeout=300, started_at="t")


def _project(tmp_path, monkeypatch, *, profile_extra=None):
    """A real project with a real project-id and a real mailbox — NO .sif.

    The missing image is the point: it is the commonest reason a worker dies before
    its first heartbeat, and it is the state in which both of these commands used to
    abort. A fixture that wrote a fake `.sif` (mine did) removes the failure the
    test claims to cover.
    """
    import yaml

    from botainer.core import config as cfgm
    from botainer.core import identity

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    cfgm.write_initial_config(proj, agent="claude", force=True)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    prof = {"partition": "GPU", "account": "acct", "max_concurrent": 2}
    prof.update(profile_extra or {})
    data["job_profiles"] = {"gpu": prof}
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    mailbox = _jobs.ensure_mailbox(paths, identity.read_project_id(proj))
    return proj, mailbox


def test_a_worker_that_NEVER_BEAT_is_not_reported_as_a_question_mark(mb):
    """THE DEFECT. No heartbeat file at all means it never ran, and that is sayable."""
    rec = _rec()
    _pool.ensure_worker_dirs(mb, rec.worker_id)

    diag = _pool.worker_diagnosis(mb, rec)

    assert diag["state"] == "never-started", diag
    assert diag["age_s"] is None, (
        f"an age was reported for a worker that never beat: {diag}")


def test_its_OWN_stderr_becomes_the_reason(mb):
    """The trace that existed and was unreachable.

    The tail, not the head: a Python traceback puts the useful line last, and the
    useful line here is the one naming the image.
    """
    rec = _rec()
    wd = _pool.ensure_worker_dirs(mb, rec.worker_id)
    (wd / "worker.err").write_text(
        "some noise\n"
        "Traceback (most recent call last):\n"
        "Error: `image:` in config: child-job image not found for 'agent-claude'\n")

    diag = _pool.worker_diagnosis(mb, rec)

    assert diag["state"] == "never-started"
    assert "child-job image not found" in diag["reason"], diag
    assert "agent-claude" in diag["reason"], diag


def test_a_STALE_heartbeat_is_distinguished_from_never_having_one(mb):
    """Two states, two causes, and "?" used to be both."""
    rec = _rec()
    _pool.write_beat(mb, rec.worker_id, "idle", 1000.0)

    fresh = _pool.worker_diagnosis(mb, rec, now=1000.0 + 10)
    assert fresh["state"] == "idle", fresh
    assert fresh["reason"] == "", (
        "a LIVE worker's stderr was read as a diagnosis; it is not one")

    stale = _pool.worker_diagnosis(mb, rec, now=1000.0 + 91)
    assert stale["state"] == "stale", stale
    assert stale["age_s"] == 91.0, stale


def test_a_worker_that_SAID_it_was_exiting_is_not_a_fault_at_any_age(mb):
    """THE REGRESSION THE REFUTING REVIEW CAUGHT IN MY OWN FIRST FIX.

    `exiting` is what a worker writes when it self-releases on its idle timeout —
    the DESIGNED end of a warm worker's life, and therefore the permanent steady
    state of every pool that has been quiet for longer than the timeout (default
    300 s; the shipped example uses 600). Aging that into "stale" reported the
    normal case as a malfunction and pointed the user at `squeue` for a job that
    had finished. A terminal word is the worker's own report and outranks the clock.
    """
    for word in ("exiting", "stopped"):
        rec = _rec()
        _pool.write_beat(mb, rec.worker_id, word, 1000.0)

        diag = _pool.worker_diagnosis(mb, rec, now=1000.0 + 400)

        assert diag["state"] == word, diag
        assert diag["terminal"] is True, (
            f"{word!r} is the worker reporting its own exit, not a fault: {diag}")
        assert diag["reason"] == "", (
            "a worker that told us it was finishing has already given its reason")
        assert diag["age_s"] == 400.0, diag


def test_a_worker_that_died_while_BUSY_is_distinguishable(mb):
    """The one case where a second thing is also broken.

    `idle`→dead loses nothing. `busy`→dead means a hot task was running when the
    worker stopped beating, so that task has nobody left to write its result —
    collapsing both into "stale" throws away the only signal that a job is orphaned.
    """
    rec = _rec()
    _pool.write_beat(mb, rec.worker_id, "busy", 1000.0)

    diag = _pool.worker_diagnosis(mb, rec, now=1000.0 + 200)

    assert diag["state"] == "stale", diag
    assert diag["last_state"] == "busy", (
        f"the worker's own last word was dropped, so an orphaned hot task is "
        f"indistinguishable from an idle worker going away: {diag}")


def test_a_beat_that_will_not_parse_is_not_called_never_started(mb):
    """It DID start — it wrote the file. Saying "never-started" there is a lie, and
    the two have different fixes (a broken submit vs. a corrupted/truncated write,
    e.g. a full filesystem)."""
    rec = _rec()
    wd = _pool.ensure_worker_dirs(mb, rec.worker_id)
    (wd / "beat.json").write_text("{not json")

    diag = _pool.worker_diagnosis(mb, rec)

    assert diag["state"] == "beat-unreadable", diag


def test_a_NON_NUMERIC_timestamp_does_not_raise(mb):
    """This runs inside the dispatcher's publish step and inside the router. A
    `float()` on a hostile-shaped value there would take out the whole cycle, and
    the worker tree is host-private but not immutable."""
    rec = _rec()
    wd = _pool.ensure_worker_dirs(mb, rec.worker_id)
    (wd / "beat.json").write_text(json.dumps({"state": "idle", "ts": "soon"}))

    diag = _pool.worker_diagnosis(mb, rec, now=1000.0)

    assert diag["state"] == "stale", diag
    assert diag["age_s"] is None, (
        f"an age computed from an unparseable timestamp is worse than none: {diag}")


def test_the_router_CANNOT_disagree_with_what_pool_status_prints(mb):
    """Pinned as one decision, not two numbers that happen to match today.

    If `find_idle_worker` stops routing to a worker at 90 s and `pool status` keeps
    calling it idle at 120 s, the user is told the pool is healthy while the
    dispatcher silently falls back to cold submits — the "--hot became a cold queue
    submit and nothing recorded it" defect, one layer up.

    My first version of this test probed ONE age (95 s) against two default
    literals, and the refuting review showed the blind band: with the router at 60 s
    and the status at 90 s, both assertions still held at 95 s while the surfaces
    disagreed at 70 s. So this drives several ages and asserts the EQUIVALENCE, and
    the implementation now derives routability from the same call.
    """
    rec = _rec()
    _pool.write_pool_state(mb, [rec])     # the router only sees recorded workers
    _pool.ensure_worker_dirs(mb, rec.worker_id)
    _pool.write_beat(mb, rec.worker_id, "idle", 1000.0)

    for offset in (1, 45, 89.5, 90.5, 120, 3600):
        now = 1000.0 + offset
        routable = _pool.find_idle_worker(mb, "gpu", now=now) is not None
        says_idle = _pool.worker_diagnosis(mb, rec, now=now)["state"] == "idle"
        assert routable == says_idle, (
            f"at {offset}s the router says routable={routable} and the status "
            f"surface says idle={says_idle} — one of them is lying to someone")


def test_the_reason_is_BOUNDED(mb):
    """It is interpolated into CLI output and into JSON the agent reads.

    An unbounded read of a file a runaway worker filled would be a second defect on
    top of the first — and the pool tree has no quota of its own.
    """
    rec = _rec()
    wd = _pool.ensure_worker_dirs(mb, rec.worker_id)
    (wd / "worker.err").write_bytes(b"x" * 50_000 + b"THE-LAST-LINE\n")

    diag = _pool.worker_diagnosis(mb, rec)

    assert len(diag["reason"]) <= 600, len(diag["reason"])
    assert "THE-LAST-LINE" in diag["reason"], (
        "the tail was not kept; the useful line of a traceback is the last one")


def test_an_UNREADABLE_stderr_is_not_a_crash(mb):
    """This runs inside `pool status` and inside the dispatcher's publish step.

    A permissions problem must not take out the command that was trying to explain
    a failure — that is the shape where a diagnostic becomes the outage. (Skipped as
    root, where the chmod cannot deny anything and the test would pass vacuously.)
    """
    import os

    if os.geteuid() == 0:
        pytest.skip("chmod 000 does not stop root; the assertion would be vacuous")
    rec = _rec()
    wd = _pool.ensure_worker_dirs(mb, rec.worker_id)
    err = wd / "worker.err"
    err.write_text("secret-ish\n")
    os.chmod(err, 0o000)
    try:
        diag = _pool.worker_diagnosis(mb, rec)
        assert diag["state"] == "never-started"
        assert diag["reason"] == "", diag
    finally:
        os.chmod(err, 0o600)


def test_the_sbatch_ERROR_path_is_the_path_that_is_read_back(mb):
    """One definition, two callers. The renderer used to take an `out_dir` argument
    while the reader rebuilt the path itself, so the two could name different files
    and the diagnosis would just be permanently empty."""
    import shlex

    from botainer.core.config import JobProfile

    wid = "w0000abc"
    script = _pool.render_worker_sbatch(wid, JobProfile(partition="GPU"), "/proj",
                                        300, quote=shlex.quote, mb=mb)

    assert f"#SBATCH --error={_pool.worker_stderr_path(mb, wid)}" in script, script


def test_a_FINISHED_worker_stops_eating_a_max_concurrent_slot(mb, monkeypatch):
    """Drives the real counting site, which is where the ghost hurt.

    Records only ever left pool state on an explicit `pool stop`, so each worker
    that self-released on its idle timeout held a slot for ever. Measured by the
    refuting review: with the shipped example profile two idled-out records made
    `_start_pool_workers` return 0, and since `max_concurrent` defaults to 1 a
    single idle-out exhausted a default profile — making the documented recovery
    (`botainer-job pool start`) the broken path.
    """
    from botainer.cli import hpc as hpcmod
    from botainer.core import policy as _pol
    from botainer.core.config import JobProfile

    prof = JobProfile(partition="GPU", account="acct", max_concurrent=1)
    dead = _rec("w000000dead")
    _pool.write_pool_state(mb, [dead])
    _pool.write_beat(mb, dead.worker_id, "exiting", 1000.0)
    monkeypatch.setattr(hpcmod, "sbatch_submit", lambda *a, **k: "99999")
    eff = _pol.intersect(_pol.load_site_policy(), _pol.load_user_policy())

    started = hpcmod._start_pool_workers(
        mb.root, "gpu", prof, mb, eff, size=1, idle_to=300)

    assert started == 1, (
        "a worker that reported its own exit still held the only slot, so the pool "
        "could never be re-warmed")
    ids = [w.worker_id for w in _pool.read_pool_state(mb)]
    assert dead.worker_id not in ids, ids


def test_the_dispatcher_reports_how_many_workers_it_ACTUALLY_started(
        mb, monkeypatch, capsys):
    """It printed the number it ASKED for.

    "dispatcher: auto-started warm pool for 'gpu' (size 2)." was written whether two
    workers started or none did, on a channel that is /dev/null for an auto-started
    dispatcher — so the one trace of a pool that could not be re-warmed said the
    opposite of what happened. Measured through the real function by the refuting
    review; the requested-vs-started difference is the whole content of the line.
    """
    from botainer.cli import hpc as hpcmod
    from botainer.core import policy as _pol
    from botainer.core.config import JobProfile

    prof = JobProfile(partition="GPU", account="acct", max_concurrent=1,
                      warm_pool_size=1)
    blocker = _rec("w0000block")
    _pool.write_pool_state(mb, [blocker])
    _pool.write_beat(mb, blocker.worker_id, "idle", 10_000_000_000.0)  # far future
    monkeypatch.setattr(hpcmod, "sbatch_submit", lambda *a, **k: "99999")

    class _Cfg:
        job_profiles = {"gpu": prof}

    hpcmod._autostart_warm_pools(
        mb.root / "unique-project-for-this-test", _Cfg(), mb,
        _pol.intersect(_pol.load_site_policy(), _pol.load_user_policy()))

    err = capsys.readouterr().err
    assert "started 0 of 1" in err, (
        f"the dispatcher reported a warm pool it did not start:\n{err}")
    assert "max_concurrent is 1" in err and "pool status" in err, (
        f"it said 0 without saying WHY or where to look — returning 0 in silence "
        f"is how this stayed invisible for the whole of #68:\n{err}")


def test_a_worker_still_PENDING_in_the_queue_is_NEVER_reaped(mb):
    """The safety half, and the reason the rest of the ghost problem stays open.

    A worker waiting in the queue has no heartbeat yet, which looks exactly like a
    worker that died before writing one. Reaping on "no heartbeat" would start a
    second worker for the same slot the moment a queue was busy — strictly worse
    than leaking the slot. Only the worker's OWN terminal report is trusted here.
    """
    pending = _rec("w00000pend")
    _pool.ensure_worker_dirs(mb, pending.worker_id)      # no beat file at all
    _pool.write_pool_state(mb, [pending])

    reaped = _pool.reap_exited_workers(mb)

    assert reaped == [], reaped
    assert [w.worker_id for w in _pool.read_pool_state(mb)] == [pending.worker_id]


def test_the_AGENT_facing_manifest_carries_the_answer_WITHOUT_host_paths(mb):
    """Both surfaces, one owner — and the agent's copy goes through the same filter
    as the sbatch-rejection text.

    The crossing itself is not new (a child job's whole stderr is already copied
    into out/ by design, and on HPC the state root is bound at its own absolute
    path anyway), so this is hygiene consistency rather than a hole: the agent gets
    the CAUSE and not botainer's directory layout.
    """
    from botainer.cli import hpc as hpcmod

    rec = _rec()
    wd = _pool.ensure_worker_dirs(mb, rec.worker_id)
    (wd / "worker.err").write_text(
        f"{mb.run_dir}/pool/{rec.worker_id}.sbatch: line 13: "
        f"child-job image not found for 'agent-claude'\n")
    _pool.write_pool_state(mb, [rec])

    hpcmod._publish_pool_status(mb)

    doc = json.loads((mb.out_dir / "pool.json").read_text())
    w, = doc["workers"]
    assert w["state"] == "never-started", w
    assert w["heartbeat_age_s"] is None, w
    assert "image not found" in w["reason"], w
    assert str(mb.run_dir) not in w["reason"], (
        f"the host state-root path reached the agent's manifest verbatim: {w}")


def test_pool_status_PRINTS_the_reason_through_the_REAL_command(
        monkeypatch, tmp_path):
    """THE OTHER HALF OF THE ROW: drive `_pool_ctx` itself, which no test did.

    My first version re-implemented the rendering loop and asserted on its own
    output — which proves the rendering I just wrote, not the rendering the command
    does. That is the "presence is not effect" failure this repo has a rule about,
    and it is the exact reason the pool path had no coverage: every existing test
    stops at the layer below.

    And NO `.sif` EXISTS HERE, deliberately. My first fixture wrote one, which made
    the command succeed for a reason unrelated to the defect — the refuting review
    showed that with the image genuinely missing the command exited 1 with
    `child-job image not found` and printed no worker line at all.
    """
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc as hpc_grp

    proj, mailbox = _project(tmp_path, monkeypatch,
                            profile_extra={"warm_pool_size": 1})
    rec = _rec()
    wd = _pool.ensure_worker_dirs(mailbox, rec.worker_id)
    (wd / "worker.err").write_text(
        "Error: child-job image not found for 'agent-claude'\n")
    _pool.write_pool_state(mailbox, [rec])

    res = CliRunner().invoke(hpc_grp, ["pool", "status", "--project", str(proj)])

    assert res.exit_code == 0, (
        f"reading pool state still requires an image it does not use:\n{res.output}")
    assert "never-started" in res.output, (
        f"the command still reports no usable state:\n{res.output}")
    assert "child-job image not found" in res.output, (
        f"the worker's own reason is STILL unreachable from a command — which is "
        f"the whole defect:\n{res.output}")
    assert "its own last words" in res.output, res.output


def test_pool_status_says_WHAT_TO_RUN_when_there_is_no_reason_either(
        monkeypatch, tmp_path):
    """Silence must not be rendered as silence.

    A worker whose allocation ended leaves no stderr at all. Printing
    `state=never-started` and nothing else tells a user their pool is broken and
    gives them nowhere to go — which is the "a question the user has to ask is a
    missing capability" rule. Name the command that answers it.
    """
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc as hpc_grp

    proj, mailbox = _project(tmp_path, monkeypatch)
    rec = _rec()
    _pool.ensure_worker_dirs(mailbox, rec.worker_id)      # no worker.err at all
    _pool.write_pool_state(mailbox, [rec])

    res = CliRunner().invoke(hpc_grp, ["pool", "status", "--project", str(proj)])

    assert res.exit_code == 0, res.output
    assert "never-started" in res.output, res.output
    assert "squeue" in res.output and rec.slurm_job_id in res.output, (
        f"no reason AND no next step — the user is told it is broken and nothing "
        f"else:\n{res.output}")


def test_pool_status_does_NOT_send_a_user_to_squeue_for_a_finished_worker(
        monkeypatch, tmp_path):
    """Through the real command, because the mislabelling was a rendering decision
    as much as a classification one: `squeue -j` for a job that completed prints
    nothing useful, and "the allocation may have ended, or never started" is an
    alarm about the normal case."""
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc as hpc_grp

    proj, mailbox = _project(tmp_path, monkeypatch)
    rec = _rec()
    _pool.ensure_worker_dirs(mailbox, rec.worker_id)
    _pool.write_beat(mailbox, rec.worker_id, "exiting", 1.0)   # long ago
    _pool.write_pool_state(mailbox, [rec])

    res = CliRunner().invoke(hpc_grp, ["pool", "status", "--project", str(proj)])

    assert res.exit_code == 0, res.output
    assert "exiting" in res.output and "finished cleanly" in res.output, res.output
    assert "squeue" not in res.output, (
        f"a worker that finished on its idle timeout is reported as a fault:\n"
        f"{res.output}")


def test_pool_status_WARNS_that_a_hot_task_died_with_a_busy_worker(
        monkeypatch, tmp_path):
    """Classifying `busy`→dead is only half of it: the user has to be TOLD.

    Mutating this rendering away killed no test in the first round, which is the
    protocol's signal that the branch existed and nothing checked it. The stuck
    request is the user's actual problem here — the worker is merely how it got
    stuck — so the line has to name where to look for the orphaned task.
    """
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc as hpc_grp

    proj, mailbox = _project(tmp_path, monkeypatch)
    rec = _rec()
    _pool.ensure_worker_dirs(mailbox, rec.worker_id)
    _pool.write_beat(mailbox, rec.worker_id, "busy", 1.0)      # long ago
    _pool.write_pool_state(mailbox, [rec])

    res = CliRunner().invoke(hpc_grp, ["pool", "status", "--project", str(proj)])

    assert res.exit_code == 0, res.output
    assert "BUSY when it stopped" in res.output, (
        f"a hot task died with this worker and the status says only 'stale':\n"
        f"{res.output}")
    assert "jobs-status" in res.output, (
        f"named the problem without naming where the stuck task is:\n{res.output}")


def test_pool_STOP_works_when_the_image_is_the_broken_thing(monkeypatch, tmp_path):
    """The worse half of the same defect: `pool stop` resolved a child image it
    never used, so a user whose image was broken could not release the allocations
    their workers were holding — only `scancel` by hand, which means knowing the
    job ids botainer had recorded for them."""
    from click.testing import CliRunner

    from botainer.cli import hpc as hpcmod
    from botainer.cli.hpc import hpc as hpc_grp

    proj, mailbox = _project(tmp_path, monkeypatch)
    rec = _rec()
    _pool.ensure_worker_dirs(mailbox, rec.worker_id)
    _pool.write_pool_state(mailbox, [rec])
    calls = []
    monkeypatch.setattr(hpcmod.subprocess, "run",
                        lambda *a, **k: calls.append(a) or None)

    res = CliRunner().invoke(hpc_grp, ["pool", "stop", "--project", str(proj)])

    assert res.exit_code == 0, (
        f"tearing down a pool required an image that is what broke:\n{res.output}")
    assert (_pool.worker_dir(mailbox, rec.worker_id) / "STOP").exists()
    assert calls and "scancel" in calls[0][0], calls
    assert _pool.read_pool_state(mailbox) == [], "the record survived a full stop"
