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
