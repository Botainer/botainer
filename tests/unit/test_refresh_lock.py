"""refresh_lock: cross-process serialization of the OAuth refresh section, incl.
the NFS/lock-less fallback that must FAIL CLOSED rather than refresh unserialized
(audit MEDIUM)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import botainer.broker.refresh_lock as RL
from botainer.broker.refresh_lock import _lockfile_path, refresh_lock
from botainer.core.refusal import Refused


def _cred(tmp_path: Path) -> Path:
    p = tmp_path / "cred.json"
    p.write_text("{}")
    return p


def test_flock_path_holds_and_releases(tmp_path: Path) -> None:
    p = _cred(tmp_path)
    with refresh_lock(p):
        pass  # local FS supports flock → no lockfile created
    assert not _lockfile_path(p).exists()


def test_lockfile_fallback_created_and_removed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(RL, "_try_flock", lambda cp: None)  # force fallback
    p = _cred(tmp_path)
    lf = _lockfile_path(p)
    with refresh_lock(p, now=time.time):
        assert lf.exists()          # held → lockfile present
    assert not lf.exists()          # released → removed


def test_contended_lockfile_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """The whole point: if serialization can't be obtained, REFUSE — never
    refresh a rotating token unserialized (which would trip reuse-lockout)."""
    monkeypatch.setattr(RL, "_try_flock", lambda cp: None)
    p = _cred(tmp_path)
    lf = _lockfile_path(p)
    os.close(os.open(str(lf), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))  # peer holds it
    with pytest.raises(Refused):
        with refresh_lock(p, now=time.time, timeout_s=0.4):
            pass


def test_stale_lockfile_is_stolen(tmp_path: Path, monkeypatch) -> None:
    """A lockfile left by a crashed holder (old mtime) must be reclaimed, not
    wedge refresh forever."""
    monkeypatch.setattr(RL, "_try_flock", lambda cp: None)
    p = _cred(tmp_path)
    lf = _lockfile_path(p)
    os.close(os.open(str(lf), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    old = time.time() - (RL._STALE_LOCKFILE_S + 60)
    os.utime(lf, (old, old))       # backdate → looks abandoned
    with refresh_lock(p, now=time.time, timeout_s=2.0):
        pass  # stolen + re-acquired, no Refused


def test_symlink_lockfile_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(RL, "_try_flock", lambda cp: None)
    p = _cred(tmp_path)
    lf = _lockfile_path(p)
    os.symlink(tmp_path / "elsewhere", lf)  # planted symlink at the lock path
    with pytest.raises(Refused):
        with refresh_lock(p, now=time.time, timeout_s=0.4):
            pass


def test_hook_env_allowlist_excludes_override_vars() -> None:
    """The whole destination-pinning guarantee pivots on the hook-env scrub: if a
    testing/override var ever entered the allowlist, a stale operator-shell export
    could re-open the credential-redirect surface (audit)."""
    from botainer.plugins.hooks import _HOOK_ENV_ALLOWLIST
    for name in ("BOTAINER_TESTING",
                 "BOTAINER_BROKER_UPSTREAM_OVERRIDE",
                 "BOTAINER_BROKER_TOKEN_ENDPOINT_OVERRIDE",
                 "BOTAINER_BROKER_CLIENT_ID_OVERRIDE",
                 "BOTAINER_BROKER_CREDS_PATH_OVERRIDE",
                 "BOTAINER_BROKER_SOCKET_DIR"):
        assert name not in _HOOK_ENV_ALLOWLIST, name
