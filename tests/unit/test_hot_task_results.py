"""Hot results must survive routing, execution and native helper retrieval."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from botainer.core.config import JobProfile
from botainer.core.policy import JobPolicy
from botainer.hpc import dispatcher, jobs, pool

JID = "a" * 16
OTHER = "b" * 16
WID = "w" + "c" * 12
IMAGE = "fixture.sif"
STAMP = "2026-01-01T00:00:00Z"
HELPER = Path(__file__).resolve().parents[2] / "plugins/hpc-launcher/agent_helper/botainer-job"


def setup_job(tmp_path, body_id=JID):
    mb = jobs.JobMailbox(tmp_path, tmp_path / "in", tmp_path / "out", tmp_path / "run")
    for p in (mb.in_dir, mb.out_dir, mb.run_dir):
        p.mkdir()
    pool.ensure_worker_dirs(mb, WID)
    pool.write_pool_state(mb, [pool.WorkerRec(WID, "12345", "cpu", 30, STAMP)])
    pool.write_beat(mb, WID, "idle", pool.time.time())
    (mb.in_dir / f"{JID}.json").write_text(json.dumps({
        "id": body_id, "profile": "cpu", "hot": True,
        "command": ["echo", "hello"], "slurm_job_id": "99999",
    }))
    return mb


def dispatch(mb):
    def no_cold(*a, **kw):
        pytest.fail("a handed-off task must never be submitted again")
    return dispatcher.process_inbox_once(
        mb, {"cpu": JobProfile(partition="test")}, JobPolicy(), IMAGE,
        sbatch=no_cold, now=STAMP)


def status(mb, jid=JID):
    return json.loads((mb.out_dir / f"{jid}.status.json").read_text())


def worker(mb, runner):
    return pool.worker_process_one(
        mb, WID, IMAGE, run_caged=runner,
        write_status=lambda jid, rec: dispatcher._write_status(mb, jid, rec))


def output_runner(argv, out, err):
    out.write_bytes(b"hello\n")
    err.write_bytes(b"diagnostic\n")
    return 7


@pytest.mark.parametrize("stdout,stderr,rc", [(b"hello\n", b"diagnostic\n", 7), (b"", b"", 0)])
def test_native_results_and_assignment_metadata(tmp_path, stdout, stderr, rc):
    mb = setup_job(tmp_path)
    dispatch(mb)
    def run(argv, out, err):
        out.write_bytes(stdout)
        err.write_bytes(stderr)
        rec = status(mb)
        assert rec["profile"] == "cpu"
        assert dispatcher._active_counts(mb) == {"cpu": 1}
        dispatcher.poll_running(mb, set())
        assert status(mb)["state"] == "running"
        return rc
    worker(mb, run)
    rec = status(mb)
    assert rec["state"] == "completed" and rec["exit_code"] == rc
    assert rec["profile"] == "cpu" and rec["submitted_at"] == STAMP
    assert rec["started_at"] and rec["finished_at"] >= rec["started_at"]
    assert rec["worker_slurm_job_id"] == "12345"
    assert "slurm_job_id" not in rec
    env = dict(os.environ, BOTAINER_JOBS_IN=str(mb.in_dir), BOTAINER_JOBS_OUT=str(mb.out_dir))
    for stream, expected in [("stdout", stdout), ("stderr", stderr)]:
        assert (mb.out_dir / f"{JID}.{stream}").read_bytes() == expected
        assert rec["logs"][stream]["state"] == "published"
    result = subprocess.run([sys.executable, str(HELPER), "logs", JID], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (stdout or b"\n")


def test_filename_identity_protects_other_results(tmp_path):
    mb = setup_job(tmp_path, OTHER)
    other_status = mb.out_dir / f"{OTHER}.status.json"
    other_log = mb.out_dir / f"{OTHER}.stdout"
    other_status.write_text('{"state": "completed"}')
    other_log.write_text("untouched")
    dispatch(mb)
    assert worker(mb, output_runner) == JID
    assert status(mb)["exit_code"] == 7
    assert other_status.read_text() == '{"state": "completed"}'
    assert other_log.read_text() == "untouched"


def test_fast_worker_completion_is_not_overwritten(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    real_route = pool.route_hot_task
    def immediate(*args, **kw):
        real_route(*args, **kw)
        worker(mb, output_runner)
    monkeypatch.setattr(pool, "route_hot_task", immediate)
    dispatch(mb)
    assert status(mb)["state"] == "completed"
    assert status(mb)["exit_code"] == 7


def test_agent_inbox_cleanup_failure_cannot_duplicate_execution(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    original = Path.unlink
    target = mb.in_dir / f"{JID}.json"
    def unlink(p, *args, **kw):
        if p == target:
            raise PermissionError("cleanup unavailable")
        return original(p, *args, **kw)
    monkeypatch.setattr(Path, "unlink", unlink)
    dispatch(mb)
    dispatch(mb)
    assert worker(mb, output_runner) == JID
    assert worker(mb, output_runner) is None
    assert status(mb)["state"] == "completed"


def test_runner_failure_is_terminal_and_not_retried(tmp_path):
    mb = setup_job(tmp_path)
    dispatch(mb)
    def fail(*args):
        raise OSError("private runtime path")
    worker(mb, fail)
    rec = status(mb)
    assert rec["state"] == "failed"
    assert "exit_code" not in rec
    assert "private runtime path" not in json.dumps(rec)
    assert worker(mb, fail) is None


@pytest.mark.parametrize("kind", ["binary", "symlink", "fifo", "replace_failure"])
def test_log_publication_outcomes(tmp_path, monkeypatch, kind):
    mb = setup_job(tmp_path)
    dispatch(mb)
    data = b"\x00\xff\n" * 50000
    secret = tmp_path / "host-private"
    secret.write_text("do not publish")
    original = os.replace
    def replace(src, dest):
        if kind == "replace_failure" and Path(dest).name == f"{JID}.stdout":
            raise OSError("private destination path")
        return original(src, dest)
    monkeypatch.setattr(os, "replace", replace)
    def run(argv, out, err):
        err.write_bytes(b"")
        if kind == "symlink":
            out.symlink_to(secret)
        elif kind == "fifo":
            os.mkfifo(out)
        else:
            out.write_bytes(data)
        return 0
    worker(mb, run)
    rec = status(mb)
    assert rec["exit_code"] == 0
    outcome = rec["logs"]["stdout"]
    if kind == "binary":
        assert outcome == {"state": "published", "bytes": len(data)}
        assert (mb.out_dir / f"{JID}.stdout").read_bytes() == data
    else:
        assert outcome["state"] == "unavailable"
        assert not (mb.out_dir / f"{JID}.stdout").exists()
        assert rec["warnings"]
        assert "private destination path" not in json.dumps(rec)
    assert rec["logs"]["stderr"]["state"] == "published"


def test_production_loop_samples_start_and_finish_and_publishes_before_terminal(tmp_path):
    mb = setup_job(tmp_path)
    dispatch(mb)
    clock = [1700000000.5]
    def run(argv, out, err):
        clock[0] += 2
        return output_runner(argv, out, err)
    def write(jid, rec):
        if rec["state"] == "completed":
            assert (mb.out_dir / f"{jid}.stdout").read_bytes() == b"hello\n"
            assert (mb.out_dir / f"{jid}.stderr").read_bytes() == b"diagnostic\n"
        dispatcher._write_status(mb, jid, rec)
    pool.worker_loop(mb, WID, IMAGE, 30, run_caged=run, write_status=write,
                     now=lambda: clock[0], max_iterations=1)
    rec = status(mb)
    assert rec["started_at"] == "2023-11-14T22:13:20Z"
    assert rec["finished_at"] == "2023-11-14T22:13:22Z"


def test_terminal_publication_failure_does_not_execute_again(tmp_path):
    mb = setup_job(tmp_path)
    dispatch(mb)
    calls = []
    def run(*args):
        calls.append(True)
        return output_runner(*args)
    def write(jid, rec):
        if rec["state"] == "completed":
            raise OSError("status destination unavailable")
        dispatcher._write_status(mb, jid, rec)
    with pytest.raises(OSError, match="status destination unavailable"):
        pool.worker_process_one(mb, WID, IMAGE, run_caged=run, write_status=write)
    with pytest.raises(RuntimeError, match="earlier hot task claim exists"):
        worker(mb, run)
    assert len(calls) == 1
    assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) is None
    assert status(mb)["state"] == "running"


def test_copy_is_chunked_and_destination_never_exposes_partial_output(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    dispatch(mb)
    data = b"x" * 150000
    original = os.fdopen
    reads = []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            assert 0 < count <= 65536
            assert not (mb.out_dir / f"{JID}.stdout").exists()
            reads.append(count)
            return self.stream.read(count)
    def fdopen(fd, *args, **kw):
        result = original(fd, *args, **kw)
        return Reader(result) if args == ("rb",) else result
    monkeypatch.setattr(os, "fdopen", fdopen)
    def run(argv, out, err):
        out.write_bytes(data)
        # Missing stderr makes its publication fail before entering a Reader.
        return 0
    worker(mb, run)
    assert len(reads) >= 4
    assert (mb.out_dir / f"{JID}.stdout").read_bytes() == data


def test_failed_copy_and_cleanup_still_publish_terminal_result(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    dispatch(mb)
    replace, unlink = os.replace, Path.unlink
    def fail_replace(src, dst):
        if Path(dst).name == f"{JID}.stdout":
            raise OSError("rename unavailable")
        return replace(src, dst)
    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(".hot-log-"):
            raise OSError("cleanup unavailable")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    worker(mb, output_runner)
    rec = status(mb)
    assert rec["state"] == "completed" and rec["exit_code"] == 7
    assert rec["logs"]["stdout"]["state"] == "unavailable"
    assert (mb.out_dir / f"{JID}.stderr").read_bytes() == b"diagnostic\n"
    assert worker(mb, output_runner) is None


def test_destination_symlink_is_replaced_without_modifying_its_target(tmp_path):
    mb = setup_job(tmp_path)
    dispatch(mb)
    target = tmp_path / "host-private"
    target.write_text("unchanged")
    destination = mb.out_dir / f"{JID}.stdout"
    destination.symlink_to(target)
    worker(mb, output_runner)
    assert destination.read_bytes() == b"hello\n"
    assert not destination.is_symlink()
    assert target.read_text() == "unchanged"


def test_failed_worker_publication_falls_back_once(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    original = os.replace
    def fail(src, dst):
        if Path(dst) == pool.worker_in_dir(mb, WID) / f".{JID}.prepared":
            raise OSError("worker preparation unavailable")
        return original(src, dst)
    monkeypatch.setattr(os, "replace", fail)
    cold = []
    def submit(*args, **kw):
        cold.append(True)
        return "54321"
    for _ in range(2):
        dispatcher.process_inbox_once(
            mb, {"cpu": JobProfile(partition="test")}, JobPolicy(), IMAGE,
            sbatch=submit, now=STAMP)
    assert len(cold) == 1
    assert status(mb)["state"] == "queued"
    assert status(mb)["slurm_job_id"] == "54321"
    assert status(mb)["warnings"]
    assert worker(mb, output_runner) is None


def test_assignment_survives_unavailable_public_status_read(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    dispatch(mb)
    original = Path.read_text
    def read(path, *args, **kw):
        if path == mb.out_dir / f"{JID}.status.json":
            raise FileNotFoundError("status visibility unavailable")
        return original(path, *args, **kw)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_text", read)
        worker(mb, output_runner)
    rec = status(mb)
    assert rec["profile"] == "cpu"
    assert rec["submitted_at"] == STAMP
    assert rec["worker_slurm_job_id"] == "12345"


@pytest.mark.parametrize("legacy", [False, True])
def test_request_cannot_forge_host_assignment_or_result_identity(tmp_path, legacy):
    mb = setup_job(tmp_path, OTHER)
    request_path = mb.in_dir / f"{JID}.json"
    request = json.loads(request_path.read_text())
    request["assignment"] = {"id": JID, "worker": WID, "profile": "forged",
                             "slurm_job_id": "67890", "worker_slurm_job_id": "67890"}
    request_path.write_text(json.dumps(request))
    if legacy:
        # Previously routed requests used a raw body in <id>.json. A reserved
        # field planted before an upgrade must never become host authority.
        (pool.worker_in_dir(mb, WID) / f"{JID}.json").write_text(json.dumps(request))
        dispatcher._write_status(mb, JID, {"id": JID, "worker": WID,
                                         "profile": "cpu", "submitted_at": STAMP})
    else:
        dispatch(mb)
    worker(mb, output_runner)
    rec = status(mb)
    assert rec["id"] == JID and rec["profile"] == "cpu"
    assert rec.get("worker_slurm_job_id") != "67890"
    assert "slurm_job_id" not in rec
    assert not (mb.out_dir / f"{OTHER}.status.json").exists()


@pytest.mark.parametrize("damage", ["missing", "wrong_id", "wrong_worker"])
def test_invalid_host_assignment_is_consumed_without_execution(tmp_path, damage):
    mb = setup_job(tmp_path)
    dispatch(mb)
    path = pool.worker_in_dir(mb, WID) / f"{JID}.task.json"
    packet = json.loads(path.read_text())
    if damage == "missing":
        del packet["assignment"]
    elif damage == "wrong_id":
        packet["assignment"]["id"] = OTHER
    else:
        packet["assignment"]["worker"] = "w" + "d" * 12
    path.write_text(json.dumps(packet))
    calls = []
    def run(*args):
        calls.append(True)
        return output_runner(*args)
    worker(mb, run)
    rec = status(mb)
    assert rec["state"] == "refused" and rec["reason"] == "hot task assignment is invalid"
    assert calls == [] and worker(mb, run) is None
    assert not (mb.out_dir / f"{OTHER}.status.json").exists()


@pytest.mark.parametrize("worker_state", [
    "exiting", "stopped", "removed", "busy", "stale", "missing_beat",
    "missing_pool", "damaged_pool", "incomplete_pool", "wrong_version",
])
def test_hot_status_cannot_outlive_worker_slot(tmp_path, worker_state):
    mb = setup_job(tmp_path)
    dispatch(mb)
    dispatcher._write_status(mb, JID, {**status(mb), "state": "running"})
    if worker_state == "removed":
        pool.write_pool_state(mb, [])
    elif worker_state.startswith(("missing_", "damaged_", "incomplete_", "wrong_")):
        pool.worker_beat_path(mb, WID).unlink()
        state_path = pool.pool_root(mb) / "state.json"
        if worker_state == "missing_pool":
            state_path.unlink()
        elif worker_state == "damaged_pool":
            state_path.write_text("not json")
        elif worker_state == "incomplete_pool":
            state_path.write_text(json.dumps({"version": pool.POOL_STATE_VERSION, "workers": [{}]}))
        elif worker_state == "wrong_version":
            state_path.write_text(json.dumps({"version": "unknown", "workers": []}))
    else:
        pool.write_beat(mb, WID, "busy" if worker_state == "stale" else worker_state,
                        0 if worker_state == "stale" else pool.time.time())
    (mb.in_dir / f"{OTHER}.json").write_text(json.dumps({
        "id": OTHER, "profile": "cpu", "command": ["true"],
    }))
    submitted = []
    def submit(*args, **kwargs):
        submitted.append(True)
        return "54321"
    results = dispatcher.process_inbox_once(
        mb, {"cpu": JobProfile(partition="test")}, JobPolicy(), IMAGE,
        sbatch=submit, now=STAMP)
    live = worker_state not in ("exiting", "stopped", "removed")
    assert [(r.job_id, r.state) for r in results] == [(OTHER, "deferred" if live else "queued")]
    assert len(submitted) == (0 if live else 1)


def test_busy_task_is_not_routable_with_an_old_idle_beat(tmp_path):
    mb = setup_job(tmp_path)
    dispatch(mb)
    routable = []
    def run(argv, out, err):
        # Model an idle heartbeat cached before the worker claimed this task.
        routable.append(pool.find_idle_worker(mb, "cpu", now=pool.time.time()))
        return output_runner(argv, out, err)
    worker(mb, run)
    assert routable == [None]
    assert status(mb)["state"] == "completed"
    assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) == WID


@pytest.mark.parametrize("boundary", ["before_status", "before_ready", "after_ready"])
def test_interrupted_handoff_resumes_exact_packet_without_cold_submit(tmp_path, monkeypatch, boundary):
    mb = setup_job(tmp_path)
    class Interrupted(BaseException):
        pass
    original_write, original_replace = dispatcher._write_status, os.replace
    def write(mailbox, jid, record):
        if boundary == "before_status":
            raise Interrupted()
        return original_write(mailbox, jid, record)
    def replace(src, dst):
        if Path(dst).name == f"{JID}.task.json":
            if boundary == "before_ready":
                raise Interrupted()
            result = original_replace(src, dst)
            if boundary == "after_ready":
                raise Interrupted()
            return result
        return original_replace(src, dst)
    with monkeypatch.context() as scoped:
        scoped.setattr(dispatcher, "_write_status", write)
        scoped.setattr(os, "replace", replace)
        with pytest.raises(Interrupted):
            dispatch(mb)
    assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) is None
    # Recovery must not use a replacement body from the agent-writable inbox.
    (mb.in_dir / f"{JID}.json").write_text(json.dumps({
        "profile": "cpu", "hot": True, "command": ["echo", "changed"],
    }))
    dispatch(mb)
    observed = []
    def run(argv, out, err):
        observed.append(argv[-2:])
        return output_runner(argv, out, err)
    worker(mb, run)
    dispatch(mb)
    assert worker(mb, run) is None
    assert observed == [["echo", "hello"]]
    assert status(mb)["state"] == "completed"


@pytest.mark.parametrize("conflicting", ["queued", "completed", "failed", "refused"])
def test_prepared_handoff_cannot_overwrite_an_existing_outcome(tmp_path, monkeypatch, conflicting):
    mb = setup_job(tmp_path)
    original = os.replace
    def fail(src, dst):
        if Path(dst).name == f"{JID}.task.json":
            raise OSError("publication unavailable")
        return original(src, dst)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", fail)
        dispatch(mb)
    dispatcher._write_status(mb, JID, {"id": JID, "state": conflicting})
    dispatch(mb)
    assert status(mb)["state"] == conflicting
    assert worker(mb, output_runner) is None
    assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) == WID


def test_failed_prepared_handoff_reserves_worker_and_retries_once(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    original = os.replace
    def fail(src, dst):
        if Path(dst).name == f"{JID}.task.json":
            raise OSError("publication unavailable")
        return original(src, dst)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", fail)
        dispatch(mb)
        dispatch(mb)
        assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) is None
        assert worker(mb, output_runner) is None
    dispatch(mb)
    assert worker(mb, output_runner) == JID
    dispatch(mb)
    assert worker(mb, output_runner) is None
    assert status(mb)["exit_code"] == 7


@pytest.mark.parametrize("damage", ["missing_profile", "invalid_profile", "missing_timeout", "invalid_timeout"])
def test_incomplete_other_worker_does_not_prove_slot_release(tmp_path, damage):
    mb = setup_job(tmp_path)
    dispatch(mb)
    record = pool.WorkerRec("w" + "d" * 12, "54321", "cpu", 30, STAMP).__dict__
    if damage == "missing_profile":
        del record["profile"]
    elif damage == "invalid_profile":
        record["profile"] = []
    elif damage == "missing_timeout":
        del record["idle_timeout"]
    else:
        record["idle_timeout"] = None
    (pool.pool_root(mb) / "state.json").write_text(json.dumps({
        "version": pool.POOL_STATE_VERSION, "workers": [record],
    }))
    assert dispatcher._active_counts(mb) == {"cpu": 1}


@pytest.mark.parametrize("damage", ["null", "wrong_id", "cold_id"])
def test_prepared_handoff_does_not_replace_damaged_status(tmp_path, monkeypatch, damage):
    mb = setup_job(tmp_path)
    original = os.replace
    def fail(src, dst):
        if Path(dst).name == f"{JID}.task.json":
            raise OSError("publication unavailable")
        return original(src, dst)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", fail)
        dispatch(mb)
    record = status(mb)
    if damage == "null":
        record = None
    elif damage == "wrong_id":
        record["id"] = OTHER
    else:
        record["slurm_job_id"] = "54321"
    dispatcher._write_status(mb, JID, record)
    dispatch(mb)
    dispatch(mb)
    assert status(mb) == record
    assert worker(mb, output_runner) is None
    assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) == WID


def test_error_after_preparation_commit_does_not_cold_submit(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    original = os.replace
    def fail(src, dst):
        result = original(src, dst)
        if Path(dst).name == f".{JID}.prepared":
            raise OSError("preparation acknowledgement unavailable")
        return result
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", fail)
        dispatch(mb)
    dispatch(mb)
    assert worker(mb, output_runner) == JID
    assert status(mb)["exit_code"] == 7
    assert worker(mb, output_runner) is None


def prepare_without_status(mb, monkeypatch):
    def fail(*args):
        raise OSError("status temporarily unavailable")
    with monkeypatch.context() as scoped:
        scoped.setattr(dispatcher, "_write_status", fail)
        dispatch(mb)


def test_prepared_handoff_survives_image_failure_and_holds_its_slot(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    prepare_without_status(mb, monkeypatch)
    assert dispatcher._active_counts(mb) == {"cpu": 1}
    assert dispatcher.pending_requests(mb) == []
    assert dispatcher.refuse_all_pending(mb, "cannot resolve image") == []
    dispatch(mb)
    assert not (mb.in_dir / f"{JID}.json").exists()
    assert worker(mb, output_runner) == JID
    assert status(mb)["exit_code"] == 7


@pytest.mark.parametrize("released", ["exiting", "stopped", "removed"])
def test_prepared_handoff_to_released_worker_is_not_run(tmp_path, monkeypatch, released):
    mb = setup_job(tmp_path)
    prepare_without_status(mb, monkeypatch)
    if released == "removed":
        pool.write_pool_state(mb, [])
    else:
        pool.write_beat(mb, WID, released, pool.time.time())
    dispatch(mb)
    assert status(mb)["state"] == "refused"
    assert "before handoff" in status(mb)["reason"]
    assert worker(mb, output_runner) is None
    assert list((pool.worker_dir(mb, WID) / "abandoned").glob("*"))


@pytest.mark.parametrize("damage", ["json", "assignment"])
def test_abandoned_preparation_never_replays_mutable_agent_request(tmp_path, monkeypatch, damage):
    mb = setup_job(tmp_path)
    prepare_without_status(mb, monkeypatch)
    dispatcher._write_status(mb, JID, {"id": JID, "worker": WID,
                                     "profile": "cpu", "state": "assigned"})
    path = pool.worker_in_dir(mb, WID) / f".{JID}.prepared"
    if damage == "json":
        path.write_text("not json")
    else:
        path.write_text('{"assignment": null}')
    for _ in range(3):
        dispatch(mb)
    assert worker(mb, output_runner) is None
    assert pool.find_idle_worker(mb, "cpu", now=pool.time.time()) == WID
    assert dispatcher.pending_requests(mb) == []
    assert dispatcher.refuse_all_pending(mb, "image unavailable") == []
    assert pool.hot_handoff_statuses(mb)[JID]["state"] == "abandoned"
    assert status(mb)["state"] == "refused"
    assert dispatcher._active_counts(mb) == {}


@pytest.mark.parametrize("state", ["assigned", "deferred", "missing"])
def test_prepared_slot_is_counted_once(tmp_path, monkeypatch, state):
    mb = setup_job(tmp_path)
    prepare_without_status(mb, monkeypatch)
    if state != "missing":
        dispatcher._write_status(mb, JID, {"id": JID, "worker": WID,
                                         "profile": "cpu", "state": state})
    assert dispatcher._active_counts(mb) == {"cpu": 1}


def test_existing_claim_preserves_unknown_execution_and_stops_worker(tmp_path):
    mb = setup_job(tmp_path)
    dispatch(mb)
    inbox = pool.worker_in_dir(mb, WID) / f"{JID}.task.json"
    claim = pool.worker_dir(mb, WID) / "run" / f"{JID}.json"
    os.link(inbox, claim)
    calls = []
    with pytest.raises(RuntimeError, match="not re-executed; outcome unknown"):
        pool.worker_loop(mb, WID, IMAGE, 30, run_caged=lambda *args: calls.append(True),
                         write_status=lambda jid, rec: dispatcher._write_status(mb, jid, rec),
                         max_iterations=1)
    assert calls == [] and inbox.exists() and claim.exists()
    assert status(mb)["state"] == "assigned"
    assert pool.read_beat(mb, WID)["state"] == "busy"


def test_abandonment_reason_survives_missing_status_and_discarded_stderr(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    prepare_without_status(mb, monkeypatch)
    (pool.worker_in_dir(mb, WID) / f".{JID}.prepared").write_text("not json")
    with open(os.devnull, "w") as sink, monkeypatch.context() as scoped:
        scoped.setattr(pool.sys, "stderr", sink)
        dispatch(mb)
    (mb.out_dir / f"{JID}.status.json").unlink()
    assert pool.hot_handoff_statuses(mb)[JID]["reason"] == "hot handoff abandoned: invalid prepared metadata"
    dispatch(mb)
    assert worker(mb, output_runner) is None


def test_unreadable_preparation_uses_registered_worker_profile_for_count(tmp_path, monkeypatch):
    mb = setup_job(tmp_path)
    prepare_without_status(mb, monkeypatch)
    (mb.in_dir / f"{JID}.json").write_text('{"profile":"replacement"}')
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == f".{JID}.prepared":
            raise OSError("temporarily unreadable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert dispatcher._active_counts(mb) == {"cpu": 1}
