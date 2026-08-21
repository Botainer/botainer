"""User-facing TIPS — short one-liners, one shown after each interactive command.

Distinct from a plugin's `contributes.agent_hints_section` (that's for the AGENT,
injected into the container). Tips are for the HUMAN at the CLI: usage nudges,
discoverability, and — deliberately — an *ambient* tier of security-surface
reminders that complements the loud point-of-use warnings (e.g. the browser
viewer's). See `botainer tips` for the whole list.

Design:
- CORE owns the display, the base list, and the rotation. It is NOT a plugin: tips
  are a cross-cutting UX concern, always-on, and must work with zero plugins
  enabled — an "on-by-default plugin" would be core with extra indirection.
- PLUGINS contribute their own tips via `contributes.user_tips`. These lead with
  the CAPABILITY the plugin offers and how to activate it (feature discovery), and
  point to docs for the security surface — so tips from INSTALLED-but-not-enabled
  plugins are useful too ("here's what you could turn on"). The pool therefore
  draws on every INSTALLED plugin, not only enabled ones.
- One tip is shown as a dim footer on stderr after a successful command, ONLY on a
  TTY (never pollutes pipes/JSON), suppressible with BOTAINER_NO_TIPS=1.
- Rotation is SEQUENTIAL via a persisted index so the user cycles through all tips
  over successive runs (better coverage than random, which repeats and skips).

RULE — tips point, they never adjudicate (user-corrected twice). A tip
may state a FACT ("the caged agent acts autonomously", "files the agent writes are
untrusted") and POINT to the security surface ("see docs/BROWSER.md"). A tip must
NOT render a comparative or absolute SAFETY VERDICT ("safe", "safer", "protects",
"secure") — a one-liner can't carry the caveats a security claim needs, so a
verdict here misleads. The nuance belongs in docs + the point-of-use warning, which
have room to be complete.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Tip:
    text: str
    source: str = "botainer"  # "botainer" (core) or a plugin name


# Curated core tips. Keep each to one or two lines. The mix is deliberate: usage +
# discoverability + an ambient security-awareness tier (files-are-untrusted,
# autonomous-agent, egress) that keeps the trust model in the user's mind between
# the loud point-of-use warnings.
BASE_TIPS: tuple[str, ...] = (
    "See all tips with `botainer tips`. Silence them with BOTAINER_NO_TIPS=1.",
    "`botainer doctor` checks your host (Docker/Apptainer, disk, config) and tells "
    "you how to fix what's wrong — run it first when something misbehaves.",
    "`botainer inspect` previews a session before you start it. It shows the compose-time plan; binds that plugins add at start time — including credentials — need `botainer dry-run --include-hooks`.",
    "Files the agent writes land in your project directory (bound into the "
    "container). Treat them as UNTRUSTED output: opening or running one on your "
    "host is a trust boundary, same as a file off the internet.",
    "By default the caged agent runs with permission prompts OFF — it acts on its "
    "own. That's the point of the container; keep secrets out of the bound project dir.",
    "`network.mode: internet` gives the agent the whole web (and lets a malicious "
    "page it visits reach out). Use `network.mode: none` for offline work.",
    "Both /packages and the container's home (/home/user) persist across sessions "
    "(per-project dirs in your state) — so an agent-planted ~/.bashrc or ~/.npmrc "
    "survives into later sessions. /scratch is scratch.",
    "`botainer status` and `botainer list` show running sessions; `botainer stop` ends one.",
    "`botainer inspect` and `botainer access` show what a session can reach: "
    "config capabilities plus the default binds (/packages, /scratch, /home/user) "
    "and any policy ceilings. For the COMPLETE list, including the credential "
    "mounts plugins add at start time, use `botainer dry-run --include-hooks`.",
    "On HPC, `botainer hpc jobs-doctor` explains why a dispatched job didn't run.",
    "Rebuilt a plugin or changed its manifest? Re-run `botainer setup` so the "
    "installed copy matches the source.",
)


def _base_tips() -> list[Tip]:
    return [Tip(t, "botainer") for t in BASE_TIPS]


def _plugin_tips(only: set[str] | None) -> list[Tip]:
    """Tips from installed plugins' manifests (`contributes.user_tips`). If `only`
    is a set of plugin names, restrict to those; if None (both the footer AND the
    `botainer tips` catalogue today), every INSTALLED plugin contributes — plugin
    tips advertise available capabilities, so not-yet-enabled ones are wanted.
    Best-effort — never raises, so tips can't break the CLI or slow the common
    path when nothing is installed."""
    out: list[Tip] = []
    try:
        from botainer.plugins.lifecycle import list_installed
        from botainer.plugins.manifest import load_manifest
    except Exception:
        return out
    try:
        installed = list_installed()
    except Exception:
        return out
    for p in installed:
        if only is not None and p.name not in only:
            continue
        try:
            manifest = load_manifest(p.plugin_dir)
            tips = list(getattr(manifest.contributes, "user_tips", []) or [])
        except Exception:
            continue
        out.extend(Tip(t, p.name) for t in tips)
    return out


def collect_tips(enabled: set[str] | None = None) -> list[Tip]:
    """Base tips + plugin tips. `enabled` restricts plugin tips to that name set;
    None (both call sites today) → every installed plugin. The param is kept for a
    future "enabled-only" mode but is currently always None."""
    return _base_tips() + _plugin_tips(enabled)


# ─────────────────────────── sequential rotation ───────────────────────────
def _index_file() -> "os.PathLike[str] | None":
    try:
        from botainer.state import dir as state_dir
        paths = state_dir.ensure_user_state_dir(create_if_missing=True)
        return paths.root / "tips-index"
    except Exception:
        return None


def _next_index(n: int) -> int:
    """Read the persisted rotation index, advance + persist it, return the value to
    use now. Best-effort: if the state dir isn't writable, fall back to 0 (still
    shows a tip; just doesn't rotate)."""
    if n <= 0:
        return 0
    path = _index_file()
    idx = 0
    if path is not None:
        try:
            idx = int(open(path, encoding="utf-8").read().strip())  # noqa: SIM115
        except (OSError, ValueError):
            idx = 0
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str((idx + 1) % n))
        except OSError:
            pass
    return idx % n


def select_tip(tips: list[Tip]) -> Tip | None:
    if not tips:
        return None
    return tips[_next_index(len(tips))]


# ─────────────────────────────── rendering ───────────────────────────────
def render_footer(tip: Tip, color: bool) -> str:
    """A single dim footer line. Plugin-sourced tips are tagged with the plugin."""
    dim = "\x1b[2m" if color else ""
    off = "\x1b[0m" if color else ""
    tag = "" if tip.source == "botainer" else f" [{tip.source}]"
    return f"{dim}\N{ELECTRIC LIGHT BULB} tip:{tag} {tip.text}  ·  all: botainer tips{off}"


def tips_suppressed() -> bool:
    # Truthy values suppress; "0"/"false"/"no"/"off"/"" do NOT (so a user who sets
    # BOTAINER_NO_TIPS=0 to re-enable isn't silently kept suppressed — Fable-5 L5).
    val = os.environ.get("BOTAINER_NO_TIPS", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def print_tip_footer(enabled: set[str] | None) -> None:
    """Print one tip as a dim stderr footer — ONLY on a TTY and when not suppressed.
    Never raises (callers wrap too, but defense in depth)."""
    try:
        if tips_suppressed() or not sys.stderr.isatty():
            return
        tip = select_tip(collect_tips(enabled))
        if tip is None:
            return
        sys.stderr.write(render_footer(tip, color=True) + "\n")
    except Exception:
        pass


# ── the RULE above, as a check ──
#
# A FILTER, and it must be read as one. The RULE at the top of this module came
# from the maintainer catching two shipped safety verdicts by hand,
# and until now it was enforced by nothing at all: it lived in a docstring here
# and in a comment above ManifestModel._validate_user_tips, while that validator
# checked non-empty, charset and length only. A tip reading "The viewer is SAFER
# and PROTECTS your clipboard" was accepted unmodified.
#
# WHAT THIS DOES NOT DO, stated plainly because a filter documented as a
# guarantee is the same class of error as a false safety verdict. The project
# rule is: prefer making a bad state IMPOSSIBLE over detecting it, and when you
# ship a detector anyway, name the structural gap it backs up. So:
#
#   The gap: a tip is a `str`, so a verdict is exactly as REPRESENTABLE as a
#   fact. No word list closes that. "redundant", "unnecessary", "you don't need
#   to worry about", "handled for you" are all verdicts this misses.
#
#   The structural fix is to make a verdict UNREPRESENTABLE: a tip becomes a
#   typed record with a REQUIRED pointer — Tip(fact=…, pointer=…) rendered as
#   "<fact>  See: <pointer>" — where the pointer resolves against the live click
#   tree or a doc path present IN THE WHEEL. Then "state a fact and POINT" is
#   the only shape the type admits and a comparative judgement has nowhere to
#   sit. Same move as run_hook(agent_writable_roots=…) being a required
#   parameter so omitting it is a TypeError. Tracked; not done.
#
# Until then this catches the literal words that caused all four known
# incidents, on BOTH surfaces — core tips AND every installed plugin's
# user_tips. The gate that existed walked BASE_TIPS only, while both of the
# maintainer's catches were in plugins/browser/botainer-plugin.yaml. A check
# that covers only the half where the incident did not happen is scenery.

_VERDICT_WORDS = (
    "safe", "safer", "safest", "secure", "securely", "protects", "protected",
    "protecting", "hardened", "sandboxed", "isolated from", "can't escape",
    "cannot escape", "no risk", "risk-free", "guarantees", "guaranteed",
)


def verdict_words_in(text: str) -> list[str]:
    """Return the safety-verdict words a tip renders, or []. See the RULE.

    Word-boundary matched, so "safety", "isolated mode" and "unsafe" do not
    trip it — the rule forbids a VERDICT, not the vocabulary of the domain.
    """
    import re
    low = text.lower()
    hits = []
    for w in _VERDICT_WORDS:
        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low):
            hits.append(w)
    return hits
