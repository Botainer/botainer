"""A token refreshed inside the container must reach the shared store.

Reported. Shared mode symlinks the per-project `.credentials.json`
into `/shared-auth/` so all projects use one file. Claude Code refreshes it with
temp-file + `rename()`, and `rename()` REPLACES the symlink with a regular file
— so the refreshed token stays local and the shared store goes stale. A NEW
project then inherits the stale token and reports "login expired" while the old
project keeps working.

The repair existed but only ran at session START, so it landed only when THAT
project was started again. These tests drive the extracted reconcile directly
and simulate the exact clobber observed on the user's cluster.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "plugins/agent-claude-shared/hooks"
sys.path.insert(0, str(HOOKS))

from pre_session import reconcile_shared_credential  # noqa: E402


_OAT = "sk-ant-oat01-" + "a" * 80
_ORT = "sk-ant-ort01-" + "b" * 80


def _creds(expires_ms: int, access: str = _OAT) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": _ORT, "expiresAt": expires_ms}})


def _setup(tmp_path, shared_ms: int):
    per = tmp_path / "per"; per.mkdir()
    shared = tmp_path / "shared"; shared.mkdir()
    (shared / ".credentials.json").write_text(_creds(shared_ms))
    return per, shared


def test_atomic_write_clobber_is_backfilled_and_relinked(tmp_path) -> None:
    """THE reported failure, reproduced exactly.

    Container refreshed the token: the symlink is now a regular file holding a
    NEWER credential, while the shared store holds the older one.
    """
    now_ms = int(time.time() * 1000)
    per, shared = _setup(tmp_path, shared_ms=now_ms)
    # Simulate what rename() did: symlink replaced by a regular, newer file.
    newer = now_ms + 3_600_000
    (per / ".credentials.json").write_text(_creds(newer))

    reconcile_shared_credential(per, shared)

    # the refresh reached the shared store...
    got = json.loads((shared / ".credentials.json").read_text())
    assert got["claudeAiOauth"]["expiresAt"] == newer, (
        "the newer token was NOT copied back — a new project would inherit the "
        "stale one and report 'login expired'")
    # ...and the per-project entry is a symlink again, so the next refresh
    # writes through to the shared file rather than detaching once more.
    assert (per / ".credentials.json").is_symlink()


def test_older_local_file_does_not_overwrite_a_newer_shared_store(tmp_path) -> None:
    """Direction matters. A stale local copy must never clobber a fresher
    shared token — that would log every other project out."""
    now_ms = int(time.time() * 1000)
    per, shared = _setup(tmp_path, shared_ms=now_ms + 3_600_000)
    (per / ".credentials.json").write_text(_creds(now_ms))

    reconcile_shared_credential(per, shared)

    got = json.loads((shared / ".credentials.json").read_text())
    assert got["claudeAiOauth"]["expiresAt"] == now_ms + 3_600_000, (
        "an OLDER local credential overwrote the shared store")


@pytest.mark.parametrize("label,forged", [
    # Each case must defeat exactly ONE of the #158 guards, so each guard is
    # independently proven. The first version of this test used a 26-character
    # fake, which failed the LENGTH check — so deleting the prefix check left
    # the test green. Mutation testing caught that; hence the split.
    ("wrong prefix, correct length", "sk-evil-oat01-" + "a" * 80),
    ("too short, correct prefix", "sk-ant-oat01-short"),
    ("empty", ""),
])
def test_forged_credential_is_not_backfilled(tmp_path, label, forged) -> None:
    """#158 anti-poisoning must still hold at this new call site.

    A hostile in-container agent can write any file it likes to its own
    credential path. If a forgery back-filled, EVERY project using shared mode
    would inherit it — the single worst outcome in this subsystem.
    """
    now_ms = int(time.time() * 1000)
    per, shared = _setup(tmp_path, shared_ms=now_ms)
    (per / ".credentials.json").write_text(
        _creds(now_ms + 3_600_000, access=forged))

    reconcile_shared_credential(per, shared)

    got = json.loads((shared / ".credentials.json").read_text())
    assert got["claudeAiOauth"]["accessToken"] == _OAT, (
        f"a forged credential ({label}) reached the host-wide shared store")


def test_absurd_expiry_is_not_backfilled(tmp_path) -> None:
    """The fourth guard: a forgery claiming to expire in 100 years would pin
    the shared store to a token nothing can dislodge."""
    now_ms = int(time.time() * 1000)
    per, shared = _setup(tmp_path, shared_ms=now_ms)
    century_ms = now_ms + 100 * 365 * 24 * 3600 * 1000
    (per / ".credentials.json").write_text(_creds(century_ms))

    reconcile_shared_credential(per, shared)

    got = json.loads((shared / ".credentials.json").read_text())
    assert got["claudeAiOauth"]["expiresAt"] == now_ms, (
        "a credential claiming a 100-year expiry reached the shared store")


def test_intact_symlink_is_left_alone(tmp_path) -> None:
    """The common case: nothing refreshed, nothing to do."""
    now_ms = int(time.time() * 1000)
    per, shared = _setup(tmp_path, shared_ms=now_ms)
    link = per / ".credentials.json"
    link.symlink_to("/shared-auth/agent-claude/.credentials.json")

    reconcile_shared_credential(per, shared)

    assert link.is_symlink()
    got = json.loads((shared / ".credentials.json").read_text())
    assert got["claudeAiOauth"]["expiresAt"] == now_ms
