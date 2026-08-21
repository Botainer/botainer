"""The credential event log: never raises, never leaks, never wedges a session.

This is diagnostics, so its failure modes matter more than its happy path. A
logger that throws takes down a session it was only supposed to observe, and a
logger that records a token is worse than no logger at all.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from botainer.state import credential_events as ce

TS = "2026-08-01T03:00:00Z"


def _rec(root, **kw):
    kw.setdefault("event", ce.Event.LINKED)
    kw.setdefault("agent", "agent-claude")
    kw.setdefault("profile", "default")
    kw.setdefault("project_uuid", "aaaa-1111")
    kw.setdefault("timestamp", TS)
    ce.record(root, **kw)


def test_records_and_reads_back(tmp_path):
    _rec(tmp_path, event=ce.Event.ROTATION_PROMOTED, verdict=ce.Verdict.MATCH)
    events = ce.read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["event"] == "rotation-promoted"
    assert events[0]["verdict"] == "match"
    assert events[0]["ts"] == TS


def test_the_file_is_host_private(tmp_path):
    """0600, in a 0700 dir. It records where credentials moved; that is not
    world-readable information."""
    _rec(tmp_path)
    p = ce.log_path(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_lives_outside_every_container_bind(tmp_path):
    """`logs/` is a sibling of `state/` and `shared-auth/`, never inside either.

    Both of those are bound into containers. A log an agent can rewrite is
    worse than no log, because it reads as evidence. Same rule as the SLURM
    output dir.
    """
    p = ce.log_path(tmp_path)
    parts = p.relative_to(tmp_path).parts
    assert parts[0] == "logs"
    assert "state" not in parts and "shared-auth" not in parts


def test_never_raises_when_the_directory_cannot_be_created(tmp_path):
    # A file where the logs/ dir should be: mkdir will fail.
    (tmp_path / "logs").write_text("not a directory")
    _rec(tmp_path)          # must not raise
    assert ce.read_events(tmp_path) == []


def test_never_raises_when_the_log_is_unwritable(tmp_path):
    _rec(tmp_path)
    p = ce.log_path(tmp_path)
    p.chmod(0o400)
    try:
        _rec(tmp_path, event=ce.Event.ACCOUNT_CHANGE_HELD)   # must not raise
    finally:
        p.chmod(0o600)


def test_stops_appending_rather_than_filling_the_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "_MAX_BYTES", 200)
    for _ in range(50):
        _rec(tmp_path)
    size = ce.log_path(tmp_path).stat().st_size
    assert size < 1000, f"log grew to {size} bytes despite the cap"


def test_a_malformed_line_does_not_hide_the_good_ones(tmp_path):
    _rec(tmp_path)
    with ce.log_path(tmp_path).open("a") as fh:
        fh.write("{ this is not json\n")
    _rec(tmp_path, event=ce.Event.IDENTITY_RESTORED)
    events = ce.read_events(tmp_path)
    assert [e["event"] for e in events] == ["linked", "identity-restored"]


def test_read_of_a_missing_log_is_empty_not_an_error(tmp_path):
    assert ce.read_events(tmp_path) == []


def test_there_is_no_way_to_pass_a_token(tmp_path):
    """Enforced by construction, not by review.

    `record()` takes named, enumerated arguments. There is no free-form dict,
    so a caller cannot accidentally shovel a credential blob in. This test pins
    the signature so adding a `**extra` later is a deliberate, visible act.
    """
    import inspect
    sig = inspect.signature(ce.record)
    assert not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    ), "record() gained **kwargs — a credential can now be logged by accident"
    assert set(sig.parameters) == {
        "state_root", "event", "agent", "profile", "project_uuid",
        "verdict", "detail", "timestamp",
    }


def test_detail_is_truncated_so_a_line_stays_atomic(tmp_path):
    """Concurrent sessions rely on a single write() under PIPE_BUF (4096) to
    interleave whole lines. An unbounded `detail` would break that."""
    _rec(tmp_path, detail="x" * 10_000)
    raw = ce.log_path(tmp_path).read_text()
    assert len(raw) < 4096, "a single event line exceeded PIPE_BUF"
    assert json.loads(raw)["detail"] == "x" * 200


def test_concurrent_appends_do_not_corrupt_each_other(tmp_path):
    """Two sessions on one host reconciling at the same time is normal."""
    pids = []
    for i in range(8):
        pid = os.fork()
        if pid == 0:
            try:
                for _ in range(20):
                    _rec(tmp_path, project_uuid=f"proj-{i}", detail="c" * 150)
            finally:
                os._exit(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)

    raw = ce.log_path(tmp_path).read_text().splitlines()
    assert len(raw) == 160
    for line in raw:
        json.loads(line)     # every line individually valid = no interleaving


def test_summarise_counts_by_event(tmp_path):
    _rec(tmp_path, event=ce.Event.ROTATION_PROMOTED)
    _rec(tmp_path, event=ce.Event.ROTATION_PROMOTED)
    _rec(tmp_path, event=ce.Event.ACCOUNT_CHANGE_HELD)
    counts = ce.summarise(ce.read_events(tmp_path))
    assert counts == {"rotation-promoted": 2, "account-change-held": 1}


def test_every_event_member_has_a_distinct_value():
    values = [e.value for e in ce.Event]
    assert len(values) == len(set(values))
    for v in values:
        assert v.islower() and " " not in v


@pytest.mark.parametrize("bad", [ce.Verdict.UNREADABLE, ce.Verdict.NO_RECORD])
def test_verdicts_that_mean_we_could_not_verify_are_distinguishable(tmp_path, bad):
    """"could not check" must never be recorded as "checked and fine" — the
    whole design turns on absence-of-evidence not being permission."""
    _rec(tmp_path, verdict=bad)
    assert ce.read_events(tmp_path)[0]["verdict"] == bad.value
    assert bad.value != ce.Verdict.MATCH.value
