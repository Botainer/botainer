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
edits source without re-running `botainer setup`, leaving the installed
plugin copy behind the source changes.

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


def enabled_names(project_root: Path) -> list[str]:
    """The project's `plugins_enabled`, in file order.

    Added because three callers wanted this and each was reading the YAML
    itself — `disable` inline (below), and `botainer plugin` twice, one of
    which read `Path.cwd()/.botainer/config.yaml` and so returned an empty
    list from any subdirectory. One reader, one answer.
    """
    config_path = _require_config(project_root)
    data = yaml.safe_load(config_path.read_text()) or {}
    return list(data.get("plugins_enabled") or [])


def disable(project_root: Path, plugin_name: str) -> None:
    config_path = _require_config(project_root)
    enabled = [p for p in enabled_names(project_root) if p != plugin_name]
    _write_plugins_enabled(config_path, enabled)


def swap(project_root: Path, *, remove: "set[str] | list[str]",
         add: "set[str] | list[str]") -> None:
    """Apply a whole set change in ONE write.

    `auth use <mode>` is a SWAP — one agent plugin out, its sibling in — and it
    used to run `disable()` then `enable()`, two writes with a state between
    them in which the project has no agent plugin at all. That intermediate is
    not hypothetical: it is what wrote our own `[]` marker, and if anything
    stops the second write (a refusal, a full disk, a signal) the project is
    left in a mode the user never chose, with `auth use` having reported
    nothing. One write has no in-between to be interrupted in.

    Order is preserved for names that stay, so comments on their lines survive;
    additions append in the order given.
    """
    config_path = _require_config(project_root)
    remove_set, add_list = set(remove), list(add)
    enabled = [p for p in enabled_names(project_root) if p not in remove_set]
    for name in add_list:
        if name not in enabled:
            enabled.append(name)
    _write_plugins_enabled(config_path, enabled)


def _require_config(project_root: Path) -> Path:
    config_path = project_root / ".botainer" / "config.yaml"
    if not config_path.exists():
        raise Refused(
            RefusalCategory.CONFIG_MISSING,
            f"no .botainer/config.yaml in {project_root}; run `botainer init`",
        )
    return config_path


def _item_name(line: str) -> str | None:
    """The plugin name on a `  - name  # comment` line, or None if not an item.

    Only the name is parsed; everything after it is opaque and is carried
    through untouched, which is the whole point.
    """
    stripped = line.strip()
    if not stripped.startswith("- "):
        return None
    rest = stripped[2:].strip()
    if not rest or rest.startswith("#"):
        return None
    return rest.split("#", 1)[0].strip() or None


def _rebuild_block(body: "list[str]", enabled: "list[str]") -> str:
    """Rebuild `plugins_enabled:` from the EXISTING lines, not from the names.

    Building it from `enabled` alone is what destroyed every inline comment;
    the names are all that survived that round-trip. Here the original line is
    the unit that moves, so anything the user wrote on it moves with it.
    """
    keep = set(enabled)
    out: "list[str]" = ["plugins_enabled:\n"]
    seen: set[str] = set()
    dropping = False          # inside a removed item's trailing comment block

    for raw in body:
        if raw.strip() == "[]":
            # OUR OWN empty marker from a previous write, not user content.
            # Keeping it produced `plugins_enabled:` / `  []` / `  - name` —
            # invalid YAML that `auth use <mode>` wrote while exiting 0,
            # leaving the project unloadable. Reachable whenever the list is
            # emptied and refilled, which is exactly what a mode switch does
            # when the agent plugin is the only entry.
            continue
        name = _item_name(raw)
        if name is not None:
            dropping = name not in keep
            if not dropping:
                out.append(raw if raw.endswith("\n") else raw + "\n")
                seen.add(name)
            continue
        if raw.strip().startswith("#"):
            # A comment line: belongs to the item above it if there was one.
            if not dropping:
                out.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        if not raw.strip():
            dropping = False   # a blank line ends an item's comment block
            out.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        # Anything else inside the block (unlikely): keep it rather than guess.
        dropping = False
        out.append(raw if raw.endswith("\n") else raw + "\n")

    # Newly enabled names, in the order the caller gave them.
    for name in enabled:
        if name not in seen:
            out.append(f"  - {name}\n")

    if len(out) == 1:                       # nothing enabled at all
        out.append("  []\n")
    return "".join(out)


def _write_plugins_enabled(config_path: Path, enabled: list[str]) -> None:
    """Update the `plugins_enabled:` list in place, preserving comments.

    Implementation-review HIGH 6: previous logic round-tripped the whole file
    through yaml.safe_load → yaml.safe_dump, which strips comments. Users
    routinely document why a plugin is enabled inline (`- nudge  # see
    GETTING_STARTED nudge tradeoff`); losing those comments on `botainer plugin
    enable/disable` is a real data-loss bug.

    THAT FIX WAS ONLY HALF APPLIED, and this docstring asserted the whole thing
    for months. Replacing the block instead of the file did save comments
    ELSEWHERE in config.yaml — but the replacement block was rebuilt from
    scratch as bare `  - name` lines, so every comment INSIDE the block still
    died, including the example above. Observed by running `botainer plugin
    enable` on a default `botainer init` project: the note explaining that the
    project PINNED its auth mode at init (the only place a user is told that)
    and the entire commented opt-in plugin menu were both gone, with no warning.

    Strategy: surgically replace just the `plugins_enabled:` block, and build
    the replacement FROM THE EXISTING LINES rather than from the names —

      * a name that is staying keeps its original line, byte for byte, so
        whatever the user wrote after it survives;
      * a standalone comment line inside the block is kept in place;
      * a name that is going takes its own trailing comment with it, which is
        right: that comment was about that plugin;
      * a genuinely new name is appended as a plain `  - name`.

    If we can't locate a `plugins_enabled:` line (i.e., the user hand-wrote a
    config without it), append a new block at EOF.
    """
    source = config_path.read_text()

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

    new_block = _rebuild_block(
        lines[block_start + 1:block_end] if block_start is not None else [],
        enabled)

    if block_start is None:
        # No existing block — append.
        suffix = "" if source.endswith("\n") else "\n"
        _write_verified(config_path, source + suffix + new_block, enabled)
        return

    new_source = "".join(lines[:block_start]) + new_block + "".join(lines[block_end:])
    _write_verified(config_path, new_source, enabled)


def _write_verified(config_path: Path, new_source: str, enabled: list[str]) -> None:
    """Parse what we are about to write, and refuse rather than write it broken.

    This is a TEXT editor for a YAML file — it splices a block rather than
    round-tripping through a parser, deliberately, because a parser eats the
    user's comments. The cost of that choice is that no parser ever sees the
    result, so a splicing bug becomes a config nobody can load, written by a
    command that exited 0. That happened: `auth use <mode>` on a project whose
    only enabled plugin was the agent left `[]` and a block item in the same
    list, and `config check` could no longer read the project at all.

    So the writer now reads its own output BEFORE the file changes. Nothing
    partial reaches disk: on a parse failure the original file is untouched and
    the caller gets a refusal naming the file, which is recoverable — the
    broken-file version was not.
    """
    import yaml

    try:
        parsed = yaml.safe_load(new_source)
    except yaml.YAMLError as exc:
        raise Refused(
            RefusalCategory.CONFIG_INVALID,
            f"refusing to write {config_path}: the edit would not parse as "
            f"YAML ({exc.__class__.__name__}). The file is UNCHANGED. This is "
            f"a botainer bug, not something you did — please report it with "
            f"the plugin list you were setting.",
        ) from exc
    got = (parsed or {}).get("plugins_enabled")
    # SETS, not sorted lists, and the honest reason: `plugins_enabled` IS a set
    # of names — the model accepts a repeat and every reader dedupes — so a
    # multiset comparison would refuse a caller that passed `{...}` or a deduped
    # list against a file that happens to list a name twice.
    #
    # NOT a live regression, and I checked rather than claiming: all three
    # callers build their list FROM the file (`enable`, `disable`, `swap`), so
    # duplicates survive on both sides and the multiset form passed too. It is
    # unreachable today and a set is still the right comparison, because the
    # next caller doing the natural thing must not be refused. What the check is
    # FOR is a rebuild that drops or invents a name, and a set catches that.
    if set(got or []) != set(enabled):
        raise Refused(
            RefusalCategory.CONFIG_INVALID,
            f"refusing to write {config_path}: after the edit the file would "
            f"say plugins_enabled={sorted(set(got or []))}, not "
            f"{sorted(set(enabled))}. The file is UNCHANGED. This is a "
            f"botainer bug, not something you did — please report it.",
        )
    config_path.write_text(new_source, encoding="utf-8")
