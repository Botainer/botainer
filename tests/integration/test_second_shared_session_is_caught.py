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

    holders = ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="bbbb")

    assert len(holders) == 1, holders
    assert holders[0]["project_root"] == "/work/other-project"
    assert holders[0]["host"] == "login1", (
        "the warning must name WHERE — on a cluster the other session is "
        "often on a different login node, which is why it is invisible")


def test_this_project_is_not_reported_against_itself(tmp_path, live):
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True)

    holders = ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="aaaa")

    assert holders == [], "restarting your own project must not warn"


def test_an_isolated_session_elsewhere_does_not_warn(tmp_path, live):
    """Isolated projects have their own credential. They do not contend, and
    warning about them would be the false alarm that gets the whole channel
    ignored."""
    _session(tmp_path, "aaaa", plugins=["agent-claude", "git"], alive=True)

    assert ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="z") == []


def test_a_broker_session_elsewhere_does_not_warn(tmp_path, live):
    """Broker never holds the refresh token, so it cannot revoke anyone."""
    _session(tmp_path, "aaaa", plugins=["agent-claude-broker"], alive=True)

    assert ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="z") == []


def test_an_ENDED_session_does_not_warn(tmp_path, live):
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True,
             ended="2026-08-17T11:00:00Z")

    assert ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="z") == [], (
        "a finished session still holds a record; warning about it would fire "
        "on every launch, which trains the user to ignore the warning")


def test_a_DEAD_session_does_not_warn(tmp_path, monkeypatch):
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: False)
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=False)

    assert ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="z") == []


def test_unreadable_bookkeeping_never_blocks_a_launch(tmp_path, live):
    """Fail OPEN. A missed warning costs a re-login; a launch refused because
    a record would not parse costs the user their session for no reason."""
    d = tmp_path / "aaaa" / "sessions" / "abcdef0123456789"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{ not json")

    assert ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="z") == []


def test_the_start_banner_leads_with_the_constraint():
    """The banner is on EVERY launch and used to sell the mode on the one
    thing it cannot do: 'One login, used by every shared-mode project'.

    THIS TEST USED TO LIE ABOUT ITSELF. Its docstring said "Asserted on the
    emitted string, not on source text", and the body did
    `inspect.getsource(start.callback)` and grepped it — a docstring in
    `start()` would have satisfied it. The assertion-shapes gate could not see
    that, because the grep went through an intermediate variable whose name was
    not on its list (#199, fixed the same day).

    The banner is now a pure function returning its lines, so this calls it.
    """
    from botainer.cli.start import shared_mode_banner

    lines = shared_mode_banner()
    assert lines, "the banner is empty"
    assert "ONE SESSION AT A TIME" in lines[0], (
        "the banner does not LEAD with the defining constraint of the mode; "
        f"its first line is {lines[0][:80]!r}")
    assert not any("used by every shared-mode project on this machine" in ln
                   for ln in lines), (
        "the banner still carries the sharing claim that mis-sold the mode")


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

    c.confirm_no_other_shared_session(_fake_project(tmp_path), agent_family="claude")

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
        c.confirm_no_other_shared_session(_fake_project(tmp_path), agent_family="claude")
    assert "keeps the credential" in str(ei.value)


def test_answering_yes_proceeds(tmp_path, monkeypatch):
    from botainer.cli import _common as c
    _real_holder(monkeypatch, HOLDER)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("click.confirm", lambda *a, **k: True)

    c.confirm_no_other_shared_session(_fake_project(tmp_path), agent_family="claude")   # no raise


def test_non_interactive_warns_but_never_hangs(tmp_path, monkeypatch, capsys):
    """A batch job on a login node cannot answer a prompt. Blocking there
    would hang until the walltime expires — worse than the problem."""
    from botainer.cli import _common as c
    _real_holder(monkeypatch, HOLDER)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("prompted with no tty — this would hang a job")
    monkeypatch.setattr("click.confirm", _boom)

    c.confirm_no_other_shared_session(_fake_project(tmp_path), agent_family="claude")
    assert "continuing" in capsys.readouterr().err


def test_no_holders_says_nothing_at_all(tmp_path, monkeypatch, capsys):
    from botainer.cli import _common as c
    _real_holder(monkeypatch, [])

    c.confirm_no_other_shared_session(_fake_project(tmp_path), agent_family="claude")

    assert capsys.readouterr().err == "", "warned when nothing was running"


def test_hpc_submit_ACTUALLY_WARNS_not_merely_calls_the_helper(
        tmp_path, monkeypatch):
    """PRESENCE IS NOT EFFECT, and the old version of this test proved it.

    It walked the AST of `cli/hpc.py` looking for a call to
    `confirm_no_other_shared_session` and asserted one existed. One did. It was
    also DEAD: the surrounding block resolved the project's plugin list through
    `config.load_project_config`, a name that does not exist on that module
    (`load_config` does), so the `AttributeError` was raised before the argument
    was even evaluated and swallowed by a blanket `except Exception`. `_fam`
    was therefore ALWAYS None and the guard NEVER RAN on the cluster path — the
    exact sibling drift this file was written to prevent, re-introduced by the
    commit that fixed it, and invisible to a test that asked only whether the
    call was written down.

    So this one drives `botainer hpc submit` and asserts the SENTENCE the user
    would see. A rename back to a nonexistent attribute fails it.
    """
    from click.testing import CliRunner

    from botainer.core import identity
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)

    # A .sif must EXIST or compose refuses before the guard is reached — which
    # is itself a queue item: three surfaces that launch nothing are blocked by
    # a check that exists for launching. Contents are irrelevant; the resolver
    # tests is_file() and nothing more.
    sif = paths.apptainer_sif_path("agent-claude")
    sif.parent.mkdir(parents=True, exist_ok=True)
    sif.write_bytes(b"NOT-A-REAL-SIF" * 100)

    # Shared mode's pre_session refuses without a credential, so the fixture
    # has to be a REAL install, not the minimum that reaches the code path.
    shared = paths.root / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True, exist_ok=True)
    cred = shared / ".credentials.json"
    cred.write_text('{"claudeAiOauth":{"accessToken":"sk-ant-oat' + "M" * 70
                    + '","refreshToken":"sk-ant-ort' + "M" * 70
                    + '","expiresAt":9999999999999}}')
    cred.chmod(0o600)

    proj = tmp_path / "proj"
    proj.mkdir()
    from botainer.core import config as cfgm
    cfgm.write_initial_config(proj, agent="claude", force=True)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    import yaml
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["plugins_enabled"] = ["agent-claude-shared"]
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))

    # Another project holding the shared claude credential RIGHT NOW.
    # APPTAINER, not docker, and that is not incidental: `live_shared_holders`
    # deliberately skips a docker record when `docker` is not on PATH, because a
    # login node has none and one stale record would otherwise warn forever.
    # That cry-wolf guard is correct; a docker fixture here would be testing the
    # wrong thing. Apptainer+Slurm is cluster-wide, so it is trusted across
    # hosts — which is exactly the case `hpc submit` cares about.
    _session(paths.state_dir, "b" * 8, plugins=["agent-claude-shared"],
             alive=True, runtime="apptainer", host="some-other-login-node")
    monkeypatch.setattr(
        "botainer.state.liveness.is_session_alive", lambda rec: True)

    monkeypatch.chdir(proj)
    res = CliRunner().invoke(
        __import__("botainer.cli.main", fromlist=["cli"]).cli,
        ["hpc", "submit", "--dry-run", "--time", "60",
         "--partition", "day", "--account", "acct"])

    assert "already using the shared" in res.output, (
        "`hpc submit` did not warn about a second concurrent shared-auth "
        "session. The guard is present in the source and dead at runtime — "
        f"which is what an AST test cannot see.\n{res.output}")


def test_a_docker_record_from_another_machine_is_ignored(tmp_path, monkeypatch):
    """CRY-WOLF GUARD. is_session_alive answers 'cannot verify' with TRUE.
    Grace has no docker, so one stale docker record would report alive forever
    and warn on EVERY start — training the user to ignore the channel."""
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)
    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True,
             host="somebody-elses-laptop")

    assert ce.live_shared_holders(_FakePaths(tmp_path), agent_family="claude", exclude_uuid="z") == [], (
        "a docker session recorded on a DIFFERENT machine was treated as live")


def test_a_live_claude_session_does_not_block_starting_codex(tmp_path):
    """A Claude shared session must not produce a Codex credential collision.

    The stores are per agent family: shared-auth/agent-claude and
    shared-auth/agent-codex use separate provider credentials. Matching every
    agent-*-shared plugin would report an unrelated session as a conflicting holder.
    The liveness scan must filter by the requested family."""
    from botainer.state import credential_events as ce

    _session(tmp_path, "aaaa", plugins=["agent-claude-shared"], alive=True,
             root="/work/claude-project", host="login1", runtime="apptainer")

    assert ce.live_shared_holders(
        _FakePaths(tmp_path), agent_family="codex", exclude_uuid="zzzz") == [], (
        "a live agent-claude-shared session was reported as a conflict for a "
        "CODEX launch — different provider, different credential, different "
        "account. Nothing codex does can log that session out."
    )
    # ...and the same records still conflict for the agent they belong to.
    assert ce.live_shared_holders(
        _FakePaths(tmp_path), agent_family="claude", exclude_uuid="zzzz"), (
        "scoping the scan to one agent broke the case it exists for"
    )
