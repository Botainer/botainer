#!/usr/bin/env python3
"""git plugin — pre_session hook (guarded mode).

AUDIT (H6): the git plugin shipped with `hooks: []`, so its
advertised "guarded mode" (read-only overlay on `.git/hooks/`) did NOTHING —
the default config told the user they were protected while the agent could
freely install host-executed git hooks (agent writes `.git/hooks/pre-commit`
→ user runs git on the host → arbitrary code runs as the user). This hook
implements guarded mode per DN-027 §8.2.

Guarded mode (the default) contributes read-only overlays so the agent cannot
tamper with the host-executed bits of the repo:
  - `/workspace/.git/hooks` (ro): the agent cannot write hook scripts.
  - `/workspace/.git/config` (ro): the agent cannot ADD `core.hooksPath`/
    `core.sshCommand`/`core.fsmonitor` at runtime to bypass the hooks overlay.
    (In-container git still writes transient config to the container's
    ephemeral `~/.gitconfig`; only the repo-local config is frozen.)
And it REFUSES the session if `.git/config` ALREADY contains a dangerous key
(`core.hooksPath` / `core.sshCommand` / `core.fsmonitor`) — those are
host-code-execution vectors that the overlay alone cannot neutralize.

`mode: off` contributes nothing (the agent has rw on `.git/`); the start banner
is expected to warn (DN-028 §7).

TO CONSIDER WHEN THIS PLUGIN IS NEXT WORKED ON — the rest of the clone-risk
class. This plugin currently guards the git-specific execution vectors
(`core.hooksPath`, `core.sshCommand`, `core.fsmonitor`, `.git/hooks`). A clone
carries others that act when something runs them, and they are the same class
rather than separate surfaces:

    .mcp.json              MCP servers, i.e. tool names + descriptions into the
                           model's schema (ALLOWED by decision 2026-08-27 — see
                           docs/CAPABILITY-SURFACE.md §4at for why singling it
                           out would cost the feature and buy little)
    package.json           `postinstall` and friends, on `npm install`
    Makefile / configure   whatever the agent is asked to run
    pyproject / setup.py   arbitrary code on build or editable install
    .envrc, .vscode/, .devcontainer/

None is an escape — under `bypass` the agent already runs commands in the cage.
They are content that STEERS. Worth deciding here whether this plugin's remit is
"git's own execution vectors" (today) or "what a clone brings", because the
second is a different and larger job, and the answer changes what belongs in
this file. Mitigation research is tracked separately.

This runs on the host as the user (like every pre_session hook). The overlay
binds are applied by the adapter on the docker/direct path; on the HPC sbatch
path plugin-contributed binds share the general outer-argv routing limitation
(H2/#160) — but the `.git/config` dangerous-key REFUSAL below works on every
runtime (it aborts the session regardless of how binds are applied).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

# Git config keys (case-insensitive) that enable host code execution and must
# never be present in a guarded repo. core.hooksPath redirects hook lookup away
# from the frozen .git/hooks; core.sshCommand runs on fetch/push; core.fsmonitor
# runs on status/most operations.
_SECTION_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"(.*)")?\s*\]\s*$')
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*?))?\s*$")

# core.fsmonitor accepts a boolean (true/false — safe; selects builtin or
# disables) OR a hook-program path (dangerous). So a bare boolean fsmonitor
# value is NOT refused.
_FSMONITOR_SAFE_VALUES = {"true", "false", ""}

# Git config (section, key) pairs whose VALUE is a command/path that git runs
# on the host when YOU use git — the host-code-execution surface a guarded
# (git-shareable) repo has no legitimate reason to dictate. The agent has rw on
# /workspace/.git, so the ro .git/config overlay stops it ADDING these at
# runtime; this refusal catches a config that ALREADY carries them (incl. via
# a prior poisoned session) before we freeze it. `*` = any subsection.
#   key  : (section, key)  — section "*" means match the key in any section.
_DANGEROUS_KEYS: set[tuple[str, str]] = {
    ("core", "hookspath"),     # redirects hook lookup away from the ro overlay
    ("core", "sshcommand"),    # runs on fetch/push
    ("core", "fsmonitor"),     # runs on status/most ops (unless boolean — handled)
    ("core", "pager"),         # runs on log/diff/...
    ("core", "editor"),        # runs on commit/rebase/...
    ("core", "askpass"),       # runs on auth prompts
    ("core", "gitproxy"),      # runs on fetch
    ("sequence", "editor"),    # runs on rebase -i
}
# Keys dangerous in ANY subsection (section, key), subsection wildcard:
_DANGEROUS_SUBSECTION_KEYS: set[tuple[str, str]] = {
    ("credential", "helper"),  # credential.<url>.helper — runs on auth
    ("gpg", "program"),        # gpg.<fmt>.program — runs on sign/verify
    ("filter", "clean"),       # filter.<name>.clean/smudge/process — runs on checkout/add
    ("filter", "smudge"),
    ("filter", "process"),
    ("diff", "command"),       # diff.<name>.command/textconv — runs on diff
    ("diff", "textconv"),
    ("merge", "driver"),       # merge.<name>.driver — runs on merge
}
# Sections that pull in an EXTERNAL config file (which could set any of the
# above) — refused outright in guarded mode (the included file is a separate,
# agent-writable path, so it bypasses the .git/config ro overlay).
_DANGEROUS_SECTIONS = {"include", "includeif"}


# YAML 1.1 parses these BARE words as booleans, not strings. `off` -> False.
#
# This was a live defect, measured 2026-08-31: the refusal below tells the user
# "or set plugins.git.mode=off to opt out (you then own the risk)", they write
# exactly that, YAML hands us False, the old `mode in ("guarded", "off")` test
# rejects it, and the fallback silently returns "guarded". The documented escape
# hatch never worked, and the `if mode == "off"` branch was unreachable for
# anyone following the instructions.
#
# It failed SAFE, which is why nobody noticed — and that is not a reason to
# downgrade it. A remedy the product NAMES and that silently does nothing
# teaches the user the product is broken, and the next thing they try is
# dropping `git` from plugins_enabled, which removes the guard entirely.
#
# The project already knew about this trap: botainer/cli/config_cmd.py and
# botainer/cli/policy.py both enumerate on/On/ON/off/Off/OFF/yes/no for exactly
# this reason, and config_cmd's --raw help says "Useful for 'no', 'yes', 'off',
# '0' that would otherwise coerce". The handling existed in the CLI and not in
# the plugin that prints the value in its own error text.
_OFF_SPELLINGS = {False, "off", "false", "no", "0", 0, "disabled"}


def _read_mode(project_root: Path) -> str:
    """Read plugins.git.mode from .botainer/config.yaml. Default 'guarded'.

    NORMALISES rather than matching exact strings, because the value the user is
    TOLD to write (`off`) does not survive YAML as a string. Anything meaning
    off is off; anything unrecognised is guarded, and says so on stderr instead
    of silently choosing the safe branch — a silent fallback to the safe value
    is what hid the original bug for as long as it lasted.
    """
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return "guarded"
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return "guarded"
    git_cfg = ((data.get("plugins") or {}).get("git") or {})
    mode = git_cfg.get("mode", "guarded")
    if isinstance(mode, str):
        mode = mode.strip().lower()
    if mode in _OFF_SPELLINGS:
        return "off"
    if mode in ("guarded", True, "on", "true", "yes"):
        return "guarded"
    sys.stderr.write(
        f"git: plugins.git.mode={mode!r} is not a value I recognise; using "
        f"'guarded'. Write `mode: off` to opt out, or remove the line.\n")
    return "guarded"


def _dangerous_git_config_keys(config_path: Path) -> list[str]:
    """Return human labels for host-code-execution settings in a .git/config.

    Section-aware parse (robust to git's INI-with-subsections format). Catches:
    `[include]`/`[includeIf]` (external-config indirection), the command-valued
    core/sequence keys, and the credential/gpg/filter/diff/merge driver keys in
    any subsection. fsmonitor with a boolean value is NOT flagged.
    """
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    section = ""       # lowercased section name, e.g. "core", "filter"
    for raw in text.splitlines():
        # Strip inline comments (git: # or ; starting a token). Conservative:
        # only treat as comment if line starts with # or ; (values may contain
        # them); good enough for detecting the dangerous KEYS.
        line = raw
        sm = _SECTION_RE.match(line)
        if sm:
            section = sm.group(1).lower()
            if section in _DANGEROUS_SECTIONS:
                found.append(f"[{sm.group(1)}] (external-config indirection)")
            continue
        if line.lstrip().startswith(("#", ";")):
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key = kv.group(1).lower()
        value = (kv.group(2) or "").strip().strip('"').strip()
        if (section, key) == ("core", "fsmonitor") and value.lower() in _FSMONITOR_SAFE_VALUES:
            continue  # boolean fsmonitor is safe (no external program)
        if (section, key) in _DANGEROUS_KEYS or (section, key) in _DANGEROUS_SUBSECTION_KEYS:
            found.append(f"{section}.{key}")
        elif section == "alias" and value.startswith("!"):
            found.append(f"alias.{key} (shell command)")
    return found


def _emit(binds: list[dict]) -> None:
    sys.stdout.write(json.dumps({
        "version": "plugin-contribution-v1",
        "kind": "pre_session",
        "binds": binds,
    }))


def main() -> int:
    project_root_env = os.environ.get("BOTAINER_PROJECT_ROOT")
    if not project_root_env:
        sys.stderr.write("git: no BOTAINER_PROJECT_ROOT in env\n")
        return 1
    project_root = Path(project_root_env)

    mode = _read_mode(project_root)
    if mode == "off":
        # Permissive: no overlays. (The start banner warns; DN-028 §7.)
        _emit([])
        return 0

    git_path = project_root / ".git"
    if not git_path.exists():
        # Not a git repo — nothing to guard. Not an error.
        _emit([])
        return 0
    if not git_path.is_dir():
        # AUDIT (H6, adversarial-review HIGH): `.git` is a FILE — a
        # gitdir pointer used by worktrees and submodules. The live hooks/ +
        # config live at the pointed-to gitdir (elsewhere), so our overlays
        # would NOT protect them. Fail CLOSED rather than silently provide no
        # protection while the user believes guarded mode is active (the agent
        # could still plant a host-executed hook in the real gitdir).
        sys.stderr.write(
            f"git: refusing — {git_path} is a gitdir pointer (worktree or "
            f"submodule); guarded mode does not yet protect the pointed-to "
            f".git/hooks. Use a normal (non-worktree) checkout, or set "
            f"plugins.git.mode=off to proceed (you then own the risk).\n"
        )
        return 2
    git_dir = git_path

    # Guarded mode: refuse the session if the repo config ALREADY carries a
    # host-code-execution key. The overlay freezes .git/config, but a value
    # that is dangerous BEFORE we freeze it must be caught and refused.
    git_config = git_dir / "config"
    if git_config.is_file():
        bad = _dangerous_git_config_keys(git_config)
        if bad:
            sys.stderr.write(
                f"git: refusing — {git_config} sets {sorted(set(bad))}, which "
                f"run code on the host when you use git (core.hooksPath redirects "
                f"hook lookup; core.sshCommand runs on fetch/push; core.fsmonitor "
                f"runs on status). A git-shareable repo carrying these is the "
                f"exact threat guarded mode exists to stop. Remove them from "
                f".git/config, or set plugins.git.mode=off to opt out (you then "
                f"own the risk).\n"
            )
            return 2

    binds: list[dict] = []
    hooks_dir = git_dir / "hooks"
    if hooks_dir.is_dir():
        binds.append({
            "source": str(hooks_dir),
            "target": "/workspace/.git/hooks",
            "mode": "ro",
        })
    if git_config.is_file():
        binds.append({
            "source": str(git_config),
            "target": "/workspace/.git/config",
            "mode": "ro",
        })
    _emit(binds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
