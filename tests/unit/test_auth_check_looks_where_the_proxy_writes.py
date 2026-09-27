"""`auth check` must verify the audit log the proxy actually writes.

WHAT IT DID. It read `<project>/data/agent-claude-proxy/audit.jsonl`. The proxy
writes `<project>/sessions/<session_id>/proxy-audit.jsonl` — wrong directory AND
wrong filename. Nothing has ever written the old path, so the check could not
pass for anybody, ever.

MEASURED, with a genuinely valid three-entry hash chain sitting in the real
location:

    $ botainer auth check
    ✗ audit log absent at …/data/agent-claude-proxy/audit.jsonl; cannot attest
      (If agent-claude-proxy isn't configured for this project,
       this is expected; nothing to verify. Otherwise: tampering.)
    exit 5

An integrity command that cannot see an intact chain, and whose only other
suggestion is tampering, manufactures an incident out of a healthy install. That
is the worst direction for a false alarm.

THE SAME WRONG BELIEF WAS WRITTEN DOWN TWICE. A comment in the login path
claimed the proxy login flow "writes audit records under
data/<plugin>/audit.jsonl per project". There is no login hook — the plugin's
`login` command dispatches to its pre_session script — and nothing under that
plugin writes a per-project file of that name. Both were corrected together; had
only the path been fixed, the next reader would have re-derived it from the
comment.

ONE LOG PER SESSION, so the command attests over a SET and says how big it is.
"3 of 4 verified" is actionable; a bare pass/fail cannot express which session
failed, and a single-file check could not report the denominator at all.

AND ABSENCE IS STILL NOT ATTESTATION. Exit 5 is kept — deleting the log is what
tampering looks like, so "no log" must never read as "verified". What changed is
that the message now names the evidence that separates the two cases (are there
session directories at all?) instead of offering "tampering" as the alternative
to "not configured".
"""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest
from click.testing import CliRunner

from botainer.cli.auth import auth
from botainer.core import config as config_module
from botainer.core import identity
from botainer.state import dir as state_dir


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    config_module.write_initial_config(proj, agent="claude", force=False)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    uid, _ = identity.resolve_identity(proj, identity_accept=True)
    base = paths.for_project(uid).base
    monkeypatch.chdir(proj)

    def _chain(session: str, entries: int = 3, tamper: bool = False):
        """A hash chain in the REAL location, built the way the proxy builds it:
        each entry's `prev_hash` is the sha256 of the previous RAW LINE."""
        path = base / "sessions" / session / "proxy-audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        prev = "0" * 64
        with open(path, "wb") as f:
            for i in range(entries):
                line = json.dumps({"ts": i, "prev_hash": prev}).encode()
                f.write(line + b"\n")
                prev = hashlib.sha256(line).hexdigest()
                if tamper and i == 0:
                    prev = "f" * 64          # break the link to the next entry
        return path

    def _run():
        return CliRunner().invoke(auth, ["check"])

    return base, _chain, _run


def test_a_valid_chain_where_the_proxy_writes_it_verifies(project):
    """THE DEFECT. This exact fixture used to exit 5 "audit log absent"."""
    _, chain, run = project
    chain("sess-good")

    res = run()

    assert res.exit_code == 0, (
        f"a genuinely valid chain was not attested (exit {res.exit_code}):\n"
        f"{res.output}")
    assert "verified" in res.output, f"no verification reported:\n{res.output}"


def test_a_tampered_chain_fails_and_names_the_session(project):
    """The half that makes a pass mean something."""
    _, chain, run = project
    chain("sess-bad", tamper=True)

    res = run()

    assert res.exit_code == 2, (
        f"a broken hash chain did not fail (exit {res.exit_code}):\n"
        f"{res.output}")
    assert "sess-bad" in res.output, (
        f"the failing session is not named, so the reader cannot find it:\n"
        f"{res.output}")


def test_one_bad_chain_among_several_still_fails_and_reports_the_denominator(project):
    """A per-session log means the answer is a fraction, not a boolean. Without
    the denominator a reader cannot tell whether the others were even read."""
    _, chain, run = project
    chain("sess-good")
    chain("sess-bad", tamper=True)

    res = run()

    assert res.exit_code == 2, f"a mixed result passed:\n{res.output}"
    assert "1 of 2" in res.output, (
        f"the output does not say how many chains were checked:\n{res.output}")


def test_no_sessions_at_all_says_the_proxy_never_ran(project):
    """Absence is not attestation — exit 5 is kept. But this case is NOT
    suspicious, and saying "otherwise: tampering" at it was the false alarm."""
    _, _, run = project

    res = run()

    assert res.exit_code == 5, f"absence was treated as a verdict:\n{res.output}"
    assert "no session directories" in res.output, (
        f"the empty case does not distinguish itself:\n{res.output}")


def test_sessions_without_logs_names_what_would_distinguish_the_two_cases(project):
    """THE HONEST MIDDLE. Sessions exist but carry no log: either the proxy was
    not enabled for them, or the logs are gone. The command cannot tell, and
    must say which evidence decides it rather than guessing."""
    base, _, run = project
    (base / "sessions" / "sess-empty").mkdir(parents=True, exist_ok=True)

    res = run()

    assert res.exit_code == 5
    assert "1 session director" in res.output, (
        f"the count of sessions is what separates 'never ran' from 'logs "
        f"removed', and it is not reported:\n{res.output}")


def test_the_old_path_is_not_consulted(project):
    """OPPOSITE DIRECTION, and it pins the actual bug rather than its symptom.

    A file at the OLD location must not be able to satisfy the check — if it
    could, the command would still be attesting something nothing writes, and
    a planted file there would forge a clean bill.
    """
    base, _, run = project
    old = base / "data" / "agent-claude-proxy" / "audit.jsonl"
    old.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": 0, "prev_hash": "0" * 64}).encode()
    old.write_bytes(line + b"\n")

    res = run()

    # ASSERT ON THE OUTPUT, NOT ONLY THE EXIT CODE. An exit-5-only assertion
    # stays green if the behaviour is deleted — exit 5 is also what an ordinary
    # empty project returns, so it cannot distinguish "ignored the planted file"
    # from "there was nothing anywhere". The blocking gate caught this, and it
    # was right to.
    assert res.exit_code == 5, (
        f"a file at the abandoned path produced a verdict (exit "
        f"{res.exit_code}), so the check still reads a location nothing "
        f"writes:\n{res.output}")
    assert "verified" not in res.output, (
        f"the planted file at the abandoned path was ATTESTED — a forged clean "
        f"bill:\n{res.output}")
    assert "no proxy audit log" in res.output, (
        f"the planted file changed what is reported, so the old path is still "
        f"being consulted:\n{res.output}")
    assert "/sessions/" in res.output and "proxy-audit.jsonl" in res.output, (
        f"the output does not name the location it actually searched, so a "
        f"reader cannot tell which path was used:\n{res.output}")
    assert "data/agent-claude-proxy" not in res.output, (
        f"the abandoned path is still named to the user:\n{res.output}")
