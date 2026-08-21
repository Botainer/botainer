"""`botainer auth doctor` must distinguish the two shared-mode failure modes.

Context:  a user reported "yesterday's project works, today's says
login expired". Three explanations were produced from reading code and all three
were wrong. This command exists so the next such report is answered from disk
state instead of from theory, and these tests pin the two diagnoses apart:

  1. SYMLINK BROKEN — rename() replaced a project's symlink into the shared
     store with a private regular file. Confirmed, observed.
  2. TOKEN DIVERGENCE — holders carry different refresh tokens, which is what
     rotation would look like. NOT confirmed; this command is how we find out.

They need opposite fixes (re-link vs. abandon multi-holder for the broker), so
reporting one when the other is true is worse than reporting nothing.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.main import cli

_SECRET = "REFRESH-SECRET-VALUE-do-not-print-me-0123456789abcdef"
_OTHER = "REFRESH-ROTATED-VALUE-also-secret-fedcba9876543210"


def _cred(refresh: str, hours: float) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": "sk-access-" + refresh[-6:],
        "refreshToken": refresh,
        "expiresAt": int((time.time() + hours * 3600) * 1000)}})


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A state root with a shared store and two projects."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    shared = tmp_path / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True)
    (shared / ".credentials.json").write_text(_cred(_SECRET, -5))

    def project(uuid: str, name: str):
        d = (tmp_path / "state" / uuid / "data" / "agent-claude"
             / "profiles" / "default")
        d.mkdir(parents=True)
        bn = tmp_path / "state" / "by-name"
        bn.mkdir(exist_ok=True)
        os.symlink(f"../{uuid}", bn / f"{name}-{uuid[:8]}")
        return d

    return {
        "root": tmp_path,
        "shared": shared / ".credentials.json",
        "alpha": project("11111111-aaaa-bbbb-cccc-000000000001", "proj_alpha"),
        "beta": project("22222222-aaaa-bbbb-cccc-000000000002", "proj_beta"),
    }


def _run() -> str:
    res = CliRunner().invoke(cli, ["auth", "doctor"])
    assert res.exit_code == 0, res.output
    return res.output


# --------------------------------------------------------------------------
# Mode 1: the broken symlink.
# --------------------------------------------------------------------------

def test_a_regular_file_where_a_symlink_belongs_is_reported(host) -> None:
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 2))

    out = _run()
    # Wording changed: the single alarming verdict became TWO,
    # split by whether the user must act. A project whose private copy is OLDER
    # than the shared store repairs itself on the next start (pre_session backs
    # it up and re-links), so alarming about it in red — as this used to — sent
    # people chasing three stale test projects that were fine. The assertion
    # now checks the FACT is reported, not the old alarm wording.
    assert "private copy" in out or "NEWER than the shared store" in out
    # "NO LONGER SHARING" is gone: it was the alarming half of a verdict that
    # fired identically whether the user had to act or not.
    assert "proj_beta" in out
    # and the healthy one must NOT be accused
    beta_section = out[out.index("What this means"):]
    assert "proj_alpha" not in beta_section


def test_all_symlinks_intact_is_reported_as_healthy(host) -> None:
    for key in ("alpha", "beta"):
        os.symlink(host["shared"], host[key] / ".credentials.json")
    out = _run()
    assert "Every project still points at the shared store" in out
    assert "NO LONGER SHARING" not in out


def test_a_dangling_symlink_is_not_silently_healthy(host) -> None:
    """A link to a deleted store must not read as 'still sharing'."""
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    host["shared"].unlink()
    out = _run()
    assert "DANGLING" in out


# --------------------------------------------------------------------------
# Mode 2: token divergence. The whole point is that this is INDEPENDENT of
# mode 1 — a project can be unlinked yet still hold the same token.
# --------------------------------------------------------------------------

def test_identical_tokens_report_no_rotation(host) -> None:
    """Unlinked BUT identical: rotation is not what is breaking this host."""
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 2))

    out = _run()
    assert "SAME refresh token" in out
    assert "DIFFERENT refresh tokens" not in out


def test_divergent_tokens_are_not_reported_as_proof_of_rotation(host) -> None:
    """Divergence has a mundane explanation and must not be sold as a finding.

    Real data showed FIVE distinct refresh tokens written across
    seven weeks — which separate `auth login` runs explain completely, with no
    rotation anywhere. The first version of this verdict said "holders have
    diverged ... rotation invalidates the rest", i.e. a confident wrong
    conclusion from the very tool built to stop me drawing those.
    """
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    (host["beta"] / ".credentials.json").write_text(_cred(_OTHER, 2))

    out = _run()
    assert "2 DIFFERENT refresh tokens" in out
    assert "does NOT show the server rotates" in out
    assert "Separate `auth login` runs" in out
    # and it must name the experiment that WOULD settle it
    # The R1 pointer is gone deliberately: it told the user to settle a question
    # that WAS settled  (rotation AND invalidation, HTTP 400),
    # from a doc under private/ that the distribution does not ship. What must
    # survive is the sound part — that divergence alone is not proof.
    assert "does NOT show the server rotates" in out
    assert "rotation-probe" in out, "must offer a way to re-measure" and "TESTPLAN" in out
    assert "SAME refresh token" not in out


def test_grouping_is_by_equality_not_by_file_count(host) -> None:
    """Three holders, two distinct tokens -> exactly 2 groups reported."""
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    (host["beta"] / ".credentials.json").write_text(_cred(_OTHER, 2))
    third = (host["root"] / "state" / "33333333-aaaa-bbbb-cccc-000000000003"
             / "data" / "agent-claude" / "profiles" / "default")
    third.mkdir(parents=True)
    (third / ".credentials.json").write_text(_cred(_OTHER, 3))

    out = _run()
    assert "2 DIFFERENT refresh tokens" in out


# --------------------------------------------------------------------------
# The load-bearing safety property.
# --------------------------------------------------------------------------

def test_no_token_material_is_ever_printed(host) -> None:
    """A diagnostic that leaks the credential is worse than no diagnostic.

    Checks the token, every substantial substring of it, and any hex digest of
    it — grouping is done by equality precisely so nothing derived from the
    secret needs to be displayed.
    """
    import hashlib

    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    (host["beta"] / ".credentials.json").write_text(_cred(_OTHER, 2))
    out = _run()

    for secret in (_SECRET, _OTHER):
        assert secret not in out
        # any run of 12+ chars of the secret would be a partial disclosure
        for i in range(0, len(secret) - 12):
            assert secret[i:i + 12] not in out, f"leaked substring at {i}"
        digest = hashlib.sha256(secret.encode()).hexdigest()
        for n in (8, 12, 16, 64):
            assert digest[:n] not in out, f"leaked a {n}-char hash of the token"
    # access tokens must not leak either
    assert "sk-access-" not in out


def test_reports_the_limit_of_what_it_checked(host) -> None:
    """Local file state cannot prove a token is accepted; say so."""
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    out = _run()
    assert "does NOT tell you" in out
    assert "revoked" in out


def test_no_credentials_at_all_is_a_clear_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    (tmp_path / "state").mkdir()
    out = _run()
    assert "Nothing is logged in" in out


def test_unparseable_credential_file_does_not_crash(host) -> None:
    (host["alpha"] / ".credentials.json").write_text("{ not json")
    out = _run()
    assert "could not parse" in out


def test_a_credential_file_with_no_token_is_called_out(host) -> None:
    """The third state, seen on the cluster.

    A file that parses but holds no refreshToken renders as
    "EXPIRED 1970-01-01 (495926h ago)" if you just format expiresAt=0 — noise
    that buries the actual fact, which is that the project is logged out while
    APPEARING to have a credential. Any "does the file exist?" check says yes.
    """
    import json as _json
    os.symlink(host["shared"], host["alpha"] / ".credentials.json")
    (host["beta"] / ".credentials.json").write_text(
        _json.dumps({"claudeAiOauth": {"accessToken": "", "expiresAt": 0}}))

    out = _run()
    assert "1970" not in out, "rendered an epoch date instead of saying it is empty"
    assert "holds no token" in out or "hold NO token" in out
    assert "logged out" in out
    assert "proj_beta" in out


# --------------------------------------------------------------------------
# `auth rotation-test` — R1 as a command.
#
# Exists because two hand-written snippets (a one-liner, then a heredoc) both
# failed on paste, and there is a memory entry saying heredocs do exactly that.
# A procedure the user must hand-assemble is a missing capability.
# --------------------------------------------------------------------------


def _armed_path(host) -> Path:
    """Whichever file `arm` chose — do not assume; ask the record.

    The tests below used to hardcode a project file, which broke the moment
    `arm` learned to prefer the shared store. Reading the record keeps them
    testing the BEHAVIOUR (armed file in, verdict out) rather than the
    selection policy, which has its own test.
    """
    import json as _json
    return Path(_json.loads((host["root"] / "rotation-test.json").read_text())["path"])

def _rt(*args) -> str:
    res = CliRunner().invoke(cli, ["auth", "rotation-test", *args])
    return res.output


def test_arm_records_backs_up_and_forces_the_next_refresh(host) -> None:
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 5))
    out = _rt("arm")
    assert "ARMED" in out

    import json as _json
    rec = _json.loads((host["root"] / "rotation-test.json").read_text())
    # the backup must actually exist — "restore" is the safety net
    assert Path(rec["backup"]).exists()
    # and the live file must now be expired, so the next call MUST refresh
    blk = _json.loads(Path(rec["path"]).read_text())["claudeAiOauth"]
    assert blk["expiresAt"] < time.time() * 1000


def test_state_file_never_contains_token_material(host) -> None:
    """The record is compared across time, so it is written to disk — which
    makes it a place a secret could come to rest. It must hold digests only."""
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 5))
    _rt("arm")
    raw = (host["root"] / "rotation-test.json").read_text()
    assert _SECRET not in raw
    for i in range(0, len(_SECRET) - 12):
        assert _SECRET[i:i + 12] not in raw


def test_check_reports_no_rotation_when_only_the_access_token_changed(host) -> None:
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 5))
    _rt("arm")
    cred = _armed_path(host)
    # simulate a refresh that keeps the same refresh token
    import json as _json
    d = _json.loads(cred.read_text())
    d["claudeAiOauth"]["accessToken"] = "sk-access-BRANDNEW"
    d["claudeAiOauth"]["expiresAt"] = int((time.time() + 3600) * 1000)
    cred.write_text(_json.dumps(d))

    out = _rt("check")
    assert "NO ROTATION" in out
    assert "symlink bug is the whole story" in out


def test_check_reports_rotation_when_both_changed(host) -> None:
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 5))
    _rt("arm")
    cred = _armed_path(host)
    cred.write_text(_cred(_OTHER, 1))          # both tokens new

    out = _rt("check")
    assert "ROTATION CONFIRMED" in out
    assert "broker" in out


def test_an_unchanged_access_token_is_inconclusive_not_a_result(host) -> None:
    """THE failure mode that would produce a wrong answer: if the session never
    made an API call, nothing refreshed, and "refresh token unchanged" would
    read as NO ROTATION when it actually means the experiment did not run."""
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 5))
    _rt("arm")

    out = _rt("check")
    assert "NO REFRESH HAPPENED" in out
    assert "Inconclusive" in out
    assert "NO ROTATION" not in out, "reported a result from a run that never happened"


def test_restore_puts_the_original_back(host) -> None:
    (host["beta"] / ".credentials.json").write_text(_cred(_SECRET, 5))
    _rt("arm")
    cred = _armed_path(host)
    original = _json_refresh(cred)
    cred.write_text(_cred(_OTHER, 1))
    _rt("restore")
    assert _json_refresh(cred) == original


def _json_refresh(path: Path) -> str:
    import json as _json
    return _json.loads(path.read_text())["claudeAiOauth"]["refreshToken"]


def test_check_without_arm_refuses(host) -> None:
    out = _rt("check")
    assert "nothing armed" in out


def test_arm_prefers_the_shared_store_because_that_is_what_gets_refreshed(host) -> None:
    """In shared mode — the default — `pre_session` re-links every project to
    the shared store at session start. Arming a project's private file would
    measure a credential the session never touches."""
    (host["beta"] / ".credentials.json").write_text(_cred(_OTHER, 9))  # newest
    out = _rt("arm")
    assert "shared-auth" in out, "armed a project file, not the shared store"


def test_check_refuses_to_compare_across_two_different_files(host) -> None:
    """THE fabricated-result guard.

    If the armed file is replaced by a symlink to the shared store, the
    before-digests belong to one token and the after-digests to another.
    Differences are then guaranteed and meaningless — reporting them as
    ROTATION CONFIRMED would invent a finding.
    """
    import json as _json
    # no shared token yet, so arm targets the project file
    host["shared"].unlink()
    cred = host["beta"] / ".credentials.json"
    cred.write_text(_cred(_SECRET, 5))
    _rt("arm")

    # now the shared store appears with a DIFFERENT token and the project is
    # re-linked to it, exactly as pre_session would do
    host["shared"].parent.mkdir(parents=True, exist_ok=True)
    host["shared"].write_text(_cred(_OTHER, 3))
    cred.unlink()
    os.symlink("/shared-auth/agent-claude/.credentials.json", cred)

    out = _rt("check")
    assert "INCONCLUSIVE" in out
    assert "ROTATION CONFIRMED" not in out, "fabricated a result from two files"
    assert "re-arm" in out.lower()
