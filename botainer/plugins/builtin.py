"""Built-in plugin discovery and installation.

For v0.1.0, first-party plugins ship inside the botainer package:
- In dev (editable install): they live at `<repo>/plugins/<name>/`.
- After `pip install`: they live at `<site-packages>/botainer/_builtin_plugins/<name>/`.

This module discovers bundled plugins and installs them into the user's
state dir at `~/.botainer/plugins/<name>/` so the rest of the launcher
sees them like any other installed plugin.

Bundled installation bypasses the third-party install gates (tier, source
origin check, consent prompts) because the trust comes from the plugins
being shipped with the launcher itself. The launcher's `trusted_plugins.lock`
(when implemented in Phase 3) records the canonical tree hash for each
bundled plugin.

Per wave-1 review:
- No `BOTAINER_DEV` bypass.
- Discovery prefers the install-time location; falls back to the
  development repo location.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from botainer.plugins import provenance
from botainer.plugins.manifest import MANIFEST_FILENAME, load_manifest
from botainer.state import dir as state_dir

# Plugins shipped with the v0.1.0 launcher. Names must match the plugin
# directory under the source/install root.
BUILTIN_PLUGIN_NAMES = (
    "agent-claude",
    "agent-claude-broker",
    "agent-claude-proxy",
    "agent-claude-shared",
    "agent-codex",
    "agent-codex-broker",
    "agent-codex-shared",
    "browser",
    "git",
    "hpc-launcher",
    "hpc-modules",
    "nudge",
    "web-ports",
    "wolfram-sidecar",
)


def find_builtin_plugins_root() -> Path | None:
    """Locate the directory containing bundled plugin source trees.

    Tries (in order):
    1. `<package>/_builtin_plugins/` — the install-time location (a future
       packaging step copies plugins/ here at sdist/wheel build).
    2. `<repo-root>/plugins/` — for editable installs / development.

    Returns the directory if found, else None.
    """
    import botainer as _bot
    pkg_root = Path(_bot.__file__).resolve().parent

    # 1. Install-time location.
    install_loc = pkg_root / "_builtin_plugins"
    if install_loc.exists():
        return install_loc

    # 2. Dev / editable install.
    repo_root = pkg_root.parent
    dev_loc = repo_root / "plugins"
    if dev_loc.exists() and any(
        (dev_loc / n / MANIFEST_FILENAME).exists() for n in BUILTIN_PLUGIN_NAMES
    ):
        return dev_loc

    return None


def discover_builtin_plugins() -> list[Path]:
    """Return paths of bundled plugin source dirs known to ship with this launcher."""
    root = find_builtin_plugins_root()
    if root is None:
        return []
    out = []
    for name in BUILTIN_PLUGIN_NAMES:
        candidate = root / name
        if (candidate / MANIFEST_FILENAME).exists():
            out.append(candidate)
    return out


def install_bundled(source_dir: Path) -> provenance.ProvenanceEntry:
    """Install a bundled plugin (skips third-party gates).

    Task #168 + #217: bundled-plugin install BYPASSES site-policy
    plugins.allowed_tiers (--allow-tier on `botainer setup`). The site
    admin who set allowed_tiers=[first-party] would still get bundled
    plugins installed regardless. v0.1.0 design choice: bundled plugins
    are first-party by definition and DO comply with --allow-tier
    naturally; if the admin wants to refuse even bundled installs they
    can post-install with `botainer plugin disable <name>`.

    This still surfaces the bypass: bundled tier IS first-party, so
    'allowed_tiers contains first-party' should always be true for
    bundled installs to land. If allowed_tiers does NOT contain
    first-party (a deliberate admin choice to refuse all plugins),
    install_bundled now REFUSES rather than silently overriding.

    The plugin source is copied into `~/.botainer/plugins/<name>/`. A
    provenance entry is appended to `installed.lock` recording the tree
    hash. Re-running for an already-installed plugin overwrites in place
    (idempotent).
    """
    manifest = load_manifest(source_dir)
    # Task #168 + #217: refuse if site policy explicitly denies first-party.
    try:
        from botainer.core import policy as _policy_module
        site = _policy_module.load_site_policy()
        user = _policy_module.load_user_policy()
        eff = _policy_module.intersect(site, user)
        if "first-party" not in eff.plugins.allowed_tiers:
            raise RuntimeError(
                f"refused: site policy plugins.allowed_tiers="
                f"{eff.plugins.allowed_tiers} excludes first-party; "
                f"bundled plugin {manifest.name!r} not installed."
            )
    except Exception as exc:
        # If policy load fails entirely, fall back to old behavior with
        # a stderr note so the user knows the gate didn't run.
        if "first-party" not in str(exc):
            import sys as _sys
            _sys.stderr.write(
                f"[botainer] install_bundled: policy check skipped: {exc}\n"
            )
        else:
            raise
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    target = paths.plugins_dir / manifest.name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source_dir, target, symlinks=False)
    tree_sha = provenance.compute_tree_sha(target)
    entry = provenance.ProvenanceEntry(
        name=manifest.name,
        version=manifest.version,
        source=f"bundled:{source_dir.name}",
        tree_sha=f"sha256:{tree_sha}",
        image_digest=_image_digest_from_manifest(manifest),
        installed_at=provenance.now_iso(),
        tier="first-party",  # bundled is first-party by definition
    )
    provenance.append_lock(paths.installed_lock_path, entry)
    return entry


def install_all_builtin() -> list[provenance.ProvenanceEntry]:
    """Install every bundled plugin. Returns list of provenance entries."""
    return [install_bundled(p) for p in discover_builtin_plugins()]


def _image_digest_from_manifest(manifest: object) -> str | None:
    """Extract the image_digest hint from a plugin manifest, if any.

    Plugin authors put `image.tag: name@sha256:<digest>` in their manifest;
    we extract the digest from that tag for the installed.lock entry. If
    the plugin uses `image.source: dockerfile`, the digest is None until
    the image is built (Phase 1+).
    """
    image = getattr(manifest, "image", None)
    if image is None:
        return None
    tag = getattr(image, "tag", None)
    if not tag or "@" not in tag:
        return None
    return tag.split("@", 1)[1]  # e.g. "sha256:abc123..."
