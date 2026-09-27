"""`botainer list` must answer "when did this project last run", honestly.

Session records use `started_at`; reading a different key leaves the reported
last-run time empty. Directory timestamps do not prove that a session ran.

TWO INDEPENDENT BUGS, and the second is the one that matters.

  1. THE READER ASKS FOR A KEY THE WRITER NEVER WRITES. `state/dir.py` did
     `spec_data.get("composed_at")`. The in-memory spec really does carry
     `composed_at` (composition.py sets it), but the `spec.json` that lands on
     disk is the SESSION RECORD, whose keys are `started_at` / `ended_at`.
     `last_session_runtime` worked and hid this, because `runtime` is a key both
     sides happen to agree on.

  2. A SESSION DIRECTORY IS NOT A SESSION. It picked the newest DIRECTORY by
     mtime and reported it as the last session. `botainer inspect` launches
     nothing and still creates a session directory with `started_at: null`, so
     the answer could be a timestamp for something that never ran. This rule is
     already written down in `cli/_common.py` — "the discriminator is a RECORD
     WITH A START TIME, not a directory entry", put there by a refuting review
     that caught the same mistake in a different place. This code predates or
     ignored it.

WHY `started_at` AND NOT `ended_at`:

`ended_at` is NOT reliably available, even for a session that is long over.

  * clean `botainer stop` writes a true end time
  * `status` reconciles a dead record lazily — but only in NON-JSON mode
    (`cli/status.py:151`), and it writes "now", i.e. when it NOTICED, not when
    the session ended
  * a crash, `kill -9`, `scancel`, or a laptop shutting down writes nothing at
    all, and nobody has to run `status` ever

So a dashboard polling `status --json` never triggers that reconcile and can
watch `ended_at` stay null forever on a session that ended days ago.

`started_at` has none of these problems: it is written once, when the session
actually launches, and nothing later needs to cooperate.

THE HONESTY REQUIREMENT THIS FILE PINS: a null `ended_at` means WE DO NOT KNOW
WHEN IT ENDED. It does NOT mean the session is running. Liveness is a different
question with a different answer (`status`'s `alive`, from
`liveness.is_session_alive`). A consumer that reads null as "still running"
would show a crashed session as live forever, so the field is emitted
explicitly rather than omitted, and its meaning is documented at the source.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from botainer.state import dir as state_dir


def _project_with_sessions(root: Path, uuid: str, sessions: dict[str, dict]) -> None:
    """Write a project whose `sessions/<id>/spec.json` files say exactly this.

    Built from the REAL on-disk shape — `meta.json` beside a `sessions/` dir,
    and session records carrying their OWN keys (`started_at`/`ended_at`)
    rather than the `composed_at` the reader was asking for. That mismatch is
    bug 1, so the fixture has to be the writer's shape, not the reader's.
    """
    proj = root / "state" / uuid
    (proj / "sessions").mkdir(parents=True, exist_ok=True)
    (proj / "meta.json").write_text(
        json.dumps({"display_name": "demo",
                    "path_history": [str(root / "demo")]}),
        encoding="utf-8")
    for sid, fields in sessions.items():
        d = proj / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "spec.json").write_text(json.dumps(fields), encoding="utf-8")


def _entry(root: Path, uuid: str, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(root))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    entries = state_dir.list_projects()
    match = [e for e in entries if e.uuid == uuid]
    assert match, f"project {uuid} not listed at all"
    return match[0]


def test_a_started_session_reports_when_it_started(tmp_path, monkeypatch):
    """THE DEFECT. This returned "" for every project on the machine."""
    root = tmp_path / "state_root"
    _project_with_sessions(root, "aaaaaaaa-0000-0000-0000-000000000001", {
        "sess1": {"session_id": "sess1", "runtime": "docker",
                  "started_at": "2026-09-20T10:00:00Z", "ended_at": None},
    })
    e = _entry(root, "aaaaaaaa-0000-0000-0000-000000000001", monkeypatch)
    assert e.last_session_at == "2026-09-20T10:00:00Z", (
        f"a session that demonstrably ran reports no time: {e.last_session_at!r}")


def test_a_directory_that_never_STARTED_is_not_a_session(tmp_path, monkeypatch):
    """BUG 2. `botainer inspect` launches nothing and still leaves a directory.

    Reporting its timestamp as "when this project last ran" is the exact error
    `cli/_common.py` already warns about.
    """
    root = tmp_path / "state_root"
    _project_with_sessions(root, "aaaaaaaa-0000-0000-0000-000000000002", {
        "inspect_only": {"session_id": "inspect_only", "runtime": "docker",
                         "started_at": None, "ended_at": None},
    })
    e = _entry(root, "aaaaaaaa-0000-0000-0000-000000000002", monkeypatch)
    assert e.last_session_at == "", (
        f"a session directory left by `inspect` — which launches nothing — was "
        f"reported as the project having run: {e.last_session_at!r}")


def test_the_newest_STARTED_session_wins_not_the_newest_directory(tmp_path, monkeypatch):
    """Both bugs together: an inspect-only dir written LAST must not mask the
    real session that ran before it."""
    root = tmp_path / "state_root"
    uuid = "aaaaaaaa-0000-0000-0000-000000000003"
    _project_with_sessions(root, uuid, {
        "real":    {"session_id": "real", "runtime": "apptainer",
                    "started_at": "2026-09-19T08:00:00Z", "ended_at": None},
        "zz_late": {"session_id": "zz_late", "runtime": "docker",
                    "started_at": None, "ended_at": None},
    })
    # make the inspect-only directory the newest on disk
    import os, time
    late = root / "state" / uuid / "sessions" / "zz_late"
    os.utime(late, (time.time() + 60, time.time() + 60))

    e = _entry(root, uuid, monkeypatch)
    assert e.last_session_at == "2026-09-19T08:00:00Z", (
        f"the newest DIRECTORY won over the newest actual session: "
        f"{e.last_session_at!r}")
    assert e.last_session_runtime == "apptainer", (
        f"the runtime came from the wrong session: {e.last_session_runtime!r}")


def test_the_latest_start_wins_among_several_real_sessions(tmp_path, monkeypatch):
    root = tmp_path / "state_root"
    uuid = "aaaaaaaa-0000-0000-0000-000000000004"
    _project_with_sessions(root, uuid, {
        "older": {"session_id": "older", "runtime": "docker",
                  "started_at": "2026-09-01T00:00:00Z", "ended_at": "2026-09-01T01:00:00Z"},
        "newer": {"session_id": "newer", "runtime": "apptainer",
                  "started_at": "2026-09-20T00:00:00Z", "ended_at": None},
    })
    e = _entry(root, uuid, monkeypatch)
    assert e.last_session_at == "2026-09-20T00:00:00Z"
    assert e.last_session_runtime == "apptainer"


def test_an_unknown_end_is_reported_as_unknown_not_as_running(tmp_path, monkeypatch):
    """THE MAINTAINER'S POINT, pinned.

    A session killed by a crash, `kill -9`, `scancel` or a laptop shutting down
    writes no `ended_at` and nothing later is obliged to. `status` reconciles
    only in non-JSON mode. So null must be readable as "we do not know", and a
    consumer must never infer "still running" from it — that would show a
    crashed session as live forever.
    """
    root = tmp_path / "state_root"
    uuid = "aaaaaaaa-0000-0000-0000-000000000005"
    _project_with_sessions(root, uuid, {
        "crashed": {"session_id": "crashed", "runtime": "apptainer",
                    "started_at": "2026-09-20T10:00:00Z", "ended_at": None},
    })
    e = _entry(root, uuid, monkeypatch)
    assert e.last_session_at == "2026-09-20T10:00:00Z", "the start is knowable"
    assert e.last_session_ended_at == "", (
        "an unknown end must be reported as unknown")


def test_a_clean_end_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "state_root"
    uuid = "aaaaaaaa-0000-0000-0000-000000000006"
    _project_with_sessions(root, uuid, {
        "clean": {"session_id": "clean", "runtime": "docker",
                  "started_at": "2026-09-20T10:00:00Z",
                  "ended_at": "2026-09-20T12:00:00Z"},
    })
    e = _entry(root, uuid, monkeypatch)
    assert e.last_session_ended_at == "2026-09-20T12:00:00Z"


def test_the_json_surface_carries_both(tmp_path, monkeypatch):
    """The dashboard reads `list --json`, so the fields must reach it."""
    from click.testing import CliRunner
    from botainer.cli.main import cli
    root = tmp_path / "state_root"
    uuid = "aaaaaaaa-0000-0000-0000-000000000007"
    _project_with_sessions(root, uuid, {
        "s": {"session_id": "s", "runtime": "docker",
              "started_at": "2026-09-20T10:00:00Z", "ended_at": None},
    })
    monkeypatch.setenv("MY_BOTAINER", str(root))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    out = CliRunner().invoke(cli, ["list", "--json"]).output
    rows = [r for r in json.loads(out) if r["uuid"] == uuid]
    assert rows, "the project did not reach the JSON surface"
    assert rows[0]["last_session_at"] == "2026-09-20T10:00:00Z"
    assert "last_session_ended_at" in rows[0], (
        "the JSON surface does not expose the end time at all, so a consumer "
        "cannot tell a clean end from an unknown one")
