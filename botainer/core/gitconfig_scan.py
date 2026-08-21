"""Section-aware `.git/config` host-code-execution scanner.

ONE audited parser, shared by BOTH the git plugin (session guarded mode) and the
HPC job dispatcher (`hpc/jobs.py::_child_git_binds`). Before the job
path had a WEAKER hand-rolled substring copy that missed sectioned keys entirely
(`core.pager` never appears literally in `[core]\n\tpager = …`) and the
`[include]` indirection — sharp-edges re-audit F1. Keep this the single source.

A repo-local git config can weaponize git into running arbitrary host commands
(as the USER, outside any container) the next time git touches the repo. These
keys are the host-code-execution surface a git-shareable repo has no legitimate
reason to dictate.
"""
from __future__ import annotations

import re
from pathlib import Path

_SECTION_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"(.*)")?\s*\]\s*$')
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*?))?\s*$")

# core.fsmonitor accepts a boolean (true/false — safe) OR a hook-program path
# (dangerous). A bare boolean value is NOT flagged.
_FSMONITOR_SAFE_VALUES = {"true", "false", ""}

# (section, key) pairs whose VALUE is a command/path git runs on the host.
DANGEROUS_KEYS: set[tuple[str, str]] = {
    ("core", "hookspath"),     # redirects hook lookup away from any ro overlay
    ("core", "sshcommand"),    # runs on fetch/push
    ("core", "fsmonitor"),     # runs on status/most ops (unless boolean)
    ("core", "pager"),         # runs on log/diff/...
    ("core", "editor"),        # runs on commit/rebase/...
    ("core", "askpass"),       # runs on auth prompts
    ("core", "gitproxy"),      # runs on fetch
    ("sequence", "editor"),    # runs on rebase -i
}
# Keys dangerous in ANY subsection (section, key), subsection wildcard:
DANGEROUS_SUBSECTION_KEYS: set[tuple[str, str]] = {
    ("credential", "helper"),  # credential.<url>.helper — runs on auth
    ("gpg", "program"),        # gpg.<fmt>.program — runs on sign/verify
    ("filter", "clean"),       # filter.<name>.clean/smudge/process
    ("filter", "smudge"),
    ("filter", "process"),
    ("diff", "command"),       # diff.<name>.command/textconv
    ("diff", "textconv"),
    ("merge", "driver"),       # merge.<name>.driver
}
# Sections that pull in an EXTERNAL config file (which could set any of the
# above from an agent-writable path outside a ro .git/config overlay).
DANGEROUS_SECTIONS = {"include", "includeif"}


def dangerous_git_config_keys(config_path: Path) -> list[str]:
    """Return human labels for host-code-execution settings in a `.git/config`.

    Section-aware parse (git's INI-with-subsections). Catches `[include]`/
    `[includeIf]`, the command-valued core/sequence keys, the credential/gpg/
    filter/diff/merge driver keys in any subsection, and `alias.* = !shell`. A
    boolean `core.fsmonitor` is NOT flagged. Empty list = clean.
    """
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    section = ""
    for raw in text.splitlines():
        line = raw
        sm = _SECTION_RE.match(line)
        if sm:
            section = sm.group(1).lower()
            if section in DANGEROUS_SECTIONS:
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
            continue
        if (section, key) in DANGEROUS_KEYS or (section, key) in DANGEROUS_SUBSECTION_KEYS:
            found.append(f"{section}.{key}")
        elif section == "alias" and value.startswith("!"):
            found.append(f"alias.{key} (shell command)")
    return found
