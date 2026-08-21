"""Tests for plugin manifest parsing + reserved name checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.refusal import RefusalCategory, Refused
from botainer.plugins.manifest import (
    MANIFEST_FILENAME,
    check_reserved_name,
    load_manifest,
)


def test_manifest_round_trip(tmp_path: Path) -> None:
    pdir = tmp_path / "plug"
    pdir.mkdir()
    (pdir / MANIFEST_FILENAME).write_text(
        "apiVersion: botainer-plugin-v1\n"
        "name: simple-plugin\n"
        "version: 0.1.0\n"
        "tier: third-party\n"
        "trust_required: declarative\n"
        "hooks: []\n"
    )
    m = load_manifest(pdir)
    assert m.name == "simple-plugin"
    assert m.tier == "third-party"


def test_manifest_with_invalid_name_refused(tmp_path: Path) -> None:
    pdir = tmp_path / "plug"
    pdir.mkdir()
    (pdir / MANIFEST_FILENAME).write_text(
        "apiVersion: botainer-plugin-v1\n"
        "name: Bad_Name\n"  # uppercase + underscore not allowed
        "version: 0.1.0\n"
    )
    with pytest.raises(Refused) as exc:
        load_manifest(pdir)
    assert exc.value.category == RefusalCategory.PLUGIN_MANIFEST_INVALID


def _write_manifest(pdir: Path, body: str) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / MANIFEST_FILENAME).write_text(
        "apiVersion: botainer-plugin-v1\n"
        "name: probe-plugin\n"
        "version: 0.1.0\n"
        "tier: third-party\n"
        "trust_required: declarative\n"
        + body
    )


# AC7 validator-parity audit: the manifest field validators
# (_validate_when, _validate_relative_script_path, _validate_script,
# _validate_command) existed but had no negative-regression tests. These
# pin that load_manifest REFUSES each hostile shape — so a future refactor
# of those validators can't silently drop the absolute/traversal/control-
# char/enum checks. (No exploitable bypass was found; this is the missing
# coverage, exercised through the real load_manifest → Refused path.)
@pytest.mark.parametrize(
    "when",
    ["pre_request", "post_request", "x; rm -rf /", "PRE_SESSION", ""],
)
def test_manifest_refuses_invalid_hook_when(tmp_path: Path, when: str) -> None:
    _write_manifest(
        tmp_path / "p",
        f"hooks:\n  - when: {when!r}\n    script: hooks/h.py\n",
    )
    with pytest.raises(Refused) as exc:
        load_manifest(tmp_path / "p")
    assert exc.value.category == RefusalCategory.PLUGIN_MANIFEST_INVALID


@pytest.mark.parametrize(
    "script",
    [
        "/etc/passwd",          # absolute
        "../../escape.sh",      # leading traversal
        "a/../../b.py",         # mid-path traversal
        "",                     # empty
    ],
)
def test_manifest_refuses_hostile_hook_script(tmp_path: Path, script: str) -> None:
    _write_manifest(
        tmp_path / "p",
        f"hooks:\n  - when: pre_session\n    script: {script!r}\n",
    )
    with pytest.raises(Refused) as exc:
        load_manifest(tmp_path / "p")
    assert exc.value.category == RefusalCategory.PLUGIN_MANIFEST_INVALID


@pytest.mark.parametrize("script", ["x\x00y.py", "x\ny.py"])
def test_hook_script_validator_rejects_control_chars(script: str) -> None:
    """NUL / newline in a script path — checked at the model layer (pydantic)
    since YAML single-quoting can't carry real control bytes.
    _validate_relative_script_path must reject them. (CR/tab are not
    currently rejected; tightening that lives in manifest.py, a
    security-surface file, so it is deferred to a reviewed change.)"""
    import pydantic
    from botainer.plugins.manifest import HookDecl

    with pytest.raises((pydantic.ValidationError, ValueError)):
        HookDecl(when="pre_session", script=script)


@pytest.mark.parametrize("script", ["/etc/x.py", "../../x.py", "a/../../b.py"])
def test_manifest_refuses_hostile_command_script(
    tmp_path: Path, script: str
) -> None:
    _write_manifest(
        tmp_path / "p",
        f"commands:\n  - name: run\n    script: {script!r}\n",
    )
    with pytest.raises(Refused) as exc:
        load_manifest(tmp_path / "p")
    assert exc.value.category == RefusalCategory.PLUGIN_MANIFEST_INVALID


def test_reserved_first_party_name_refused_for_third_party() -> None:
    with pytest.raises(Refused) as exc:
        check_reserved_name("agent-claude", source_is_first_party=False)
    assert exc.value.category == RefusalCategory.PLUGIN_NAME_RESERVED


def test_agent_prefix_refused_for_third_party() -> None:
    with pytest.raises(Refused) as exc:
        check_reserved_name("agent-newfoo", source_is_first_party=False)
    assert exc.value.category == RefusalCategory.PLUGIN_NAME_RESERVED


def test_arbitrary_third_party_name_accepted() -> None:
    check_reserved_name("my-cool-plugin", source_is_first_party=False)


def test_first_party_source_can_use_reserved_name() -> None:
    check_reserved_name("agent-claude", source_is_first_party=True)


def test_first_party_plugins_load_from_repo() -> None:
    repo_plugins = Path(__file__).resolve().parents[2] / "plugins"
    for name in (
        "agent-claude", "agent-claude-proxy", "git",
        "hpc-launcher", "hpc-modules", "nudge", "web-ports",
    ):
        m = load_manifest(repo_plugins / name)
        assert m.name == name
        assert m.tier == "first-party"
