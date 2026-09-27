"""Plugin provenance: tree SHA + image digest lock (codex HIGH 9 minimal model).

Recorded in `~/.botainer/plugins/installed.lock` as JSON-Lines after install.
Each entry:
- name, version
- source (file:// URL, git URL, tarball path)
- tree_sha (sha256 of the plugin source tree, deterministic)
- image_digest (after `docker pull` or `docker build`; None at install time
  for dockerfile plugins until first build)
- installed_at (ISO-8601 UTC)
- tier (first-party | community-verified | third-party)
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProvenanceEntry:
    name: str
    version: str
    source: str
    tree_sha: str
    image_digest: str | None
    installed_at: str
    tier: str


def compute_tree_sha(plugin_dir: Path) -> str:
    """Deterministic SHA-256 over the plugin source tree.

    Walks files in sorted order, hashes (relpath || NUL || content) for each.

    Task #303: skip .pyc files + __pycache__ dirs. Python bytecode is
    interpreter-version-dependent (CPython 3.10 != 3.11); including
    .pyc bytes in the tree hash made verify_plugin fail-closed on the
    first session after a Python upgrade for every installed plugin.
    Source files (.py, .yaml, .md, etc.) are the trust surface; the
    bytecode is a build artifact derived from them.
    """
    plugin_dir = plugin_dir.resolve()
    h = hashlib.sha256()
    files = sorted(
        p for p in plugin_dir.rglob("*")
        if p.is_file()
        and p.suffix != ".pyc"
        and "__pycache__" not in p.parts
    )
    for f in files:
        rel = str(f.relative_to(plugin_dir)).encode("utf-8")
        h.update(rel)
        h.update(b"\x00")
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\x00")
    return h.hexdigest()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


import contextlib
import os as _os


@contextlib.contextmanager
def lock_for(lock_path: Path):
    """Serialize installed.lock mutations across processes (AUDIT).

    installed.lock is mutated two ways — append_lock() appends a line, and
    cli/image.py._record_image_digest() does a read-modify-WRITE (rewrites the
    whole file). Concurrent (e.g. a `plugin add` appending while `image build`
    rewrites) silently dropped an entry — a lost update; composition then can't
    resolve the dropped plugin's image. An exclusive flock on a sibling
    `<lock>.flock` serializes both. POSIX (Linux/macOS/HPC — botainer's targets).
    """
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    guard = lock_path.with_name(lock_path.name + ".flock")
    fd = _os.open(str(guard), _os.O_CREAT | _os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            _os.close(fd)


def append_lock(lock_path: Path, entry: ProvenanceEntry) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "name": entry.name,
            "version": entry.version,
            "source": entry.source,
            "tree_sha": entry.tree_sha,
            "image_digest": entry.image_digest,
            "installed_at": entry.installed_at,
            "tier": entry.tier,
        },
        sort_keys=True,
    )
    with lock_for(lock_path):
        with lock_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_lock(lock_path: Path) -> list[ProvenanceEntry]:
    if not lock_path.exists():
        return []
    out: list[ProvenanceEntry] = []
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(
            ProvenanceEntry(
                name=str(d.get("name", "")),
                version=str(d.get("version", "")),
                source=str(d.get("source", "")),
                tree_sha=str(d.get("tree_sha", "")),
                image_digest=d.get("image_digest"),
                installed_at=str(d.get("installed_at", "")),
                tier=str(d.get("tier", "third-party")),
            )
        )
    return out


#: The one place this string's shape is defined.
#:
#: It was built in `cli/image.py` and taken apart in `core/composition.py` by
#: slicing a hard-coded prefix length, with no shared constant between them, and
#: `cli/hpc.py` was about to become a third copy. `apptainer:` disambiguates
#: from a docker image id, which is a bare `sha256:<hex>`; the trailing path
#: records WHICH file was hashed, so a marker cannot be read as vouching for a
#: .sif it never saw.
APPTAINER_MARKER_PREFIX = "apptainer:sha256:"


def sha256_file(path: Path) -> str:
    """Stream a file's sha256. Streamed because a .sif is hundreds of MiB."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def apptainer_marker(sif_path: Path, digest: str) -> str:
    """Build the installed.lock marker for a freshly-built .sif."""
    return f"{APPTAINER_MARKER_PREFIX}{digest}:{sif_path}"


def is_apptainer_marker(marker: str | None) -> bool:
    """Does this lock entry carry an apptainer marker at all?

    SEPARATE FROM `parse_apptainer_marker` ON PURPOSE. Selecting entries with
    the parser would treat a MALFORMED marker (`apptainer:sha256::/path`, empty
    hex) as "not a marker" and skip it — turning a refusal into a silent
    proceed. Selection asks "does this claim to be an apptainer marker"; the
    parser asks "what hex does it carry". A malformed one must still be
    SELECTED, so it can then fail the comparison.
    """
    return bool(marker) and marker.startswith(APPTAINER_MARKER_PREFIX)


def parse_apptainer_marker(marker: str | None) -> str | None:
    """Return the recorded hex from a marker, or None if it is not one.

    The counterpart to `apptainer_marker`, so the build side and the verify
    side cannot disagree about the shape.
    """
    if not marker or not marker.startswith(APPTAINER_MARKER_PREFIX):
        return None
    return marker[len(APPTAINER_MARKER_PREFIX):].split(":", 1)[0]


def apptainer_marker_path(marker: str | None) -> str | None:
    """Return the .sif PATH a marker was recorded against, or None.

    THE OTHER HALF OF THE MARKER, and it stays load-bearing for a different
    reason than the one first written here. That reason was: the hpc-launcher
    takes this path ahead of the conventional filename, so a surface reading only
    the hex half can call an image healthy while the launcher looks for a file
    that is gone. BOTH resolvers now consult the recorded path LAST, so that is no
    longer true and the finding it justified was retired with it — a recorded path
    that is gone, with a matching hash at the conventional name, is what MOVING a
    state root leaves behind and every launch path runs that file.

    What the path half is still for: saying WHICH file a digest was recorded
    against when a mismatch is reported ("the record is for <other>, and <this>
    was resolved instead"), and letting the resolvers fall back to a `.sif` that
    exists only where botainer itself wrote it.
    """
    if not is_apptainer_marker(marker):
        return None
    rest = marker[len(APPTAINER_MARKER_PREFIX):]
    _, _, path = rest.partition(":")
    return path or None


def record_image_digest(plugin_name: str, image_id: str) -> None:
    """Update installed.lock with a freshly-built image digest.

    MOVED HERE FROM `cli/image.py`, unchanged. It lived next to
    ONE of the two commands that build an image: `botainer image build
    --runtime apptainer` recorded a marker and `botainer hpc build` did not, so
    `composition._verify_apptainer_sif_provenance` — whose first branch is "no
    marker, nothing to verify" — took its fail-open branch for every image
    built that way. Both ARE documented cluster routes (`image build --runtime
    apptainer` is what the HPC installer script runs; `hpc build` is what the
    getting-started guide's §4A says), so this was not "the cluster has no
    check" — it was "half your cluster users have no check, depending on which
    of two documented commands they followed".

    It is shared code rather than a second copy in `hpc.py` because a copy
    agrees on the day it is written and drifts afterwards — and this particular
    body carries three things a reimplementation loses: the fields are written
    explicitly (not `json.dumps(e.__dict__)`), the write goes through
    `write_secure` for atomic-rename plus restrictive perms at create, and the
    plugin-not-found branch ADDS a minimal entry instead of giving up.
    """
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    lock_path = paths.installed_lock_path
    # AUDIT (MEDIUM): hold the installed.lock flock across the whole
    # read-modify-write so a concurrent append_lock() (e.g. `plugin add`) can't
    # be clobbered by this rewrite (lost update). append_lock takes the same
    # lock; this nests under it via the single guard file.
    with lock_for(lock_path):
        record_image_digest_locked(plugin_name, image_id, lock_path)


def record_image_digest_locked(plugin_name: str, image_id: str,
                               lock_path: Path) -> None:
    """The read-modify-write half. Caller MUST already hold `lock_for`."""
    entries = read_lock(lock_path)
    new_entries = []
    found = False
    for e in entries:
        if e.name == plugin_name:
            # Replace the entry with updated image_digest.
            new_entries.append(ProvenanceEntry(
                name=e.name,
                version=e.version,
                source=e.source,
                tree_sha=e.tree_sha,
                image_digest=image_id,
                installed_at=e.installed_at,
                tier=e.tier,
            ))
            found = True
        else:
            new_entries.append(e)
    if not found:
        # Plugin wasn't recorded (rare; would mean install_bundled didn't
        # add it). Add a minimal entry.
        new_entries.append(ProvenanceEntry(
            name=plugin_name,
            version="?",
            source="image-built-locally",
            tree_sha="sha256:0",
            image_digest=image_id,
            # `now_iso()`, NOT a second call to datetime. This branch wrote
            # `2026-09-12T13:31:50.645956+00:00` while every other writer in the
            # tree wrote `2026-09-12T13:31:50Z`, so one lock file could hold both
            # shapes. Nothing compared them, which is why it survived — but the
            # two do not sort together: '.' (0x2E) is below 'Z' (0x5A), so a
            # lexical sort puts a microsecond-bearing stamp BEFORE a whole-second
            # one from the same moment. One formatter, one shape.
            installed_at=now_iso(),
            tier="first-party",
        ))
    # Atomic rewrite as JSONL (matches provenance.read_lock format).
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for e in new_entries:
        lines.append(json.dumps({
            "name": e.name,
            "version": e.version,
            "source": e.source,
            "tree_sha": e.tree_sha,
            "image_digest": e.image_digest,
            "installed_at": e.installed_at,
            "tier": e.tier,
        }, sort_keys=True))
    # Task #264 + #265: atomic-rename + restrictive perms at create.
    from botainer.state.secure_write import write_secure
    write_secure(lock_path, "\n".join(lines) + "\n", mode=0o644)


def forget_image_digest(plugin_name: str) -> bool:
    """Clear a plugin's recorded image digest. True if there was one to clear.

    THE REMEDY THE REFUSAL NAMES. When a .sif no longer matches its recorded
    sha256, composition refuses and tells the user to "remove the stale
    installed.lock entry if this change is intentional" — which, until this
    existed, meant hand-editing the product's own state file. A surface that
    names a remedy the product does not provide is the defect, not the user's
    problem.

    Deliberately replacing an image IS legitimate: the HPC guide documents
    building a .sif on a workstation and copying it to a cluster that forbids
    login-node builds. That path records no marker of its own, so without a way
    to drop the old one the only route back was another compute-node
    allocation and a 10-20 minute rebuild.

    Clears ONLY an existing entry — unlike `record_image_digest`, it never adds
    a minimal one, because "forget what I never recorded" has nothing to write.
    """
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    lock_path = paths.installed_lock_path
    with lock_for(lock_path):
        entries = read_lock(lock_path)
        if not any(e.name == plugin_name and e.image_digest for e in entries):
            return False
        record_image_digest_locked(plugin_name, None, lock_path)
    return True


def record_apptainer_sif(plugin_name: str, sif_path: Path) -> str:
    """Hash a just-built .sif, record it, return the hex digest.

    THE STEP BOTH BUILDERS OWE. `image build --runtime apptainer` did it
    inline; `hpc build` did not. One function so they cannot diverge
    again, and so the verify side has exactly one shape to parse.
    """
    digest = sha256_file(sif_path)
    record_image_digest(plugin_name, apptainer_marker(sif_path, digest))
    return digest
