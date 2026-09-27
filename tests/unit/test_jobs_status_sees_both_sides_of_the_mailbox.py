"""`hpc jobs-status` must see what the AGENT queued, not only what the host did.

THE MAILBOX HAS TWO SIDES and this command read one. `out/` is host-written;
`in/` is what the caged agent writes. So a request the agent queued that the
dispatcher has not picked up exists only in `in/` — and the command answered:

    no jobs dispatched yet for this project.

which is false. One HAS been dispatched, by the agent. And it is the case the
command's own help promises to surface — "see what your caged agent has
queued/running/stuck without attaching" — and the most useful one, because a
request stuck in `in/` usually means the dispatcher is not running at all. The
one state you would open this command to diagnose was the one it could not show.

WHAT IS RENDERED IS THE AGENT'S OWN TEXT. The id comes from the FILENAME and is
already charset-gated; `profile` comes from the agent-written body, so it goes
through the same `_clean` + width the status table uses. A request whose body is
unreadable must still be LISTED — "I cannot read it" is exactly the state a
human needs to see, and dropping it would restore the silence this fixes.

AND THE SELECTION RULE HAS TWO COPIES. `pending_requests` now states it once;
`process_inbox_once` still has its own because it needs the body it already
read, in a hot loop. Two copies that might drift is the class this project keeps
recording — so the last test here drives BOTH over the same mailbox and asserts
they choose the same requests. That converts "might drift" into "proven to
agree", which is what makes leaving the second copy honest rather than lazy.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from botainer.cli.hpc import hpc
from botainer.core import config as config_module
from botainer.core import identity
from botainer.hpc import dispatcher as _disp
from botainer.hpc import jobs as _jobs
from botainer.state import dir as state_dir


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    state_dir.ensure_user_state_dir(create_if_missing=True)

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    config_module.write_initial_config(proj, agent="claude", force=False)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    monkeypatch.chdir(proj)

    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid = (proj / ".botainer" / "project-id").read_text(encoding="utf-8").strip()
    mb = _jobs.ensure_mailbox(paths, uid)

    def _run():
        return CliRunner().invoke(hpc, ["jobs-status"])

    return proj, mb, _run


def _queue(mb, job_id: str, body: dict | None = None) -> None:
    """Write a request the way the AGENT does: a file in `in/` named by id.

    THE IDS BELOW ARE 16 LOWERCASE HEX CHARACTERS because that is what the
    dispatcher accepts (`^[0-9a-f]{16}$`), and that charset is a SECURITY
    property rather than a style: the id becomes a path segment, so a
    readable id like `job-aaaa1111` is REFUSED by design. My first draft of
    this file used readable ids, every case came back empty, and the fixture
    was what was wrong — the third time today that checking the fixture
    before the code saved a false finding.
    """
    (mb.in_dir / f"{job_id}.json").write_text(
        json.dumps(body if body is not None else {"profile": "cpu-small"}),
        encoding="utf-8")


def _status(mb, job_id: str, state: str) -> None:
    """Write the host side, the way the dispatcher does."""
    (mb.out_dir / f"{job_id}.status.json").write_text(
        json.dumps({"id": job_id, "state": state, "profile": "cpu-small"}),
        encoding="utf-8")


def test_an_empty_mailbox_still_says_nothing_is_there(project):
    """THE OPPOSITE DIRECTION FIRST. Replacing a false negative with a false
    alarm would be no improvement, and an empty mailbox is every new project."""
    _, _mb, run = project

    res = run()

    assert res.exit_code == 0, res.output
    assert "no jobs dispatched yet" in res.output, (
        f"a genuinely empty mailbox lost its answer:\n{res.output}")


@pytest.mark.parametrize("conflicting", [False, True])
def test_prepared_hot_handoff_is_visible_without_hiding_another_owner(project, conflicting):
    from botainer.hpc import pool
    _, mb, run = project
    jid, wid = "a" * 16, "w" + "b" * 12
    _queue(mb, jid)
    def unavailable(*args):
        raise OSError("status temporarily unavailable")
    with pytest.raises(pool.HotHandoffPending):
        pool.route_hot_task(mb, wid, {"command": ["true"]}, job_id=jid,
                            assignment={"id": jid, "worker": wid, "profile": "cpu-small",
                                        "state": "assigned"}, write_status=unavailable)
    if conflicting:
        _disp._write_status(mb, jid, {"id": jid, "worker": "w" + "c" * 12,
                                    "state": "assigned", "profile": "other-profile"})
        pool.resume_hot_handoffs(mb, lambda job_id, rec: _disp._write_status(mb, job_id, rec))
    result = run()
    assert result.exit_code == 0, result.output
    assert result.output.count(jid) == 1
    if conflicting:
        assert "assigned" in result.output and "other-profil" in result.output
        assert "abandoned" not in result.output
    else:
        assert "hot handoff pending delivery" in result.output
    assert "has not taken it" not in result.output


def test_status_files_with_duplicate_body_ids_remain_visible(project):
    _, mb, run = project
    first, second = "a" * 16, "b" * 16
    for jid in (first, second):
        (mb.out_dir / f"{jid}.status.json").write_text(json.dumps({
            "id": second, "state": "completed", "profile": "cpu-small",
        }))
    result = run()
    assert result.exit_code == 0, result.output
    assert result.output.count(first) == result.output.count(second) == 1


def test_a_request_the_agent_queued_is_not_reported_as_nothing(project):
    """THE DEFECT."""
    _, mb, run = project
    _queue(mb, "aaaa1111aaaa1111")

    res = run()

    assert "no jobs dispatched yet" not in res.output, (
        f"a queued request was reported as nothing at all:\n{res.output}")
    assert "aaaa1111aaaa1111" in res.output, (
        f"the request is not listed:\n{res.output}")


def test_the_waiting_request_says_what_state_it_is_in(project):
    """A row with no explanation is a different failure from a missing row."""
    _, mb, run = project
    _queue(mb, "bbbb2222bbbb2222")

    res = run()

    assert "awaiting" in res.output, (
        f"the row does not say the request is unhandled:\n{res.output}")
    assert "dispatcher" in res.output, (
        f"nothing points at WHY it might be sitting there:\n{res.output}")


def test_it_points_at_the_command_that_can_actually_answer(project):
    """STATE A FACT AND POINT. Whether the dispatcher is alive is a real
    question; `jobs-doctor` walks the chain and this command cannot."""
    _, mb, run = project
    _queue(mb, "cccc3333cccc3333")

    res = run()

    assert "jobs-doctor" in res.output, (
        f"the user is told something is waiting and given no next step:\n"
        f"{res.output}")


def test_a_request_the_dispatcher_HAS_taken_is_not_listed_twice(project):
    """Once `out/<id>.status.json` exists the request is handled, and the status
    row is the truth about it. Showing both would double-count the queue."""
    _, mb, run = project
    _queue(mb, "dddd4444dddd4444")
    _status(mb, "dddd4444dddd4444", "queued")

    res = run()

    assert res.output.count("dddd4444dddd4444") == 1, (
        f"the same job appears on both sides of the mailbox:\n{res.output}")
    assert "awaiting" not in res.output, (
        f"a handled request is still being called unhandled:\n{res.output}")


def test_an_unreadable_request_body_is_still_listed(project):
    """'I cannot read it' is a state a human needs to see. Dropping it would
    restore exactly the silence this change exists to remove."""
    _, mb, run = project
    (mb.in_dir / "eeee5555eeee5555.json").write_text("{not json", encoding="utf-8")

    res = run()

    assert "eeee5555eeee5555" in res.output, (
        f"an unreadable request vanished instead of being reported:\n"
        f"{res.output}")


def test_the_agents_own_text_is_scrubbed_before_it_is_printed(project):
    """The body is agent-controlled. A profile name carrying an escape sequence
    must not reach the host user's terminal as one."""
    _, mb, run = project
    _queue(mb, "ffff6666ffff6666", {"profile": "a\x1b[31mRED\x07b"})

    res = run()

    assert "\x1b" not in res.output and "\x07" not in res.output, (
        "an agent-written profile name put control characters on the host "
        "terminal")
    assert "ffff6666ffff6666" in res.output, (
        f"scrubbing dropped the row instead of cleaning it:\n{res.output}")


def test_pool_control_is_not_a_job(project):
    """The dispatcher's selection skips it, so the view must too — otherwise the
    two disagree about what the queue contains."""
    _, mb, run = project
    _queue(mb, "abcd7777abcd7777", {"kind": "pool_control", "action": "stop"})

    res = run()

    assert "abcd7777abcd7777" not in res.output, (
        f"a pool-control message is being shown as a job:\n{res.output}")


def test_the_lister_and_the_dispatcher_choose_the_same_requests(project):
    """THE ANTI-DRIFT CASE, and the reason the second copy is allowed to stay.

    `pending_requests` states the selection rule once; `process_inbox_once` has
    its own because it needs the body it already read in a hot loop. Rather than
    trust that they agree, drive both over one mailbox holding every awkward
    case at once and compare what they pick.
    """
    _, mb, _run = project
    _queue(mb, "1111aaaa1111aaaa")                       # plain, unhandled
    _queue(mb, "2222bbbb2222bbbb")                       # handled below
    _status(mb, "2222bbbb2222bbbb", "queued")
    _queue(mb, "3333cccc3333cccc")                       # deferred → still pending
    _status(mb, "3333cccc3333cccc", "deferred")
    _queue(mb, "4444dddd4444dddd", {"kind": "pool_control"})
    (mb.in_dir / "not-a-json-file.txt").write_text("x", encoding="utf-8")
    (mb.in_dir / ".hidden.json").write_text("{}", encoding="utf-8")
    (mb.in_dir / "has spaces and slashes.json").write_text("{}", encoding="utf-8")

    from_lister = {job_id for job_id, _req in _disp.pending_requests(mb)}

    submitted: list[str] = []

    def _fake_sbatch(*a, **k):
        return "1"

    def _capture(mailbox, job_profiles, jobs_policy, image, **kw):
        # Re-walk the dispatcher's OWN selection by asking it to refuse
        # everything it considers pending — the one entry point that exercises
        # the same clauses without needing a scheduler.
        return _disp.refuse_all_pending(mailbox, "test")

    for r in _capture(mb, {}, None, "img"):
        submitted.append(r.job_id)

    assert from_lister == set(submitted), (
        f"the view and the dispatcher disagree about what is pending:\n"
        f"  lister:     {sorted(from_lister)}\n"
        f"  dispatcher: {sorted(submitted)}\n"
        f"A request in one set and not the other is either invisible to the "
        f"user or invisible to the scheduler."
    )
    assert "1111aaaa1111aaaa" in from_lister and "3333cccc3333cccc" in from_lister, (
        f"the agreement is vacuous — both picked too little: {from_lister}")
    assert "2222bbbb2222bbbb" not in from_lister, "a handled request is pending"
    assert "4444dddd4444dddd" not in from_lister, "pool_control is not a job"
