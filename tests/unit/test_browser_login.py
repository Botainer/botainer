"""`botainer plugin browser login` — the pure instruction renderer for credential
handoff. Imports the plugin command module directly."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_LOGIN = Path(__file__).resolve().parents[2] / "plugins/browser/commands/login.py"
_spec = importlib.util.spec_from_file_location("browser_login", _LOGIN)
login = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(login)  # type: ignore[union-attr]


def test_render_includes_capture_command_and_config_key():
    out = login.render("https://example.com")
    assert "playwright codegen --save-storage=.botainer/browser-auth.json https://example.com" in out
    assert "storage_state: .botainer/browser-auth.json" in out
    assert "SECRET" in out and ".gitignore" in out          # secret-hygiene warning
    assert "keyloggable" in out or "PASSWORD off" in out     # the why


def test_render_without_url_uses_placeholder():
    out = login.render(None)
    assert "<the-site-url>" in out


def test_main_prints_and_returns_zero(capsys):
    assert login.main(["https://site.test"]) == 0
    assert "https://site.test" in capsys.readouterr().out
