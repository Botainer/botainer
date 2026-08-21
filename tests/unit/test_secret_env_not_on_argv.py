"""THREAT-MODEL audit (BS-1): credential-shaped env must never reach
the container runtime's COMMAND LINE.

Both adapters render `spec.env.values` as `--env K=V` / `-e K=V`. On a shared HPC
login or compute node `/proc/<pid>/cmdline` is world-readable, so a co-tenant could
read the codex broker's sentinel — which is not merely the container's
OPENAI_API_KEY but the broker daemon's ONLY TCP access token — and spend the user's
subscription for the life of the session.

The project already states this rule at plugins/browser/hooks/start_viewer.py:12
("NEVER via --env, which is visible in `ps` on a shared apptainer node"). The
viewer obeyed it; the brokers did not. It is enforced centrally so that a NEW
plugin contributing a secret gets the safe path without its author remembering.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.composition import _split_secret_env, _write_secret_env_file
from botainer.core.spec import (
    EnvSpec, MountPlan, NetworkMode, NetworkSpec, SessionSpec,
)


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        session_id="ses-x", project_uuid="u" * 32, project_root="/tmp/p",
        image="t:0.1", runtime="apptainer", state_dir=str(tmp_path),
        plugins_enabled=(), env=EnvSpec(values={}), mount_plan=MountPlan(),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
    )


@pytest.mark.parametrize("key", [
    "ANTHROPIC_AUTH_TOKEN",          # claude broker sentinel
    "OPENAI_API_KEY",                # codex broker sentinel == its TCP token
    "BOTAINER_BROKER_REQUIRED_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "SOME_VENDOR_PASSWORD",
    "MY_CREDENTIAL",
])
def test_credential_shaped_keys_are_kept_off_argv(key: str) -> None:
    safe, secret = _split_secret_env({key: "s3cret", "HOME": "/home/u"})
    assert key not in safe, f"{key} would be rendered onto the command line"
    assert key in secret


@pytest.mark.parametrize("key", [
    "ANTHROPIC_BASE_URL",            # routing, not a secret — must stay usable
    "OPENAI_BASE_URL",
    "CODEX_HOME",
    "HOME",
    "BOTAINER_AGENT_PERMISSIONS",
])
def test_non_secret_env_still_goes_the_normal_way(key: str) -> None:
    safe, secret = _split_secret_env({key: "value"})
    assert key in safe and key not in secret


def test_secret_file_is_0600_and_parses_as_an_env_file(tmp_path: Path) -> None:
    p = _write_secret_env_file(_spec(tmp_path), {"OPENAI_API_KEY": "sk-x", "A_TOKEN": "t"})
    assert p.stat().st_mode & 0o777 == 0o600, oct(p.stat().st_mode)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["A_TOKEN=t", "OPENAI_API_KEY=sk-x"]      # sorted, KEY=VALUE
    # host-private session dir, never a container bind source
    assert p.parent.name == "ses-x" and p.parent.parent.name == "sessions"


def test_newline_in_a_secret_is_refused_not_truncated(tmp_path: Path) -> None:
    """An env-file is line-delimited; a newline would silently split the value
    (or inject a second key). Refuse rather than mangle."""
    from botainer.core.refusal import Refused
    with pytest.raises(Refused):
        _write_secret_env_file(_spec(tmp_path), {"A_TOKEN": "good\nB_TOKEN=evil"})
