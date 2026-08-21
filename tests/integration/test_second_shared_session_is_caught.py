"""A second concurrent shared-auth session must not start silently.

Shared mode holds ONE credential. Refreshing mints a new refresh token and
REVOKES the old one (measured), so two live sessions log each other
out — whichever refreshes first wins, and the loser finds out hours later as an
"expired" token that reads like a server fault.

Nothing checked for this until, and the constraint appeared on NO
forward-facing surface: not the start banner, not `auth use`, not `init`, not
one shipped doc. Only `auth doctor` said it, to a user already broken. The
maintainer — who wrote the mode — did not know it existed.

That last fact is why these tests are about a CHECK and not about wording. A
warning nobody could have authored from knowledge was never going to be
written, so the fix has to come from state the launcher already records.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from botainer.state import credential_events as ce


class _FakePaths:
    def __init__(self, root: Path):
        self.state_dir = root

    def for_project(self, uuid):
        return _FakeProj(self.state_dir / uuid)


class _FakeProj:
    def __init__(self, base: Path):
        self.sessions_dir = base / "sessions"


def _session(tmp, uuid, *, plugins, alive, ended=None, root="/p", host="h1",
             runtime="docker"):
    sid = (uuid * 8)[:16]          # session ids must be hex 8-64
    d = tmp / uuid / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    handle = ({"docker": {"container_id": (uuid * 16)[:24]}} if runtime == "docker"
              else {"apptainer": {"instance_name": "bot-" + sid,
                                  "slurm_jobid": "12345678"}})
    (d / "spec.json").write_text(json.dumps({
        "session_id": sid, "project_uuid": uuid, "project_root": root,
        "runtime": runtime, "image": "img", "host": host,
        "spec": {"plugins_enabled": plugins},
        "started_at": "2026-08-17T10:00:00Z", "ended_at": ended,
        "runtime_handle": handle,
        "schema_version": 1,
    }))
    return d


@pytest.fixture()
def live(monkeypatch):
    """Make every session record report alive unless the test says otherwise."""
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)


def test_a_live_shared_session_elsewhere_is_found(tmp_path, live):
    # Apptainer+Slurm, on another login node: squeue is cluster-wide, so this
    # IS verifiable cross-host — unlike docker, which is host-local.
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared", "git"], alive=True,
             root="/work/other-project", host="login1", runtime="apptainer")

    holders = ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="bbbb")

    assert len(holders) == 1, holders
    assert holders[0]["project_root"] == "/work/other-project"
    assert holders[0]["host"] == "login1", (
        "the warning must name WHERE — on a cluster the other session is "
        "often on a different login node, which is why it is invisible")


def test_this_project_is_not_reported_against_itself(tmp_path, live):
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True)

    holders = ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="aaaa")

    assert holders == [], "restarting your own project must not warn"


def test_an_isolated_session_elsewhere_does_not_warn(tmp_path, live):
    """Isolated projects have their own credential. They do not contend, and
    warning about them would be the false alarm that gets the whole channel
    ignored."""
    _session(tmp_path, "aaaa", plugins=["agent-claude", "git"], alive=True)

    assert ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="z") == []


def test_a_broker_session_elsewhere_does_not_warn(tmp_path, live):
    """Broker never holds the refresh token, so it cannot revoke anyone."""
    _session(tmp_path, "aaaa", plugins=["agent-claude-broker"], alive=True)

    assert ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="z") == []


def test_an_ENDED_session_does_not_warn(tmp_path, live):
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True,
             ended="2026-08-17T11:00:00Z")

    assert ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="z") == [], (
        "a finished session still holds a record; warning about it would fire "
        "on every launch, which trains the user to ignore the warning")


def test_a_DEAD_session_does_not_warn(tmp_path, monkeypatch):
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: False)
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=False)

    assert ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="z") == []


def test_unreadable_bookkeeping_never_blocks_a_launch(tmp_path, live):
    """Fail OPEN. A missed warning costs a re-login; a launch refused because
    a record would not parse costs the user their session for no reason."""
    d = tmp_path / "aaaa" / "sessions" / "abcdef0123456789"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{ not json")

    assert ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="z") == []


def test_the_start_banner_leads_with_the_constraint():
    """The banner is on EVERY launch and used to sell the mode on the one
    thing it cannot do: 'One login, used by every shared-mode project'.

    Asserted on the emitted string, not on source text.
    """
    import inspect

    from botainer.cli import start as start_cli

    src = inspect.getsource(start_cli.start.callback)
    banner = src[src.index("Auth mode: SHARED"):src.index("Auth mode: SHARED") + 400]
    assert "ONE SESSION AT A TIME" in banner, (
        "the start banner does not state the defining constraint of the mode")
    assert "used by every shared-mode " "project on this machine" not in banner, (
        "the banner still leads with the sharing claim that mis-sold the mode")


# ── the part that was UNTESTED, which is how a crash shipped two days ago ──


def _real_holder(monkeypatch, holders):
    from botainer.state import credential_events as _ce
    monkeypatch.setattr(_ce, "live_shared_holders", lambda *a, **k: holders)


def _fake_project(tmp_path):
    from botainer.core import identity
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111")
    return proj


HOLDER = [{"uuid": "aaaa", "project_root": "/work/other", "host": "login1",
           "session_id": "abcdef0123456789", "started_at": "2026-08-17T10:00Z"}]


def test_the_prompt_actually_runs_and_names_the_other_project(
        tmp_path, monkeypatch, capsys):
    """RUNS the function. My first version of this feature had NO test that
    executed it — the same gap that shipped a dispatcher which died on its
    first line with the whole suite green."""
    from botainer.cli import _common as c
    _real_holder(monkeypatch, HOLDER)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("click.confirm", lambda *a, **k: True)

    c.confirm_no_other_shared_session(_fake_project(tmp_path))

    err = capsys.readouterr().err
    assert "/work/other" in err, f"did not name the other project: {err!r}"
    assert "login1" in err, "did not say which host — the usual reason it is invisible"
    assert "auth use isolated" in err, "did not say how to fix it"


def test_answering_no_stops_the_launch(tmp_path, monkeypatch):
    """The refusal path, executed. Previously assumed to work."""
    from botainer.cli import _common as c
    _real_holder(monkeypatch, HOLDER)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("click.confirm", lambda *a, **k: False)

    with pytest.raises(SystemExit) as ei:
        c.confirm_no_other_shared_session(_fake_project(tmp_path))
    assert "keeps the credential" in str(ei.value)


def test_answering_yes_proceeds(tmp_path, monkeypatch):
    from botainer.cli import _common as c
    _real_holder(monkeypatch, HOLDER)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("click.confirm", lambda *a, **k: True)

    c.confirm_no_other_shared_session(_fake_project(tmp_path))   # no raise


def test_non_interactive_warns_but_never_hangs(tmp_path, monkeypatch, capsys):
    """A batch job on a login node cannot answer a prompt. Blocking there
    would hang until the walltime expires — worse than the problem."""
    from botainer.cli import _common as c
    _real_holder(monkeypatch, HOLDER)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("prompted with no tty — this would hang a job")
    monkeypatch.setattr("click.confirm", _boom)

    c.confirm_no_other_shared_session(_fake_project(tmp_path))
    assert "continuing" in capsys.readouterr().err


def test_no_holders_says_nothing_at_all(tmp_path, monkeypatch, capsys):
    from botainer.cli import _common as c
    _real_holder(monkeypatch, [])

    c.confirm_no_other_shared_session(_fake_project(tmp_path))

    assert capsys.readouterr().err == "", "warned when nothing was running"


def test_both_start_and_hpc_submit_call_the_same_helper():
    """Sibling drift, as a test. The check was added to start.py and NOT to
    `hpc submit` — the guard on the laptop path, nothing on the cluster path,
    which is the product. Asserted with AST so a comment cannot satisfy it."""
    import ast
    import inspect

    from botainer.cli import hpc as hpc_cli
    from botainer.cli import start as start_cli

    for mod, label in ((start_cli, "botainer start"), (hpc_cli, "hpc submit")):
        calls = [
            n for n in ast.walk(ast.parse(inspect.getsource(mod)))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "confirm_no_other_shared_session"
        ]
        assert calls, (
            f"{label} does not call confirm_no_other_shared_session — a "
            f"shared-auth session can start there with no check")


def test_a_docker_record_from_another_machine_is_ignored(tmp_path, monkeypatch):
    """CRY-WOLF GUARD. is_session_alive answers 'cannot verify' with TRUE.
    Grace has no docker, so one stale docker record would report alive forever
    and warn on EVERY start — training the user to ignore the channel."""
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True,
             host="somebody-elses-laptop")

    assert ce.live_shared_holders(_FakePaths(tmp_path), exclude_uuid="z") == [], (
        "a docker session recorded on a DIFFERENT machine was treated as live")
