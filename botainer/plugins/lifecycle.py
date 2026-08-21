"""Plugin enable/disable and listing.

Enable/disable are per-project state, written to `.botainer/config.yaml`
(`plugins_enabled:` list). Installation is host-wide.

Source-of-truth in editable installs
====================================
For end-users running a pip-installed wheel, the plugin tree lives at
`<state_root>/plugins/<name>/`. `botainer setup` copies bundled plugins
from the wheel's resources into that tree.

For developers running `pip install -e <clone>`, the plugin tree is
ALSO at `<state_root>/plugins/<name>/` — but the CLONE has the canonical
source at `<clone>/plugins/<name>/`. The two trees can drift if a dev
edits source without re-running `botainer setup`. That drift was the
source of multiple "my fix isn't running" debugging episodes during
v0.1.0 development.

Resolution: in editable-install mode, source overrides installed for
plugins that exist in both trees. Third-party plugins (in installed
only) are unaffected. Production wheel installs hit the installed
tree as before.

Detection: walk up from this module's __file__ looking for a
pyproject.toml whose [project].name == "botainer". If found, the
sibling `plugins/` dir is the source plugin tree.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import yaml

from botainer.core.refusal import RefusalCategory, Refused
from botainer.plugins.manifest import MANIFEST_FILENAME, load_manifest
from botainer.plugins.provenance import read_lock
from botainer.state import dir as state_dir


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    version: str
    tier: str
    source: str
    plugin_dir: Path


@functools.lru_cache(maxsize=1)
def _editable_source_plugins_root() -> Path | None:
    """If botainer is editable-installed from a clone, return the
    source's `plugins/` directory. Otherwise None.

    Cached — the answer doesn't change in a process lifetime. Walking
    parents on every `list_installed()` call would be cheap but wasteful
    and could surprise tests that monkey-patch __file__.
    """
    from botainer import __file__ as botainer_init
    here = Path(botainer_init).resolve()
    # botainer/__init__.py → botainer/ → <clone>/. Check up to 6 levels
    # as a defensive bound (no real project nests this deep).
    for parent in list(here.parents)[:6]:
        pyproject = parent / "pyproject.toml"
        if not pyproject.exists():
            continue
        try:
            content = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        # Cheap text check; avoids importing tomllib on every startup.
        # Both quoting styles are valid TOML so accept either.
        if 'name = "botainer"' not in content and "name = 'botainer'" not in content:
            continue
        plugins_root = parent / "plugins"
        if plugins_root.is_dir():
            return plugins_root
        return None
    return None


def list_installed() -> list[InstalledPlugin]:
    """List installed plugins. In editable-install mode, source overrides
    installed for names present in both."""
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    by_name_in_lock = {e.name: e for e in read_lock(paths.installed_lock_path)}

    # 1. Collect candidate dirs from the installed tree (~/.botainer/plugins/).
    candidates: dict[str, Path] = {}
    if paths.plugins_dir.exists():
        for child in sorted(paths.plugins_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / MANIFEST_FILENAME).exists():
                candidates[child.name] = child

    # 2. In editable-install mode, overlay source plugins. Source wins.
    source_root = _editable_source_plugins_root()
    if source_root is not None:
        for child in sorted(source_root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / MANIFEST_FILENAME).exists():
                candidates[child.name] = child  # source overrides installed

    out: list[InstalledPlugin] = []
    for name in sorted(candidates):
        child = candidates[name]
        try:
            m = load_manifest(child)
        except Refused:
            continue
        lock = by_name_in_lock.get(m.name)
        is_editable_source = (source_root is not None
                              and child.is_relative_to(source_root))
        if is_editable_source:
            source_label = f"editable:{source_root.parent.name}/plugins"
        else:
            source_label = lock.source if lock else "(unrecorded)"
        # AUDIT (MEDIUM): show the LAUNCHER-determined tier recorded
        # at install (lock.tier), not the manifest's self-declared m.tier — else
        # a plugin gated as third-party could still DISPLAY a self-declared
        # `community-verified`.
        #
        # SECURITY (audit, S2): the old fallback was
        # `lock.tier if lock else m.tier`, i.e. ANY directory with no lock entry
        # got the tier it claimed about ITSELF. `cp -r` a tree declaring
        # `tier: first-party` into ~/.botainer/plugins/ and it was treated as
        # first-party by the compose-time ceiling and hook registration —
        # bypassing the entire install.py gate stack, and contradicting
        # trust.py's own "trust comes from a launcher-shipped allowlist, not a
        # self-declared manifest field".
        #
        # Trust must come from a MECHANISM. There are exactly two:
        #   1. a lock entry  → the launcher decided this tier at install time.
        #   2. the editable source tree → located by walking up from
        #      botainer/__init__.py to the pyproject.toml that names botainer,
        #      i.e. this IS the launcher's own shipped tree, not a claim.
        # Anything else has NO evidence, so it gets the lowest tier and the
        # default policy ceiling (allowed_tiers: ["first-party"]) refuses it.
        if lock is not None:
            tier = lock.tier
        elif is_editable_source:
            tier = m.tier
        else:
            tier = "third-party"
        out.append(
            InstalledPlugin(
                name=m.name,
                version=m.version,
                tier=tier,
                source=source_label,
                plugin_dir=child,
            )
        )
    return out


def enable(project_root: Path, plugin_name: str) -> None:
    config_path = _require_config(project_root)
    # AUDIT (H9): this previously called verify_plugin inside a
    # try/except expecting a Refused(TAMPER_DETECTED) on a hash mismatch — but
    # verify_plugin RETURNS a (tier, detail) tuple and NEVER raises (trust.py),
    # so the return was discarded, the except was dead, and the comment's
    # "Mismatch raises ... TAMPER_DETECTED" was false: enable did NO
    # verification. Rather than re-add a runtime/enable-time hash check (the
    # launcher-written lock is the wrong integrity boundary — see the detailed
    # reasoning in composition.compose_session; an attacker with FS write to
    # the plugin dir also has write to the lock, and editable/dev installs
    # drift the hash → chronic false positives), plugin-tree integrity is
    # deferred to install-time wheel-signature verification (v0.2). enable just
    # records the plugin as enabled; the manifest is validated on load.
    data = yaml.safe_load(config_path.read_text()) or {}
    enabled = list(data.get("plugins_enabled") or [])
    if plugin_name not in enabled:
        enabled.append(plugin_name)
    _write_plugins_enabled(config_path, enabled)


def disable(project_root: Path, plugin_name: str) -> None:
    config_path = _require_config(project_root)
    data = yaml.safe_load(config_path.read_text()) or {}
    enabled = [p for p in (data.get("plugins_enabled") or []) if p != plugin_name]
    _write_plugins_enabled(config_path, enabled)


def _require_config(project_root: Path) -> Path:
    config_path = project_root / ".botainer" / "config.yaml"
    if not config_path.exists():
        raise Refused(
            RefusalCategory.CONFIG_MISSING,
            f"no .botainer/config.yaml in {project_root}; run `botainer init`",
        )
    return config_path


def _write_plugins_enabled(config_path: Path, enabled: list[str]) -> None:
    """Update the `plugins_enabled:` list in place, preserving comments.

    Implementation-review HIGH 6: previous logic round-tripped the
    whole file through yaml.safe_load → yaml.safe_dump, which strips
    comments. Users routinely document why a plugin is enabled
    inline (`- nudge  # see GETTING_STARTED nudge tradeoff`); losing
    those comments on `botainer plugin enable/disable` is a real
    data-loss bug.

    Strategy: surgically replace just the `plugins_enabled:` block.
    Find the block start, find its end (next top-level key or EOF),
    rewrite the list, leave everything else alone.

    If we can't locate a `plugins_enabled:` line (i.e., the user
    hand-wrote a config without it), append a new block at EOF.
    """
    source = config_path.read_text()
    new_block_lines = ["plugins_enabled:"]
    if enabled:
        for name in enabled:
            new_block_lines.append(f"  - {name}")
    else:
        new_block_lines.append("  []")
    new_block = "\n".join(new_block_lines) + "\n"

    lines = source.splitlines(keepends=True)
    block_start = None
    block_end = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # Top-level (no indent) `plugins_enabled:` line
        if (
            line.startswith("plugins_enabled:")
            and stripped.startswith("plugins_enabled:")
        ):
            block_start = i
            block_end = i + 1
            # Consume continuation: indented or blank lines.
            while block_end < len(lines):
                nxt = lines[block_end]
                if nxt.strip() == "":
                    # blank line — could be inside block or end of block
                    # If next non-blank is indented, keep going; else stop.
                    j = block_end + 1
                    while j < len(lines) and lines[j].strip() == "":
                        j += 1
                    if j < len(lines) and lines[j].startswith((" ", "\t", "-")):
                        block_end = j
                        continue
                    break
                if nxt.startswith((" ", "\t", "-")):
                    block_end += 1
                    continue
                break
            break

    if block_start is None:
        # No existing block — append.
        suffix = "" if source.endswith("\n") else "\n"
        config_path.write_text(source + suffix + new_block, encoding="utf-8")
        return

    new_source = "".join(lines[:block_start]) + new_block + "".join(lines[block_end:])
    config_path.write_text(new_source, encoding="utf-8")
