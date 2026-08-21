"""Diagnose why agent job-dispatch is (not) wired for a project.

The job-dispatch chain has several SILENT failure points — `job_profiles` in the
wrong place, the config file the user edits not being the one the launcher reads,
a stale/absent hpc-launcher plugin. Each one leaves the caged agent with no
`botainer-job` command and no jobs instructions, with no error. This module walks
the whole chain and returns PLAIN-ENGLISH lines (the exact thing the user should
be able to run instead of pasting python one-liners) — CLI wiring is in
`cli/hpc.py` (`botainer hpc jobs-doctor`).

Kept dependency-light + pure (returns strings, doesn't print) so it is unit
tested against every failure mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_REL = ".botainer/config.yaml"
_BOTAINER_JOB_TARGET = "/usr/local/bin/botainer-job"


def diagnose(project_root: Path) -> tuple[bool, list[str]]:
    """Return (job_dispatch_will_work, report_lines).

    Walks: config file → job_profiles → hpc-launcher plugin → botainer-job CLI,
    and for each failure gives the specific cause + fix.
    """
    out: list[str] = []
    cfg_path = (project_root / CONFIG_REL).resolve()
    out.append(f"config file the launcher reads:  {cfg_path}")

    if not cfg_path.exists():
        out.append("  ✗ that file does not exist — you are not in a botainer "
                   "project dir, or you edited a config somewhere else.")
        return False, out

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        out.append(f"  ✗ YAML parse error (this alone breaks everything): {exc}")
        return False, out
    if not isinstance(raw, dict):
        out.append("  ✗ config top-level is not a mapping.")
        return False, out

    # ── job_profiles ──
    jp = raw.get("job_profiles")
    n = len(jp) if isinstance(jp, dict) else 0
    out.append(f"top-level job_profiles:          {n} "
               f"{'profile(s): ' + ', '.join(list(jp)[:8]) if n else '(none)'}")

    if n == 0:
        out.append("  ✗ NO usable top-level job_profiles. Looking for where they "
                   "went...")
        for line in _find_misplaced_profiles(raw):
            out.append(f"    → {line}")
        out.append("    FIX: `job_profiles:` must be a TOP-LEVEL key (column 0, no "
                   "indentation), with each profile indented 2 spaces under it. "
                   "See examples/hpc-job-profiles.yaml.")
        # keep walking so the report is complete, but this already fails.

    # ── hpc-launcher plugin + the botainer-job CLI it provides ──
    plugin_dir, why = _resolve_hpc_launcher()
    if plugin_dir is None:
        out.append(f"hpc-launcher plugin:             NOT resolved ({why})")
        out.append("  ✗ FIX: `botainer setup` (bundled reinstall) or, for an "
                   "editable install, sync the source so it's on disk.")
        botjob_ok = False
    else:
        out.append(f"hpc-launcher plugin:             {plugin_dir}")
        botjob = plugin_dir / "agent_helper" / "botainer-job"
        botjob_ok = botjob.exists()
        out.append(f"  {'✓' if botjob_ok else '✗'} agent_helper/botainer-job:  "
                   f"{'present' if botjob_ok else 'MISSING (stale plugin — this '
                   'copy predates job dispatch)'}")
        if not botjob_ok:
            out.append("    FIX: refresh this plugin — `botainer setup` (copy "
                       "install) or sync the source (editable install).")

    ok = (n > 0) and botjob_ok
    out.append("")
    if ok:
        out.append("VERDICT: ✓ job dispatch is wired. The agent will get a "
                   "`botainer-job` command and the jobs instructions.")
    else:
        blockers = []
        if n == 0:
            blockers.append("no top-level job_profiles")
        if not botjob_ok:
            blockers.append("botainer-job CLI unavailable")
        out.append(f"VERDICT: ✗ job dispatch is DISABLED — {', '.join(blockers)}. "
                   f"Fix the ✗ line(s) above, then re-run this.")
    return ok, out


def _find_misplaced_profiles(raw: dict[str, Any]) -> list[str]:
    """Best-effort: where did the profiles actually end up? The two common
    mistakes are nesting them under `plugins:` (a free dict that swallows them
    silently) and naming the key `profiles:` instead of `job_profiles:`."""
    hits: list[str] = []
    if isinstance(raw.get("profiles"), dict) and raw["profiles"]:
        hits.append("found a top-level `profiles:` key — it must be named "
                    "`job_profiles:` (with the job_ prefix). Rename it.")
    plugins = raw.get("plugins")
    if isinstance(plugins, dict):
        for k in ("job_profiles", "profiles"):
            if isinstance(plugins.get(k), dict) and plugins[k]:
                hits.append(f"found `{k}:` NESTED UNDER `plugins:` — job_profiles "
                            f"is NOT a plugin setting; move it out to the top "
                            f"level (column 0).")
    # a present-but-empty key: `job_profiles:` with nothing (validly) under it
    if "job_profiles" in raw and not raw.get("job_profiles"):
        hits.append("`job_profiles:` key exists but is EMPTY — the profiles under "
                    "it aren't indented as its children (each profile needs 2 "
                    "spaces of indent below the `job_profiles:` line).")
    if not hits:
        hits.append(f"not found under any obvious key. Top-level keys present: "
                    f"{sorted(raw)}. The profiles are in a different file, or "
                    f"under an unexpected key.")
    return hits


def _resolve_hpc_launcher() -> tuple[Path | None, str]:
    """(plugin_dir, reason). Uses the launcher's OWN resolver so the answer
    matches what compose sees (editable source overrides installed copy)."""
    try:
        from botainer.plugins.lifecycle import list_installed
    except Exception as exc:  # pragma: no cover - import guard
        return None, f"cannot import lifecycle: {exc}"
    hpc = next((i for i in list_installed() if i.name == "hpc-launcher"), None)
    if hpc is None:
        return None, "not in list_installed() (not installed to $MY_BOTAINER/"\
                     "plugins and not an editable source overlay)"
    return Path(hpc.plugin_dir), "resolved"
