"""Trust store for bundled (first-party) plugins.

Per sharp-edges C1 + prior-art review:
- Trust comes from a launcher-shipped allowlist, not a self-declared
  manifest field.
- The allowlist records `{plugin_name → sha256:<tree-hash>}` for each
  bundled plugin.
- At every `botainer start` (Phase 3+), the launcher re-hashes the
  plugin tree and compares. Match → first-party. Mismatch → "user-modified"
  (warning; future may drop privileges).
- For v0.1.0 prototype: trust file generated on first setup (records
  current hashes). In a real ship, this lock ships with the pip wheel
  itself (immutable).

NB: per simplification review, no `BOTAINER_DEV` bypass. If users want
to skip verification, they can rebuild and re-record (or edit the lock
file — visible action).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from botainer.plugins import provenance

TRUSTED_PLUGINS_LOCK_NAME = "trusted_plugins.lock"


@dataclass(frozen=True)
class TrustEntry:
    name: str
    tree_sha: str  # algorithm-prefixed ("sha256:...")


def lock_path_for_install() -> Path:
    """Where the trusted_plugins.lock lives in the launcher install.

    For v0.1.0 prototype: alongside the bundled plugins root. This is the
    immutable shipped-with-launcher trust file.
    """
    from botainer.plugins.builtin import find_builtin_plugins_root
    root = find_builtin_plugins_root()
    if root is None:
        raise FileNotFoundError("bundled plugins root not found; cannot locate trust lock")
    return root / TRUSTED_PLUGINS_LOCK_NAME


def generate_trust_lock() -> dict[str, str]:
    """Compute {plugin_name → sha256:<tree-hash>} for all bundled plugins.

    Real ship: this would run at pip-package-build time. For prototype:
    runs at first `botainer setup` and is cached.
    """
    from botainer.plugins.builtin import discover_builtin_plugins
    out: dict[str, str] = {}
    for src in discover_builtin_plugins():
        tree_sha = provenance.compute_tree_sha(src)
        out[src.name] = f"sha256:{tree_sha}"
    return out


def write_trust_lock(lock_path: Path, entries: dict[str, str]) -> None:
    """Persist a generated trust lock."""
    # Tasks #263 + #265: atomic-rename + restrictive perms at create.
    from botainer.state.secure_write import write_secure
    payload = {
        "version": "trusted-plugins-v1",
        "entries": entries,
    }
    write_secure(lock_path, json.dumps(payload, indent=2, sort_keys=True), mode=0o600)


def read_trust_lock(lock_path: Path) -> dict[str, str]:
    """Read a trust lock file. Returns empty dict if missing/malformed."""
    if not lock_path.exists():
        return {}
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {str(k): str(v) for k, v in entries.items()}


def ensure_trust_lock() -> dict[str, str]:
    """Return the current trust lock, generating it on first run if missing.

    In a real ship, the lock would already exist (shipped in the pip wheel).
    For the prototype we generate-and-cache.
    """
    try:
        lock_path = lock_path_for_install()
    except FileNotFoundError:
        return {}
    existing = read_trust_lock(lock_path)
    if existing:
        return existing
    generated = generate_trust_lock()
    if generated:
        write_trust_lock(lock_path, generated)
    return generated


def verify_plugin(name: str, installed_dir: Path) -> tuple[str, str]:
    """Verify the installed plugin tree against the trust lock.

    Returns (effective_tier, detail) where effective_tier is one of:
    - "first-party"        : tree matches the trust lock
    - "user-modified"      : trust lock has an entry but tree hash differs
    - "untrusted"          : no trust lock entry (third-party or not bundled)

    Task #143 SCOPE LIMIT: 'first-party' here means the source tree
    (manifest yaml, hook scripts, Dockerfile/.def files) matches the
    bundled hash. It does NOT mean the *built image* matches — a
    malicious base image, npm install, apt package, or curl-bash in
    %post can ship arbitrary code into the .sif / docker image even
    if the source-tree hash matches.

    For full image-binary trust we would need:
      - reproducible image builds (currently #144 is open)
      - per-built-image hash in installed.lock (separate from source-
        tree hash here)
      - re-verify on every container start
    All v0.2 work; v0.1.0 trust is source-only.
    """
    trust = ensure_trust_lock()
    expected = trust.get(name)
    if expected is None:
        return ("untrusted", f"{name} is not in the trust lock")
    actual_hash = provenance.compute_tree_sha(installed_dir)
    actual = f"sha256:{actual_hash}"
    if actual == expected:
        return ("first-party", "tree hash matches the trust lock")
    return (
        "user-modified",
        f"tree hash {actual[:20]}... differs from expected {expected[:20]}...",
    )
