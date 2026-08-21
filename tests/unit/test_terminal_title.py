"""Tests for terminal title rendering + emission."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from botainer.core.spec import SessionSpec
from botainer.inspect import terminal_title


def _make_spec(**kw) -> SessionSpec:
    base = dict(
        session_id="abcdef0123456789",
        project_uuid="u",
        project_root="/home/user/proj-foo",
        state_dir="/s",
        runtime="docker",
        image="img",
    )
    base.update(kw)
    return SessionSpec(**base)


def test_render_title_basic() -> None:
    spec = _make_spec()
    title = terminal_title.render_title(spec)
    assert title == "botainer: proj-foo [session abcdef01]"


def test_render_title_handles_unicode_project_name() -> None:
    spec = _make_spec(project_root="/home/user/проект")
    title = terminal_title.render_title(spec)
    assert "проект" in title
    assert "abcdef01" in title


def test_render_title_strips_control_chars() -> None:
    """Defense: a project name with an embedded ESC/BEL should not poison
    the title."""
    spec = _make_spec(project_root="/home/user/foo\x07bar\x1bbaz")
    title = terminal_title.render_title(spec)
    assert "\x07" not in title
    assert "\x1b" not in title
    assert "foobarbaz" in title


def test_render_title_empty_project_name() -> None:
    """If project_root is '/', use '(unnamed)' rather than empty."""
    spec = _make_spec(project_root="/")
    title = terminal_title.render_title(spec)
    assert "(unnamed)" in title


def test_emit_title_no_op_when_not_tty() -> None:
    """When stream isn't a TTY, emit nothing (safe for log files)."""
    spec = _make_spec()
    stream = io.StringIO()
    # io.StringIO().isatty() returns False by default.
    terminal_title.emit_title(spec, stream=stream)
    assert stream.getvalue() == ""


def test_emit_title_no_op_when_dumb_term(monkeypatch) -> None:
    """TERM=dumb → no emission even on a TTY."""
    spec = _make_spec()
    stream = MagicMock()
    stream.isatty.return_value = True
    monkeypatch.setenv("TERM", "dumb")
    terminal_title.emit_title(spec, stream=stream)
    stream.write.assert_not_called()


def test_emit_title_no_op_when_empty_term(monkeypatch) -> None:
    """TERM unset → no emission."""
    spec = _make_spec()
    stream = MagicMock()
    stream.isatty.return_value = True
    monkeypatch.delenv("TERM", raising=False)
    terminal_title.emit_title(spec, stream=stream)
    stream.write.assert_not_called()


def test_emit_title_emits_on_capable_tty(monkeypatch) -> None:
    """TTY + capable TERM → emit OSC 0 sequence."""
    spec = _make_spec()
    stream = MagicMock()
    stream.isatty.return_value = True
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal_title.emit_title(spec, stream=stream)
    written = stream.write.call_args[0][0]
    assert written.startswith("\x1b]0;")
    assert written.endswith("\x07")
    assert "proj-foo" in written
    assert "abcdef01" in written
    stream.flush.assert_called_once()


def test_is_terminal_capable_off_for_pipe() -> None:
    """A pipe (no isatty) is never capable."""
    stream = io.BytesIO()
    assert not terminal_title.is_terminal_capable(stream)
