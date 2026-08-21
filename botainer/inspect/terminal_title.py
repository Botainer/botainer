"""Set the user's terminal window title for the duration of the session.

Emits the OSC 0 escape sequence: `ESC ] 0 ; <title> BEL`. Recognized by
iTerm2, Apple Terminal, GNOME Terminal, Alacritty, kitty, xterm, mintty,
Windows Terminal, and tmux's pass-through (when allow-passthrough is on).

We emit this from the launcher right before `docker run`/`apptainer exec`
takes over the terminal. The agent CLI (or tmux, if nudge is enabled)
may overwrite it later — that's fine. The initial set-and-leave gives
users a way to distinguish multiple botainer sessions visually.

Title format:
    botainer: <project-name> [session <short-sid>]

Doesn't run in:
- --json / --quiet modes (suppressed via callsite)
- when stdout/stderr aren't a TTY (avoids emitting to log files)
- when TERM is `dumb` (no support for OSC sequences)
"""

from __future__ import annotations

import os
import sys
from typing import IO

from botainer.core.spec import SessionSpec

_OSC_PREFIX = "\033]0;"
_BEL = "\007"


def is_terminal_capable(stream: IO[str] = sys.stderr) -> bool:
    """Heuristic for whether to emit OSC sequences."""
    if not stream.isatty():
        return False
    term = os.environ.get("TERM", "")
    return bool(term) and term != "dumb"


def render_title(spec: SessionSpec) -> str:
    """Compose the title string from spec.

    Avoids shell-special chars (BEL, control codes, embedded newlines).
    The session_id is a hex prefix so it's already safe; project_root
    basename comes from the filesystem which can theoretically contain
    control characters — we sanitize.
    """
    from pathlib import Path

    project_name = Path(spec.project_root).name or "(unnamed)"
    project_name = "".join(c for c in project_name if c.isprintable() and c not in "\033\007")
    short_sid = spec.session_id[:8]
    return f"botainer: {project_name} [session {short_sid}]"


def emit_title(spec: SessionSpec, *, stream: IO[str] = sys.stderr) -> None:
    """Emit the OSC 0 sequence on the given stream.

    No-op when the stream isn't a capable terminal. Safe to call
    unconditionally; will not corrupt non-TTY output.
    """
    if not is_terminal_capable(stream):
        return
    title = render_title(spec)
    # Sanity: title must not contain BEL or ESC (both close the sequence).
    if _BEL in title or "\033" in title:
        return  # defense-in-depth; render_title already strips these
    stream.write(f"{_OSC_PREFIX}{title}{_BEL}")
    stream.flush()
