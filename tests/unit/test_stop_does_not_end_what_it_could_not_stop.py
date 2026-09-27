"""A stop that failed must not record the session as ended. (#229)

`_stop_one` ran `_do_runtime_stop` in a `try` and did all end-of-life
bookkeeping in a `finally`. Every failure path in `_do_runtime_stop` was a bare
`return` — no container id, docker/scancel not on PATH, a non-zero `docker
stop`, the 30s timeout whose own message says "try --force" — and all of them
landed in the same `finally`, which stamps `ended_at` and runs
`run_post_session_hooks`.

TWO CONSEQUENCES, BOTH OBSERVED:

  * `stop` printed "docker not on PATH; cannot stop" and then recorded the
    session as ended, while `status` went on showing it as running. The record
    and the display disagreed about the same session, and nothing self-heals:
    status only writes ended_at when it is absent, never clears a wrong one.

  * post_session ran against a LIVE container. In shared mode that sets the
    project's `.credentials.json` aside as `.credentials.json.pre-shared` and
    replaces it with a symlink — so a stop that did nothing still changed the
    login the running agent was holding. Exit code was 0 throughout, so a script
    wrapping `botainer stop` read the failure as success.

WHY THE `finally` WAS THERE, because the fix must not undo it: #146. Before it,
a failed stop left the session "running" forever with no end timestamp and
`status` lied in the other direction. That reasoning is right and is preserved —
a stop that SUCCEEDS, or a session with nothing to stop, still records. What
changed is that `_do_runtime_stop` now reports a verdict instead of signalling
failure with a bare `return`, so the caller can tell the two apart.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from botainer.cli import stop as stop_mod


class _Rec:
    """Only the fields _stop_one touches."""

    def __init__(self, *, runtime="docker", container_id="deadbeef0123",
                 ended_at=None):
        self.session_id = "s" * 16
        self.runtime = runtime
        self.ended_at = ended_at
        self.spec = {}
        self.docker = type("D", (), {"container_id": container_id})()
        self.apptainer = None


def _spy(monkeypatch):
    """Record whether the two bookkeeping side effects happened."""
    calls = {"ended": False, "post_session": False}

    def _upd(session_dir, **kw):
        if "ended_at" in kw:
            calls["ended"] = True

    monkeypatch.setattr(stop_mod.session_record, "update_runtime", _upd)
    monkeypatch.setattr(stop_mod.composition, "run_post_session_hooks",
                        lambda spec: calls.__setitem__("post_session", True))

    # STUB THE SPEC VALIDATION, or the post_session assertions cannot fire.
    # `rec.spec` here is `{}`, which SessionSpec.model_validate rejects — and
    # the production code wraps that call in `except Exception: pass`. So with a
    # bare `{}` spec, post_session never runs no matter what the code does, and
    # `assert not calls["post_session"]` would pass against the unfixed version
    # too. Found by running the suite: the positive-control test failed both
    # with AND without the fix, which is what an unfalsifiable assertion looks
    # like from the outside.
    from botainer.core.spec import SessionSpec
    monkeypatch.setattr(SessionSpec, "model_validate",
                        classmethod(lambda cls, raw: object()))
    return calls


def test_a_stop_that_could_not_run_records_nothing(tmp_path, monkeypatch) -> None:
    """docker not on PATH: the session is still up, so nothing is torn down."""
    calls = _spy(monkeypatch)
    monkeypatch.setattr(stop_mod.shutil, "which", lambda _n: None)
    stop_mod._stop_one(_Rec(), tmp_path, force=False)
    assert not calls["ended"], (
        "ended_at was stamped after `stop` reported it could not stop — "
        "`status` will keep showing the session as running")
    assert not calls["post_session"], (
        "post_session ran against a container that is still up; in shared mode "
        "that sets the live credential aside and re-symlinks it")


def test_a_stop_that_FAILED_records_nothing(tmp_path, monkeypatch) -> None:
    """`docker stop` exiting non-zero is a failure, not an ending."""
    calls = _spy(monkeypatch)
    monkeypatch.setattr(stop_mod.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(stop_mod.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stderr": "Cannot connect to the daemon"})())
    stop_mod._stop_one(_Rec(), tmp_path, force=False)
    assert not (calls["ended"] or calls["post_session"])


def test_a_stop_that_WORKED_still_records(tmp_path, monkeypatch) -> None:
    """#146 must survive the fix.

    Without this, the fix would be satisfied by never recording anything, and a
    stopped session would show as running forever.
    """
    calls = _spy(monkeypatch)
    monkeypatch.setattr(stop_mod.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(stop_mod.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stderr": ""})())
    stop_mod._stop_one(_Rec(), tmp_path, force=False)
    assert calls["ended"] and calls["post_session"], (
        "a successful stop must still record ended_at and run post_session "
        "(#146) — otherwise `status` reports it running forever")


def test_nothing_to_stop_counts_as_stopped(tmp_path, monkeypatch) -> None:
    """A mock session is not running; recording that is correct, not a failure."""
    calls = _spy(monkeypatch)
    stop_mod._stop_one(_Rec(runtime="mock"), tmp_path, force=False)
    assert calls["ended"]


def test_an_already_ended_session_is_not_ended_again(tmp_path, monkeypatch) -> None:
    """Passing an explicit id skips the liveness filter.

    So `stop <id>` on a finished session overwrote its historical ended_at with
    today's date and ran post_session a second time — re-signalling a recorded
    pid that may since belong to an unrelated process.
    """
    calls = _spy(monkeypatch)
    stop_mod._stop_one(_Rec(runtime="mock", ended_at="2026-09-08T10:00:00Z"),
                       tmp_path, force=False)
    assert not calls["ended"], "a historical ended_at was overwritten"
    assert not calls["post_session"], "post_session ran a second time"
