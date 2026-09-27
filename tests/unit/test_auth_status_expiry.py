"""`auth status` must report token HEALTH, not merely that a file exists.

A credential file can exist while its token is expired. Reporting success
from `path.exists()` alone hides that distinction; status needs to inspect
expiry when the credential format supports it.

Why it went unnoticed: only the BROKER path ever parsed `expiresAt`
(botainer/broker/credential_source.py). In shared/isolated MOUNT modes the file
is bound rw into the container and Claude Code refreshes it in place — nothing
on the HOST reads the expiry, so an idle store goes stale silently.

`_credential_expiry` is deliberately best-effort: an unrecognised shape reports
"unknown", never a crash and never a false "valid". These tests pin that
fail-toward-refusal behaviour, since a wrong "valid" is what made the original
report useless.
"""
from __future__ import annotations

import json
import time

from botainer.cli.auth import _credential_expiry


def _write(tmp_path, name: str, doc) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def test_expired_token_is_reported_expired(tmp_path) -> None:
    path = _write(tmp_path, "c.json",
                  {"claudeAiOauth": {"expiresAt": (time.time() - 7200) * 1000}})
    state, detail = _credential_expiry(path)
    assert state == "expired", (state, detail)
    assert "ago" in detail


def test_live_token_is_reported_valid_with_remaining(tmp_path) -> None:
    path = _write(tmp_path, "c.json",
                  {"claudeAiOauth": {"expiresAt": (time.time() + 9000) * 1000}})
    state, detail = _credential_expiry(path)
    assert state == "valid", (state, detail)
    assert "valid for" in detail


def test_missing_file_is_absent_not_valid(tmp_path) -> None:
    assert _credential_expiry(tmp_path / "nope.json")[0] == "absent"


def test_unrecognised_shapes_report_unknown_never_valid(tmp_path) -> None:
    """The important direction: never claim health we did not verify."""
    api_key = _write(tmp_path, "k.json", {"api_key": "sk-not-a-real-key"})
    no_exp = _write(tmp_path, "n.json", {"claudeAiOauth": {}})
    bad = tmp_path / "b.json"
    bad.write_text("not json at all", encoding="utf-8")

    for path in (api_key, no_exp, str(bad)):
        state, detail = _credential_expiry(path)
        assert state == "unknown", (path, state)
        assert state != "valid"
        assert detail, "an 'unknown' must say WHY it could not be checked"
