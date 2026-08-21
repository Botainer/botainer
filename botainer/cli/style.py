"""Uniform color + warning-level conventions for user-facing output.

Four levels, semantic not just aesthetic:

  alert  — security-relevant; the user MUST look (bold red, leading ⚠)
  warn   — potential issue; non-blocking (bold yellow, leading ⚠)
  info   — section header / status (bold cyan)
  ok     — success confirmation (green, leading ✓)

Plus `body(text)` for indented detail lines under any of the above.

Why this exists:
- v0.1.x had ad-hoc `click.secho(..., fg=...)` calls in dozens of
  places, each picking color independently. Result: trust warnings
  (security) were the same yellow as "no recorded image" (config
  issue), and users couldn't tell what required action from what was
  noise. See feedback_visible_user_instructions memory note.
- A single module here means: pick a level by SEMANTICS, not color.
  If we ever change the palette (accessibility, branding), we change
  it in one place.

All helpers default to stderr so they don't pollute JSON/script
stdout. They honor `NO_COLOR` (per the de-facto standard).

Usage:
    from botainer.cli import style as st
    st.alert("PLUGIN TRUST WARNING — modified plugin enabled")
    st.body("Run `botainer plugin verify <name>` to inspect.")
    st.warn("Auth mode: SHARED (host-wide credentials)")
    st.info("Plugins enabled: agent-claude-shared, git")
    st.ok("Image built; recorded in installed.lock")

Don't add new ad-hoc `secho` colors for user-facing text. Pick a
level.
"""

from __future__ import annotations

import os
import sys

import click


def _color_on() -> bool:
    """True iff we should emit ANSI codes. Defaults to stderr TTY.

    `NO_COLOR` env var (any non-empty value) disables color, per
    https://no-color.org/.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


def alert(text: str, *, err: bool = True) -> None:
    """Security-relevant / must-look. Bold red, ⚠ prefix."""
    click.secho(f"⚠ {text}", fg="red", bold=True, err=err, color=_color_on())


def warn(text: str, *, err: bool = True) -> None:
    """Potential issue, non-blocking. Bold yellow, ⚠ prefix."""
    click.secho(f"⚠ {text}", fg="yellow", bold=True, err=err, color=_color_on())


def info(text: str, *, err: bool = True) -> None:
    """Section header or status line. Bold cyan."""
    click.secho(text, fg="cyan", bold=True, err=err, color=_color_on())


def ok(text: str, *, err: bool = True) -> None:
    """Success confirmation. Green, ✓ prefix."""
    click.secho(f"✓ {text}", fg="green", err=err, color=_color_on())


def body(text: str, *, err: bool = True) -> None:
    """Indented detail under an alert/warn/info header. Plain cyan."""
    # The 4-space indent is intentional and consistent across the
    # codebase. Don't add your own indent.
    for line in text.splitlines() or [""]:
        click.secho(f"    {line}", fg="cyan", err=err, color=_color_on())
