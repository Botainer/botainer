"""Atomic + restrictive-mode file writes.

Task #265: every `path.write_text(...)` followed by `os.chmod(0o600)` has
a brief world-readable window between create (with default umask 0644)
and chmod. Sensitive files (policy.yaml, meta.json, trust.lock, etc.)
should be created with O_CREAT|O_EXCL|O_WRONLY at 0o600 from the start.

Task #263 + #264: combined with atomic-rename (tempfile → os.replace)
this also closes the RMW truncation race for trust.lock and installed.lock.

Usage:
    from botainer.state.secure_write import write_secure
    write_secure(path, content, mode=0o600)
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from pathlib import Path


from botainer.core.refusal import Refused, RefusalCategory

def write_secure(path: Path, content: str, *, mode: int = 0o600,
                 encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically with `mode` set at create time.

    Steps:
      1. Create a temp file in the SAME DIR (so os.replace is atomic
         on the same filesystem)
      2. Write content with os.open(..., O_CREAT|O_EXCL|O_WRONLY, mode)
         — the file is created with restrictive perms from byte 1
      3. os.fsync + close
      4. os.replace(tmp, path) — atomic rename; old file (if any) is
         replaced in a single syscall; no half-state visible

    Task #265 (world-readable window): step 2 sets mode at create.
    Tasks #263/#264 (RMW truncation): step 4 is atomic.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.tmp.",
            dir=parent,
        )
    except OSError as exc:
        # A FULL DISK OR AN EXHAUSTED QUOTA IS NOT A STACK TRACE.
        #
        # A raw quota exception does not identify reclaimable state. Point to
        # `botainer where` so the user can inspect storage before deleting it.
        #
        # EDQUOT (122 on Linux) and ENOSPC are the same problem to a user and
        # want the same answer, so they get one message. Every state write funnels
        # through here, which is why the guidance lives at this chokepoint rather
        # than at the dozen call sites.
        if exc.errno not in (errno.EDQUOT, errno.ENOSPC):
            raise
        what = ("disk quota exceeded" if exc.errno == errno.EDQUOT
                else "no space left on the device")
        raise Refused(
            RefusalCategory.STATE_WRITE_FAILED,
            f"cannot write {path.name}: {what} on {parent}.\n"
            f"\n"
            f"  botainer could not save this project's state, so the session "
            f"cannot start.\n"
            f"  Nothing was corrupted — the write is atomic and did not begin.\n"
            f"\n"
            f"  Find what is using the space:\n"
            f"      botainer where\n"
            f"  It lists every project with sizes and marks which directories "
            f"are safe to\n"
            f"  delete (scratch and packages are; data/ and sessions/ are NOT — "
            f"that is your\n"
            f"  credentials and project identity).\n"
            f"\n"
            f"  On a cluster this is usually botainer's per-project data on "
            f"your HOME quota.\n"
            f"  CHECK BOTH NUMBERS — clusters limit bytes AND file count, and "
            f"the second\n"
            f"  is the one that surprises people: /packages (pip/npm/conda) is "
            f"only a few\n"
            f"  GB but is hundreds of thousands of files, so an inode cap dies "
            f"to it while\n"
            f"  `du` still looks fine. Move whichever is the problem:\n"
            f"      tools/pkg/relocate-storage.sh --component packages "
            f"--dest <big-volume>\n"
            f"      tools/pkg/relocate-storage.sh --component scratch  "
            f"--dest <your-scratch>\n"
            f"  See docs/STORAGE.md §2b.",
        ) from exc
    try:
        # mkstemp uses mode 0600 by default already; chmod again to be
        # explicit in case future Python changes the default.
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync isn't critical on tmpfs / proc
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
