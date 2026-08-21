"""Cross-process serialization for the OAuth read→refresh→write critical section.

The OAuth brokers refresh a ROTATING refresh token: the provider hands back a new
refresh token and invalidates the old one, so two processes that refresh
concurrently against the SAME credential file both present the old token — the
second trips the backend's reuse-detection and can lock the account (codex's
`refresh_token_reused`; the same class for Anthropic). Serialization is therefore
a correctness+security requirement, not just hygiene.

`fcntl.flock` is the natural tool, but it silently no-ops / raises on filesystems
without working lock daemons — exactly the HPC/NFS/parallel-FS targets. The old
code fell back to running LOCKLESS there (audit MEDIUM). This module
serializes on those filesystems too, via an ``O_CREAT|O_EXCL`` lock FILE (whose
create is atomic on local FS and NFSv3+), and — critically — FAILS CLOSED (raises
Refused) rather than ever refreshing unserialized.

Usage::

    with refresh_lock(cred_path):
        # re-read the credential here (another holder may have just refreshed) —
        # if it's now fresh, use it; else refresh + write-back.
        ...

`flock` is tried first (cheap, auto-released on crash); the lockfile is the
fallback. The lockfile carries a stale-steal timeout so a crashed holder can't
wedge refresh forever.
"""
from __future__ import annotations

import contextlib
import errno
import os
import time
from pathlib import Path
from typing import Callable, Iterator

from botainer.core.refusal import RefusalCategory, Refused

# How long to wait for a peer to release the lock before giving up (fail closed).
_ACQUIRE_TIMEOUT_S = 35.0          # > the 30s refresh HTTP timeout
# A lockfile older than this is treated as abandoned by a crashed holder.
_STALE_LOCKFILE_S = 120.0
_POLL_S = 0.2


@contextlib.contextmanager
def refresh_lock(
    cred_path: Path, *, now: Callable[[], float] = time.time,
    timeout_s: float = _ACQUIRE_TIMEOUT_S,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock tied to ``cred_path`` for the body.

    The PRIMARY lock is an ``O_EXCL`` lockfile — atomic and cross-node-safe on the
    shared/NFS filesystems an HPC credential lives on. ``fcntl.flock`` is added
    only as a local belt-and-suspenders: on many network filesystems flock is
    emulated NODE-LOCALLY yet still RETURNS SUCCESS, so if it were the primary two
    brokers on different compute nodes would both "acquire" it, refresh
    concurrently, and trip the provider's refresh-token-reuse account lockout — the
    exact failure this serialization exists to prevent (sharp-edges MED).
    Raises :class:`Refused` if the lock can't be acquired within ``timeout_s`` — the
    caller must NOT refresh a rotating token unserialized.
    """
    handle = None
    flock_handle = None
    try:
        handle = _acquire_lockfile(cred_path, now=now, timeout_s=timeout_s)
        flock_handle = _try_flock(cred_path)  # best-effort local addition
        yield
    finally:
        if flock_handle is not None:
            flock_handle.release()
        if handle is not None:
            handle.release()


class _FlockHandle:
    def __init__(self, fh) -> None:
        self._fh = fh

    def release(self) -> None:
        with contextlib.suppress(OSError):
            self._fh.close()  # closing releases the flock


class _LockfileHandle:
    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd

    def release(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)
        with contextlib.suppress(OSError):
            os.unlink(self._path)


def _try_flock(cred_path: Path) -> _FlockHandle | None:
    """Blocking LOCK_EX on the credential file. Returns a handle, or None if the
    filesystem doesn't support flock (caller falls back to the lockfile)."""
    try:
        import fcntl
    except ImportError:  # non-POSIX
        return None
    try:
        fh = open(cred_path, "rb")
    except OSError:
        return None  # file missing/unreadable — the caller's _load will report it
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        # flock unsupported/unavailable (e.g. NFS without lockd) → fall back.
        with contextlib.suppress(OSError):
            fh.close()
        return None
    return _FlockHandle(fh)


def _lockfile_path(cred_path: Path) -> Path:
    return cred_path.with_name(f".{cred_path.name}.refresh.lock")


def _acquire_lockfile(
    cred_path: Path, *, now: Callable[[], float], timeout_s: float
) -> _LockfileHandle:
    """O_EXCL lockfile mutex (works where flock doesn't). Polls until it can
    create the lockfile, stealing a stale one; raises Refused on timeout."""
    lock_path = _lockfile_path(cred_path)
    deadline = now() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600)
            return _LockfileHandle(lock_path, fd)
        except FileExistsError:
            _maybe_steal_stale(lock_path, now=now)
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # someone planted a symlink at the path
                raise Refused(
                    RefusalCategory.BROKER_REFRESH_FAILED,
                    f"refresh lockfile {lock_path} is a symlink; refusing")
            raise Refused(RefusalCategory.BROKER_REFRESH_FAILED,
                          f"cannot create refresh lockfile {lock_path}: {exc}")
        if now() >= deadline:
            raise Refused(
                RefusalCategory.BROKER_REFRESH_FAILED,
                f"timed out acquiring the refresh lock for {cred_path} "
                f"(another session is refreshing?); refusing to spend a rotating "
                f"token unserialized")
        time.sleep(_POLL_S)


def _maybe_steal_stale(lock_path: Path, *, now: Callable[[], float]) -> None:
    """Unlink the lockfile if it looks abandoned by a crashed holder."""
    try:
        age = now() - os.stat(lock_path).st_mtime
    except OSError:
        return  # vanished — the next O_EXCL attempt will win
    if age > _STALE_LOCKFILE_S:
        with contextlib.suppress(OSError):
            os.unlink(lock_path)
