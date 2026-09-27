"""`--hot` that cannot be routed says so, instead of quietly queueing (#150).

A hot request asks for "run this now, no SLURM queue wait" by handing it to an
idle warm-pool worker. When that is not possible the dispatcher falls through to
an ordinary cold sbatch — which is the right BEHAVIOUR, because the work still
runs, and was the wrong REPORT: the status record showed a plain queued job with
nothing to say hot had been attempted, let alone why it failed.

On a busy partition that is hours of waiting nobody can account for, and the
three ways it happens are indistinguishable from each other and from a job that
never asked for hot at all.

The reason rides the `warnings` list that a queued status already carries, so
`botainer-job status` and the agent both see it with no new field to learn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from botainer.core.policy import JobPolicy
from botainer.core.config import JobProfile
from botainer.hpc import dispatcher as _d
from botainer.hpc import jobs as _jobs


# Job ids are `^[0-9a-f]{16}$` — the dispatcher derives the id from the
# FILENAME and validates it, deliberately never reading request["id"], so that
# agent-supplied data cannot become a path component. A test using a friendly id
# like "j1" is silently skipped by that filter and proves nothing.


def _mailbox(tmp_path: Path) -> _jobs.JobMailbox:
    mb = _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                          out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir(parents=True, exist_ok=True)
    return mb


def _hot_request(mb: _jobs.JobMailbox, job_id: str, profile: str) -> None:
    (mb.in_dir / f"{job_id}.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": job_id, "profile": profile,
        "hot": True, "command": ["echo", "hi"],
    }))


def _status_of(mb: _jobs.JobMailbox, job_id: str) -> dict:
    return json.loads((mb.out_dir / f"{job_id}.status.json").read_text())


def _run(mb: _jobs.JobMailbox, profiles: dict) -> list:
    return _d.process_inbox_once(
        mb, profiles, JobPolicy(), "img.sif",
        sbatch=lambda *a, **k: "12345", now="2026-08-28T00:00:00Z")


def test_a_hot_request_for_a_profile_with_no_pool_says_which_profile(
        tmp_path) -> None:
    """The commonest case: `--hot` against a profile nobody configured a warm
    pool for. It queues, which is fine, and used to look identical to a request
    that never asked for hot."""
    mb = _mailbox(tmp_path)
    _hot_request(mb, "00000000000000a1", "gpu")

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2)})
    status = _status_of(mb, "00000000000000a1")

    assert status["state"] == "queued", "the work still runs — that part is right"
    warnings = " ".join(status.get("warnings", []))
    assert "--hot" in warnings, "the status must say hot was attempted"
    assert "'gpu'" in warnings, "and name the profile, so it is actionable"
    assert "no warm pool is running" in warnings
    assert "pool start" in warnings, (
        "name the command that fixes it, not just the absence"
    )


def test_an_unknown_profile_is_REFUSED_rather_than_warned_about(tmp_path) -> None:
    """Not a hot-fallback case at all, and the first cut of this fix got that
    wrong by adding a warning for it.

    An unknown profile never becomes a cold job: submit_request refuses it and
    names the profile. A warning attached to a job that is not going to run
    would be a rule backing up no gap — dead code with a comment on it. The
    test is kept to pin WHY that branch is absent.
    """
    mb = _mailbox(tmp_path)
    _hot_request(mb, "00000000000000a2", "typo")

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2)})
    status = _status_of(mb, "00000000000000a2")

    assert status["state"] == "refused"
    assert "typo" in status["reason"], "the refusal names the profile"
    assert "warnings" not in status


def test_a_hot_request_with_a_configured_but_EMPTY_pool_says_so_differently(
        tmp_path) -> None:
    """This is the case a user most needs distinguished from the one above.

    The pool IS configured — so the answer is "start a worker / wait", not
    "configure a pool". Collapsing the two into one message would send someone
    to edit a config that is already correct.
    """
    mb = _mailbox(tmp_path)
    _hot_request(mb, "00000000000000a3", "gpu")

    # A worker REGISTERED for this profile but not idle. Read from pool state,
    # because that is what routing consults — a config field can say a pool
    # exists when none is running, and the message must match reality.
    from botainer.hpc import pool as _pool
    wid = _pool.new_worker_id("abcdef012345")
    _pool.ensure_worker_dirs(mb, wid)
    _pool.write_pool_state(mb, [_pool.WorkerRec(wid, "999", "gpu", 300, "now")])

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2,
                                warm_pool_size=2)})
    warnings = " ".join(_status_of(mb, "00000000000000a3").get("warnings", []))

    assert "no idle worker" in warnings, warnings
    assert "waits in the SLURM queue" in warnings, (
        "and the consequence, in the user's terms — the whole point of --hot "
        "was not waiting"
    )


def test_a_routing_failure_names_the_error_and_the_worker(
        tmp_path, monkeypatch) -> None:
    """The third fall-through was `except Exception: pass`, which made a real
    routing bug indistinguishable from an empty pool."""
    from botainer.hpc import pool as _pool

    mb = _mailbox(tmp_path)
    _hot_request(mb, "00000000000000a4", "gpu")
    monkeypatch.setattr(_pool, "find_idle_worker",
                        lambda *a, **k: "worker-7")

    def _boom(*_a, **_k):
        raise OSError("mailbox full")
    monkeypatch.setattr(_pool, "route_hot_task", _boom)

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2,
                                warm_pool_size=2)})
    warnings = " ".join(_status_of(mb, "00000000000000a4").get("warnings", []))

    assert "worker-7" in warnings, "name WHICH worker failed"
    assert "OSError" in warnings and "mailbox full" in warnings, (
        "and the real error — swallowing it is what made this class of bug "
        "invisible in the first place"
    )


def test_a_request_that_never_asked_for_hot_gets_no_hot_warning(
        tmp_path) -> None:
    """A warning that fires on ordinary jobs is noise, and this project has
    already been bitten by a channel people learn to skip."""
    mb = _mailbox(tmp_path)
    (mb.in_dir / "00000000000000a5.json").write_text(json.dumps({
        "version": "botainer-job-v1", "id": "00000000000000a5", "profile": "gpu",
        "command": ["echo", "hi"],
    }))

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2)})
    status = _status_of(mb, "00000000000000a5")

    assert "--hot" not in " ".join(status.get("warnings", []))


def test_a_hot_request_that_IS_routed_is_not_warned_about(tmp_path, monkeypatch) -> None:
    """The success path must stay clean — and must still be reachable, or the
    tests above would pass on a build where hot never works at all."""
    from botainer.hpc import pool as _pool

    mb = _mailbox(tmp_path)
    _hot_request(mb, "00000000000000a6", "gpu")
    monkeypatch.setattr(_pool, "find_idle_worker", lambda *a, **k: "w123456789abc")

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2,
                                warm_pool_size=2)})
    status = _status_of(mb, "00000000000000a6")

    assert status["state"] == "assigned", status
    assert status["worker"] == "w123456789abc"
    assert "warnings" not in status


def test_a_worker_for_a_DIFFERENT_profile_does_not_count_as_this_pool(
        tmp_path) -> None:
    """Exposed by mutation: ignoring the profile when asking "is a pool
    running?" passed every other test, because each had either no workers at all
    or only workers of the profile being asked about.

    A cluster with a `cpu` pool running and a `gpu` request is the ordinary
    case, and it must say "no pool running for gpu" — telling someone to wait
    for an idle worker in a pool that does not exist is advice they can follow
    forever.
    """
    from botainer.hpc import pool as _pool

    mb = _mailbox(tmp_path)
    _hot_request(mb, "00000000000000a7", "gpu")
    wid = _pool.new_worker_id("abcdef012345")
    _pool.ensure_worker_dirs(mb, wid)
    _pool.write_pool_state(mb, [_pool.WorkerRec(wid, "999", "cpu", 300, "now")])

    _run(mb, {"gpu": JobProfile(partition="GPU", max_concurrent=2),
              "cpu": JobProfile(partition="CPU", max_concurrent=2)})
    warnings = " ".join(_status_of(mb, "00000000000000a7").get("warnings", []))

    assert "no warm pool is running" in warnings, warnings
    assert "no idle worker" not in warnings, (
        "a cpu worker is not an idle gpu worker; saying 'wait for one' points "
        "at a pool that does not exist"
    )
