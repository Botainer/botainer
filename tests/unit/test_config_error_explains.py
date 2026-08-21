"""A schema error must tell you what to DO, not just that you were wrong.

User report: they wrote `job-profiles:` instead of `job_profiles:`
and could not work out what was wrong. pydantic's `extra="forbid"` KNEW the key
was unknown and held the complete list of valid names, and surfaced neither —
just "Extra inputs are not permitted".

The failure is that the error had every fact needed to solve itself and
volunteered none of them. These tests pin the three things it must now
volunteer: the near-miss, the nesting footgun, and where to read more.
"""
from __future__ import annotations

import uuid

import pytest

from botainer.core.config import load_config
from botainer.core.refusal import Refused


def _project(tmp_path, body: str):
    (tmp_path / ".botainer").mkdir()
    (tmp_path / ".botainer" / "project-id").write_text(str(uuid.uuid4()))
    (tmp_path / ".botainer" / "config.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def _refusal(tmp_path, body: str) -> str:
    with pytest.raises(Refused) as exc:
        load_config(_project(tmp_path, body))
    return str(exc.value)


def test_hyphen_for_underscore_typo_names_the_right_key(tmp_path) -> None:
    """THE reported case. A hyphen/underscore swap scores poorly on difflib for
    short keys, so it is checked explicitly — this must not regress to a
    generic 'unknown key'."""
    msg = _refusal(tmp_path, "version: config-v1\nagent: claude\n"
                             "job-profiles:\n  cpu:\n    partition: day\n")
    assert "did you mean `job_profiles`" in msg, msg


def test_unknown_key_lists_the_valid_ones(tmp_path) -> None:
    """Even with no near miss, the user must not have to go hunting."""
    msg = _refusal(tmp_path, "version: config-v1\nagent: claude\nfrobnicate: yes\n")
    assert "is not a config key" in msg
    assert "Valid top-level keys:" in msg
    assert "job_profiles" in msg and "plugins_enabled" in msg


def test_top_level_key_nested_under_plugins_is_called_out(tmp_path) -> None:
    """The silent footgun: right key, wrong indentation, quietly ignored.
    A different mistake from a typo, needing a different fix, so it gets its
    own sentence rather than a generic suggestion."""
    msg = _refusal(tmp_path, "version: config-v1\nagent: claude\n"
                             "plugins:\n  job_profiles:\n    cpu: {partition: day}\n")
    assert "appear under `plugins:`" in msg
    assert "TOP-LEVEL" in msg
    assert "Un-indent" in msg


@pytest.mark.parametrize("body", [
    "version: config-v1\nagent: claude\njob-profiles: {}\n",            # unknown key
    "version: config-v1\nagent: claude\nresources:\n  cpu: \"lots\"\n",  # wrong type
])
def test_every_schema_error_points_somewhere(tmp_path, body: str) -> None:
    """Including errors with no key to suggest — a type error still needs a
    route to the schema and to a working example."""
    msg = _refusal(tmp_path, body)
    assert "botainer schema config" in msg, msg
    assert "examples/" in msg, msg
