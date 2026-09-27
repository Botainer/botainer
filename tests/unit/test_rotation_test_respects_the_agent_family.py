"""`auth rotation-test` must obey the agent family it was given. (queue 25/26/27)

Three defects, one root cause: this command hardcoded Claude's credential shape
instead of reading it from `_AGENT_CREDENTIALS`, which every other part of the
module already goes through. ALL THREE WERE REPRODUCED BY RUNNING the real CLI
against a state root on disk, not by reading the code.

1. IT WROTE CLAUDE'S OAUTH BLOCK INTO CODEX'S CREDENTIAL FILE.
   `arm` forces the next call to refresh by backdating the expiry. Codex's
   auth.json carries no BACKDATABLE expiry: its refresh timing comes from the
   `exp` claim inside the access token, falling back to `last_refresh` + 8 days,
   and `last_refresh` is an RFC3339 string rather than the epoch-ms field this
   command writes. (botainer is not ignorant of codex expiry — the codex broker
   reads it. It is simply not a field `arm` can move.) So
   `doc.setdefault("claudeAiOauth", {})["expiresAt"] = ...` INVENTED one:

       before  {"tokens": {...}, "last_refresh": "..."}
       after   {"tokens": {...}, "last_refresh": "...",
                "claudeAiOauth": {"expiresAt": 1789065788978}}

   It then printed `refresh: e3b0c44298fc   access: e3b0c44298fc` and called
   that ARMED. Both digests are sha256(""), because `_oauth_block` was asked for
   `claudeAiOauth` in a file that has none. A corrupted credential and a
   measurement of nothing, reported as a success.

2. THE SPELLING OF `--agent` CHOSE A DIFFERENT FILE.
   The shared-store path interpolated the raw string, so `--agent claude` built
   `shared-auth/claude/...` — which never exists — fell through, and armed
   whichever PROJECT copy was newest. `--agent agent-claude` armed the shared
   store. Synthetic path example, same root and command:

       --agent claude        -> ~/state/proj1/data/.../default/.credentials.json
       --agent agent-claude  -> ~/shared-auth/agent-claude/.credentials.json

   Not cosmetic: the docstring says arming a project file "measures a credential
   nothing touched", so one spelling silently produces the useless experiment.

3. `restore` — THE DOCUMENTED RECOVERY STEP — DIED WITH A RAW TRACEBACK.
   Arm a project copy, let `pre_session` re-link that project to the shared
   store (which replaces the file with a symlink to the CONTAINER path
   /shared-auth/...), then restore: `FileNotFoundError` out of `shutil.copy2`.

   The obvious fix — resolve the link like `check` does — would have been WORSE
   than the crash. The backup holds the PROJECT's old credential; the link points
   at the SHARED store. Following it overwrites the live shared credential with a
   stale copy of a different one: silent damage, in the command whose entire job
   is undoing damage. So it refuses and writes nothing, which is also the honest
   answer — the backdated file no longer exists, so there is nothing to undo.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.auth_doctor import _ROTATION_STATE
from botainer.cli.main import cli

#: sha256 of the empty string. The digest `arm` printed for both tokens when it
#: read the wrong OAuth block — named here so the assertion says WHY it matters.
_DIGEST_OF_NOTHING = hashlib.sha256(b"").hexdigest()[:12]


def _claude_cred(refresh: str) -> str:
    return json.dumps({"claudeAiOauth": {
        "refreshToken": refresh,
        "accessToken": "access-" + refresh,
        "expiresAt": int((time.time() + 3600) * 1000)}})


def _codex_cred() -> str:
    """A REAL codex auth.json shape: `tokens`, snake_case, and NO expiry.

    The missing expiry is the whole point of the first defect, so a fixture that
    invented one would test nothing.
    """
    return json.dumps({
        "tokens": {"refresh_token": "codex-refresh-AAAA",
                   "access_token": "codex-access-BBBB",
                   "id_token": "codex-id-CCCC"},
        "last_refresh": "2026-09-01T00:00:00Z"})


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """A state root holding BOTH families' shared stores, plus one project."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    for fam in ("agent-claude", "agent-codex"):
        (tmp_path / "shared-auth" / fam).mkdir(parents=True)
    (tmp_path / "shared-auth" / "agent-codex" / "auth.json").write_text(
        _codex_cred())
    proj = (tmp_path / "state" / "11111111-aaaa-bbbb-cccc-000000000001"
            / "data" / "agent-claude" / "profiles" / "default")
    proj.mkdir(parents=True)
    return tmp_path


def _shared_claude(root: Path) -> Path:
    return root / "shared-auth" / "agent-claude" / ".credentials.json"


def _project_claude(root: Path) -> Path:
    return (root / "state" / "11111111-aaaa-bbbb-cccc-000000000001" / "data"
            / "agent-claude" / "profiles" / "default" / ".credentials.json")


def _run(*args):
    return CliRunner().invoke(cli, ["auth", "rotation-test", *args])


def _armed_path(root: Path) -> Path:
    return Path(json.loads((root / _ROTATION_STATE).read_text())["path"])


# ── 1. the foreign-block corruption ──────────────────────────────────────────

def test_arm_refuses_for_a_family_whose_expiry_field_is_unknown(root) -> None:
    res = _run("arm", "--agent", "codex")
    assert res.exit_code != 0, res.output
    assert "cannot arm agent-codex" in res.output, res.output


def test_the_codex_refusal_says_why_and_names_what_does_work(root) -> None:
    """A refusal that does not say what to do instead just moves the problem."""
    out = _run("arm", "--agent", "codex").output
    assert "backdates" in out, out
    assert "--agent claude" in out, out


def test_arm_leaves_the_codex_credential_BYTE_IDENTICAL(root) -> None:
    """The defect: it appended a `claudeAiOauth` block to codex's auth.json."""
    cred = root / "shared-auth" / "agent-codex" / "auth.json"
    before = cred.read_bytes()

    _run("arm", "--agent", "codex")

    assert cred.read_bytes() == before, (
        "the codex credential was modified by a command that refused")
    assert "claudeAiOauth" not in json.loads(cred.read_text()), (
        "Claude's OAuth block was written into a codex credential file")


def test_arm_writes_no_rotation_state_when_it_refuses(root) -> None:
    """Otherwise `check` later reads a record for an experiment never armed."""
    _run("arm", "--agent", "codex")
    assert not (root / _ROTATION_STATE).exists(), (
        "a refused arm left rotation state behind")


# ── 2. the two spellings ─────────────────────────────────────────────────────

@pytest.mark.parametrize("spelling", ["claude", "agent-claude"])
def test_both_spellings_arm_the_same_file(root, spelling) -> None:
    """`claude` used to arm a project copy; `agent-claude` the shared store."""
    _shared_claude(root).write_text(_claude_cred("SHARED-refresh"))
    _project_claude(root).write_text(_claude_cred("PROJECT-refresh"))
    # Make the shared store the OLDER file, so a spelling that misses it falls
    # through to "newest" and picks the project copy — which is what happened.
    os.utime(_shared_claude(root), (0, 0))

    res = _run("arm", "--agent", spelling)

    assert res.exit_code == 0, res.output
    assert _armed_path(root) == _shared_claude(root), (
        f"--agent {spelling} armed {_armed_path(root)}, not the shared store")


def test_the_recorded_digests_are_not_the_digest_of_nothing(root) -> None:
    """`arm` printed sha256("") twice and called it a measurement."""
    _shared_claude(root).write_text(_claude_cred("SHARED-refresh"))

    res = _run("arm", "--agent", "claude")

    rec = json.loads((root / _ROTATION_STATE).read_text())
    assert rec["refresh"] != _DIGEST_OF_NOTHING, (
        "the refresh digest is sha256(''), so no token was actually read")
    assert rec["access"] != _DIGEST_OF_NOTHING, res.output
    assert rec["refresh"] != rec["access"], (
        "both digests are equal, which is what reading an absent block looks "
        "like")


def test_arm_records_the_family_so_check_reads_the_same_block(root) -> None:
    _shared_claude(root).write_text(_claude_cred("SHARED-refresh"))
    _run("arm", "--agent", "claude")
    assert json.loads((root / _ROTATION_STATE).read_text())["agent"] == (
        "agent-claude")


# ── 3. restore ───────────────────────────────────────────────────────────────

def _arm_the_project_copy(root: Path) -> None:
    """Arm the PROJECT file: the only case where re-linking can happen.

    NO `pytest.skip` FALLBACK. If this cannot arm the project copy the three
    restore tests below are meaningless, and a skip would let them report green
    while asserting nothing — which has already happened once in this repo.
    """
    _project_claude(root).write_text(_claude_cred("PROJECT-refresh"))
    res = _run("arm", "--agent", "claude")
    assert res.exit_code == 0, res.output
    assert _armed_path(root) == _project_claude(root), (
        f"setup failed: armed {_armed_path(root)}, wanted the project copy")


def test_restore_refuses_when_the_armed_file_became_a_symlink(root) -> None:
    """The defect: a raw FileNotFoundError traceback out of shutil.copy2."""
    _arm_the_project_copy(root)
    _project_claude(root).unlink()
    os.symlink("/shared-auth/agent-claude/.credentials.json",
               _project_claude(root))

    res = _run("restore", "--agent", "claude")

    assert res.exit_code != 0, res.output
    assert "not the file that was backed up" in res.output, res.output
    assert not isinstance(res.exception, FileNotFoundError), (
        "still dying with the raw traceback")


def test_the_symlink_refusal_does_not_DIAGNOSE_a_cause_it_did_not_see(
        root) -> None:
    """It used to assert `pre_session` had re-linked the project.

    That is one cause among several, and where the store was ALREADY a symlink
    every sentence of it was false. A refusal explaining a cause it never
    observed is a confident wrong answer, which is the failure this repo keeps
    recording.
    """
    _arm_the_project_copy(root)
    _project_claude(root).unlink()
    os.symlink("/shared-auth/agent-claude/.credentials.json",
               _project_claude(root))

    out = _run("restore", "--agent", "claude").output

    assert "pre_session" not in out, out
    assert "re-linked" not in out, out


def test_the_restore_refusal_names_the_backup_it_kept(root) -> None:
    """Refusing without saying where the backup went strands the user."""
    _arm_the_project_copy(root)
    backup = json.loads((root / _ROTATION_STATE).read_text())["backup"]
    _project_claude(root).unlink()
    os.symlink("/shared-auth/agent-claude/.credentials.json",
               _project_claude(root))

    out = _run("restore", "--agent", "claude").output

    assert backup in out, out


def test_restore_does_NOT_write_the_shared_credential_through_the_link(
        root) -> None:
    """Following the link would be worse than the crash it replaced.

    The backup holds the PROJECT's token; the link points at the SHARED store.
    """
    _arm_the_project_copy(root)
    _project_claude(root).unlink()
    os.symlink("/shared-auth/agent-claude/.credentials.json",
               _project_claude(root))
    _shared_claude(root).write_text(_claude_cred("LIVE-SHARED-refresh"))

    _run("restore", "--agent", "claude")

    live = json.loads(_shared_claude(root).read_text())
    assert live["claudeAiOauth"]["refreshToken"] == "LIVE-SHARED-refresh", (
        "the live shared credential was overwritten with the project's stale "
        "copy")


def test_restore_still_works_on_a_plain_file(root) -> None:
    """The ordinary path must stay ordinary, or the guard is just breakage."""
    _shared_claude(root).write_text(_claude_cred("SHARED-refresh"))
    _run("arm", "--agent", "claude")
    backdated = json.loads(_shared_claude(root).read_text())
    assert backdated["claudeAiOauth"]["expiresAt"] < time.time() * 1000, (
        "setup failed: arm did not backdate the expiry")

    res = _run("restore", "--agent", "claude")

    assert res.exit_code == 0, res.output
    after = json.loads(_shared_claude(root).read_text())
    assert after["claudeAiOauth"]["expiresAt"] > time.time() * 1000, (
        "restore did not put the original expiry back")


# ── 4. found by refutation: restore destroyed the login it was undoing ───────
#
# All four below are PRE-EXISTING defects the credential tzar found while trying
# to refute the fix above. They are in this commit because recording a finding
# is not resolving it — and because the first two are worse than anything the
# original three queue rows described.

def test_restore_REFUSES_once_the_refresh_token_has_rotated(root) -> None:
    """THE CRITICAL ONE. `restore` used to copy2 the whole backup back.

    EF-1 measured that a rotated-away refresh token is DEAD (HTTP 400). So
    restore was SAFE exactly when the experiment failed and DESTRUCTIVE exactly
    when it succeeded — and `check` printed "restore if needed" three lines
    under ROTATION CONFIRMED. The product invited the user to log themselves
    out at the one moment it was fatal. In broker mode that file is the
    host-wide shared store, so it is every project at once.
    """
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    # what a real session does: refresh, and the server rotates the token
    _shared_claude(root).write_text(_claude_cred("ROTATED-refresh"))

    res = _run("restore", "--agent", "claude")

    assert res.exit_code != 0, res.output
    live = json.loads(_shared_claude(root).read_text())
    assert live["claudeAiOauth"]["refreshToken"] == "ROTATED-refresh", (
        "restore wrote the pre-rotation token back over a live one — this is "
        "the logout it was supposed to prevent")


def test_that_refusal_explains_the_credential_is_FINE(root) -> None:
    """"Refused" alone reads as breakage and sends the user hunting."""
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    _shared_claude(root).write_text(_claude_cred("ROTATED-refresh"))

    out = _run("restore", "--agent", "claude").output

    assert "fine" in out.lower(), out
    assert "invalidated" in out or "no longer accepts" in out, out


def test_restore_writes_the_expiry_and_NEVER_a_token(root) -> None:
    """arm changes one field, so undoing it needs one field."""
    _shared_claude(root).write_text(_claude_cred("SAME-refresh"))
    _run("arm", "--agent", "claude")
    # the agent rewrote the file, keeping the same token (no refresh yet)
    doc = json.loads(_shared_claude(root).read_text())
    doc["claudeAiOauth"]["accessToken"] = "AGENT-REWROTE-THIS"
    _shared_claude(root).write_text(json.dumps(doc))

    res = _run("restore", "--agent", "claude")

    assert res.exit_code == 0, res.output
    after = json.loads(_shared_claude(root).read_text())
    assert after["claudeAiOauth"]["accessToken"] == "AGENT-REWROTE-THIS", (
        "restore clobbered a field it had no business touching")
    assert after["claudeAiOauth"]["expiresAt"] > time.time() * 1000


def test_check_REFUSES_a_verdict_computed_from_an_absent_token(root) -> None:
    """THE HIGH ONE. An empty stub reported ROTATION CONFIRMED.

    An empty credential stub is not evidence of rotation. `arm` has a
    digest-of-nothing guard; `check` must reject the same absent-token state
    rather than reporting a successful rotation.
    """
    _shared_claude(root).write_text(_claude_cred("SOME-refresh"))
    _run("arm", "--agent", "claude")
    # the stub Claude Code writes when a login never completed
    _shared_claude(root).write_text(json.dumps(
        {"claudeAiOauth": {"expiresAt": 0}}))

    res = _run("check", "--agent", "claude")

    assert res.exit_code != 0, res.output
    assert "INCONCLUSIVE" in res.output, res.output
    assert "ROTATION CONFIRMED" not in res.output, (
        "printed a verdict derived from sha256('')")


def test_a_confirmed_rotation_tells_you_NOT_to_restore(root) -> None:
    """The branch that used to carry the invitation is the fatal one."""
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    _shared_claude(root).write_text(_claude_cred("ROTATED-refresh"))

    out = _run("check", "--agent", "claude").output

    assert "ROTATION CONFIRMED" in out, out
    assert "Do NOT restore" in out, out
    assert "restore if needed" not in out, out


def test_arm_refuses_to_backdate_THROUGH_a_symlink(root) -> None:
    """Structural: makes the ambiguous undo state unreachable.

    Backdating a link's target writes to a file the record does not name, and
    `restore` then cannot tell "re-linked later" from "was always a link".
    """
    real = root / "elsewhere.json"
    real.write_text(_claude_cred("ELSEWHERE-refresh"))
    os.symlink(real, _shared_claude(root))

    res = _run("arm", "--agent", "claude")

    assert res.exit_code != 0, res.output
    assert "symlink" in res.output, res.output
    assert json.loads(real.read_text())["claudeAiOauth"]["expiresAt"] > (
        time.time() * 1000), "backdated the link's target anyway"


# ── 5. found by the loop tzar auditing the commit above ─────────────────────

def test_restore_calls_an_EMPTY_STUB_what_it_is_not_a_rotation(root) -> None:
    """A REGRESSION THE FIX ABOVE INTRODUCED, caught by the loop tzar.

    `arm` and `check` both gained a digest-of-nothing guard. `restore` did not.
    So a credential that had become the empty stub digested to sha256(""),
    failed the "did the token change?" equality, and took the ROTATION branch:

        refused: the refresh token has CHANGED since arm...
          A refresh happened and the server issued a new token.
          Your live credential is fine.

    Both sentences are false for an empty credential. `check` on the
    identical file said "EMPTY — this project is logged out". Two surfaces of
    one command contradicting each other about the same bytes.
    """
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    _shared_claude(root).write_text(json.dumps({"claudeAiOauth": {"expiresAt": 0}}))

    out = _run("restore", "--agent", "claude").output

    assert "EMPTY" in out, out
    assert "not a rotation" in out, out
    assert "A refresh happened" not in out, (
        f"still explaining an empty credential as a rotation:\n{out}")
    assert "credential is fine" not in out, (
        f"still telling the user they are fine while logged out:\n{out}")


def test_restore_and_check_AGREE_on_the_empty_state(root) -> None:
    """They disagreed. Pinned together so neither can drift again.

    Both render the state through `_expiry_note`, so this asserts the shared
    wording actually reaches both surfaces rather than being reimplemented.
    """
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    _shared_claude(root).write_text(json.dumps({"claudeAiOauth": {"expiresAt": 0}}))

    restore_out = _run("restore", "--agent", "claude").output
    check_out = _run("check", "--agent", "claude").output

    for out in (restore_out, check_out):
        assert "logged out" in out, out


def test_restore_writes_nothing_to_an_empty_credential(root) -> None:
    """Its contract is "put the expiry back, never a token", and there is no
    expiry here. Writing the backup would put a token back blind."""
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    stub = json.dumps({"claudeAiOauth": {"expiresAt": 0}})
    _shared_claude(root).write_text(stub)

    _run("restore", "--agent", "claude")

    assert _shared_claude(root).read_text() == stub, (
        "restore wrote to a credential it had refused to act on")


def test_that_refusal_names_the_two_things_that_can_ACTUALLY_help(root) -> None:
    """"Refused" plus a true diagnosis is still a dead end without a next step."""
    _shared_claude(root).write_text(_claude_cred("BEFORE-refresh"))
    _run("arm", "--agent", "claude")
    _shared_claude(root).write_text(json.dumps({"claudeAiOauth": {"expiresAt": 0}}))

    out = _run("restore", "--agent", "claude").output

    assert "auth doctor" in out, out
    assert "auth login" in out, out
