"""What KIND of filesystem is a path on, and can SQLite's WAL work there.

WHY THIS EXISTS. #146: codex keeps four SQLite databases in `CODEX_HOME`, all in
WAL mode, and re-sets WAL on every open (measured — forcing `journal_mode=DELETE`
from the host is reverted before the first query). WAL requires an mmap'd
shared-memory file (`-shm`), which network filesystems do not provide. That makes
it a PLACEMENT problem, and placement cannot be reasoned about without knowing
what the path is sitting on.

Nothing in botainer knew this. `botainer where` walked `st_dev` to find the mount
POINT and never asked its TYPE — so a user on GPFS and a user on an SSD saw the
same output, and the one heading for corruption got no signal.

THREE STATES, NOT TWO, AND THAT IS THE WHOLE DESIGN.
`network` / `local` / `unknown`. There is no complete list of parallel
filesystems — sites run things this file has never heard of — so a two-state
answer would have to guess, and the guess has an asymmetric cost:

    wrongly "local"    -> we stay silent and the user's database corrupts
    wrongly "unknown"  -> one extra line of output that says "I could not tell"

So an unrecognised type is `unknown` and NEVER `local`. Callers must handle three
states; that is deliberate, and it is why this returns an enum-ish string rather
than a bool. Same consequence-asymmetry reasoning that made the credential carry
an allowlist rather than a denylist: when the two mistakes cost different
amounts, the ambiguous case takes the cheap one.

WHAT THIS IS NOT. The membership sets below are a FILTER, not a property, and
they are documented as such. Structure cannot help here — we genuinely cannot
observe "does WAL work" without writing a WAL database and reading it back, which
is a runtime probe with side effects, not a classification. The honest form is:
recognise what we can, say `unknown` otherwise, and never claim safety we have
not established.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

# Filesystems where SQLite WAL is known NOT to work, because WAL needs an mmap'd
# `-shm` file shared between processes on ONE host. Sources are the SQLite
# documentation's own statement that WAL "does not work on a network filesystem".
#
# `fuse.*` is prefix-matched separately below: sshfs, s3fs and friends are all
# network transports wearing a local-looking name.
NETWORK_FILESYSTEMS = frozenset({
    "nfs", "nfs3", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs",
    "lustre", "gpfs", "mmfs", "beegfs", "cephfs", "ceph", "glusterfs",
    "panfs", "pvfs2", "orangefs", "afs", "9p", "davfs", "davfs2",
    "ocfs2", "gfs2",
})

# Types we affirmatively recognise as host-local. Kept SHORT on purpose: this
# set is the only thing that can produce a "local" verdict, so a name belongs
# here only when WAL genuinely works on it.
LOCAL_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "reiserfs",
    "apfs", "hfs", "hfsplus", "ufs",
    "tmpfs", "ramfs", "overlay", "overlayfs", "squashfs", "erofs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk",
})

NETWORK = "network"
LOCAL = "local"
UNKNOWN = "unknown"


def _mount_table() -> list[tuple[str, str]]:
    """[(mount_point, fstype), …], longest mount point first.

    Longest-first matters: `/` and `/home/user/scratch` both "contain" a path
    under the latter, and only the longest match is the filesystem the path is
    actually on. Sorting here rather than at each call site means a caller
    cannot get it subtly wrong.
    """
    rows: list[tuple[str, str]] = []
    proc = Path("/proc/self/mounts")
    if proc.exists():
        try:
            for line in proc.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    # Field 2 is the mount point with octal escapes for spaces.
                    point = parts[1].replace("\\040", " ")
                    rows.append((point, parts[2]))
        except OSError:
            pass
    elif platform.system() == "Darwin":
        # macOS has no /proc. `mount` prints "dev on /point (type, opts)".
        try:
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            if " on " not in line or "(" not in line:
                continue
            point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
            kind = line.rsplit("(", 1)[1].split(",", 1)[0].rstrip(")")
            rows.append((point, kind))
    rows.sort(key=lambda r: len(r[0]), reverse=True)
    return rows


def stale_binds(mountinfo: str) -> list[tuple[str, str]]:
    """Return (source, mount_point) pairs marked //deleted in mountinfo.

    A single-file bind can retain an old inode after the host atomically replaces
    the file. Parsing the kernel's marker distinguishes this state from a missing
    ordinary file without statting every target. The caller supplies the text so
    the parser can inspect either the current container or another mount namespace.

    The root and mount point are fields 4 and 5 before the optional fields and
    literal " - " separator. Paths use octal escapes for spaces."""
    out: list[tuple[str, str]] = []
    for line in mountinfo.splitlines():
        left = line.split(" - ", 1)[0]
        parts = left.split()
        if len(parts) < 5:
            continue
        root, point = parts[3], parts[4]
        if root.endswith("//deleted"):
            out.append((root[: -len("//deleted")].replace("\\040", " "),
                        point.replace("\\040", " ")))
    return out


def read_stale_binds() -> list[tuple[str, str]] | None:
    """`stale_binds` for THIS process, or None where /proc/self/mountinfo is
    not available (macOS, and any non-Linux host). None means "cannot tell",
    never "none found"."""
    proc = Path("/proc/self/mountinfo")
    try:
        if not proc.exists():
            return None
        return stale_binds(proc.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def filesystem_type(path: str | os.PathLike[str]) -> str:
    """The filesystem type string for `path`, or "" if it cannot be determined.

    Resolves the path first, and walks UP to the nearest existing ancestor — a
    caller usually asks about a directory botainer is about to create, and
    "does not exist yet" must not become "unknown filesystem".
    """
    try:
        p = Path(path).resolve()
    except OSError:
        return ""
    while not p.exists() and p != p.parent:
        p = p.parent
    target = str(p)
    for point, kind in _mount_table():
        if target == point or target.startswith(point.rstrip("/") + "/"):
            return kind
    return ""


def classify(path: str | os.PathLike[str]) -> str:
    """`NETWORK`, `LOCAL`, or `UNKNOWN` — never a guess.

    An unrecognised type is UNKNOWN, not LOCAL. See the module docstring: the
    two mistakes cost wildly different amounts, so the ambiguous case takes the
    cheap one.
    """
    kind = filesystem_type(path).lower()
    if not kind:
        return UNKNOWN
    if kind.startswith("fuse."):
        # sshfs, s3fs, gcsfuse … network transports with local-looking names.
        # `fuseblk` (a local NTFS/exFAT mount) is in LOCAL_FILESYSTEMS and does
        # not carry the dot, so it does not land here.
        return NETWORK
    if kind in NETWORK_FILESYSTEMS:
        return NETWORK
    if kind in LOCAL_FILESYSTEMS:
        return LOCAL
    return UNKNOWN


def sqlite_wal_is_safe(path: str | os.PathLike[str]) -> bool | None:
    """True / False / None-for-unknown, for a SQLite WAL database at `path`.

    PROBED, NOT INFERRED — for the same reason `is_case_insensitive` below is,
    and this function used to get it wrong. It classified the filesystem TYPE
    and answered from that: network -> unsafe, local -> safe. But the type does
    not answer the question. WAL needs the `-shm` file to be shared *memory*,
    which means mmap; whether a given mount provides that depends on the server,
    the mount options and the client build, not on the string in `/proc/mounts`.
    A GPFS site may have it. An NFS mount with the wrong options may not. Every
    answer derived from the type was a guess about the very thing a wrong guess
    corrupts.

    WHY THIS MATTERS (#146). codex keeps four SQLite databases and re-sets WAL
    on every open — forcing `journal_mode=DELETE` from outside does not stick,
    measured. And the databases live under `MY_BOTAINER` in ALL THREE auth
    modes: isolated, shared and broker all root the codex home at
    `<state root>/state/<uuid>/data/agent-codex/…`. So this is a PLACEMENT
    question, not a credential one, and the auth mode is irrelevant to it.

    That is what makes probing worth the write: it lets `botainer doctor` answer
    "will codex work on this filesystem?" on a login node, with no codex
    installed, no container, no login and nothing to paste back.

    Still tri-state, deliberately. A caller that wants to refuse must
    distinguish "we know this breaks" from "we could not tell"; collapsing them
    either refuses working setups or stays silent on breaking ones. Both have
    happened in this project under other names. None means the probe could not
    run — no write permission, no sqlite3 — never "probably fine".

    Writes and removes a small database under `path`. Never raises.
    """
    import sqlite3
    import tempfile

    try:
        probe_dir = tempfile.mkdtemp(prefix=".botainer-walprobe-", dir=os.fspath(path))
    except Exception:            # noqa: BLE001 — unwritable, missing, whatever
        return None

    db = os.path.join(probe_dir, "probe.db")
    try:
        conn = sqlite3.connect(db)
        try:
            # SQLite reports the mode it ACTUALLY adopted, so this is the answer
            # rather than a request. On a filesystem that cannot back WAL it
            # stays in the previous mode instead of erroring.
            got = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if not got or str(got[0]).lower() != "wal":
                return False
            # Adopting the mode is not the same as it working. A committed write
            # is what forces the -shm segment into existence, which is the part
            # that actually needs mmap.
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            return os.path.exists(db + "-shm")
        finally:
            conn.close()
    except sqlite3.Error:
        # An I/O error here IS the finding — SQLITE_IOERR_SHMOPEN is exactly
        # what a filesystem without shared-memory support raises.
        return False
    except Exception:            # noqa: BLE001 — a probe must never take out its caller
        return None
    finally:
        import shutil
        shutil.rmtree(probe_dir, ignore_errors=True)


def is_case_insensitive(path: str | os.PathLike[str]) -> bool | None:
    """Does this path collapse `Foo` and `foo`? True / False / None-if-untestable.

    PROBED, NOT INFERRED, and that is deliberate. The filesystem TYPE does not
    answer this: APFS ships case-insensitive by default but can be formatted
    case-sensitive, and a Docker Desktop bind inherits whatever the host volume
    does. Any answer derived from the type would be a guess about the very thing
    a wrong guess corrupts.

    WHY IT MATTERS, measured on a Mac-hosted bind 2026-08-31:

        write Foo.py "first"   then   foo.py "second"
        -> ONE file, named Foo.py, containing "second"

    Not an error. A SILENT CLOBBER — the first file's content is gone and
    nothing reports it. `import Model` returns what `model.py` defined. The same
    project on a cluster (ext4/GPFS/Lustre) keeps both files, so the dev scaffold
    and the production platform disagree without saying so.

    The probe writes two files in a private temp directory UNDER `path` and
    removes them. It is the only honest way to answer, but it is a WRITE, so
    callers must not run it on a path the user did not expect to be touched.
    Returns None when the directory cannot be written — an unwritable path is
    not evidence of case sensitivity.
    """
    import tempfile
    try:
        with tempfile.TemporaryDirectory(dir=str(path), prefix=".botainer-case-") as d:
            probe = Path(d)
            (probe / "botainer_case_probe").write_text("a", encoding="utf-8")
            upper = probe / "BOTAINER_CASE_PROBE"
            if upper.exists():
                # The uppercase name resolves to the file we just wrote under
                # the lowercase one: the filesystem folded them together.
                return True
            return False
    except OSError:
        return None


def describe(path: str | os.PathLike[str]) -> str:
    """One short human phrase: `ext4 (local)`, `lustre (network)`, `unknown`.

    For surfaces that should STATE what they found rather than act on it —
    `botainer where` prints the mount point and, until now, never its type.
    """
    kind = filesystem_type(path)
    verdict = classify(path)
    if not kind:
        return "unknown"
    return f"{kind} ({verdict})"
