"""agent-codex-broker credential source: read botainer's OWN codex key host-side,
inject `Authorization: Bearer sk-…`, fail CLOSED otherwise. The OpenAI analog of
test_broker_module.py's BotainerCredentialBroker coverage."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from botainer.broker.openai_credential import (
    OpenAICredentialBroker,
    resolve_codex_credential_path,
)
from botainer.core.broker_sentinel import make_sentinel
from botainer.core.refusal import Refused

FAKE_KEY = "sk-" + "a" * 48
FAKE_KEY_2 = "sk-" + "b" * 48


def _write(path: Path, content: str, *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)
    return path


# ── path resolution mirrors the codex pre_session binds ──

def test_shared_path_matches_agent_codex_shared_hook(tmp_path: Path) -> None:
    p = resolve_codex_credential_path(state_root=tmp_path, mode="shared")
    assert p == tmp_path / "shared-auth" / "agent-codex" / "auth.json"


def test_isolated_path_matches_agent_codex_hook(tmp_path: Path) -> None:
    p = resolve_codex_credential_path(
        state_root=tmp_path, mode="isolated", project_uuid="u", profile="default")
    assert p == (tmp_path / "state" / "u" / "data" / "agent-codex"
                 / "profiles" / "default" / "api_key")


def test_isolated_requires_uuid(tmp_path: Path) -> None:
    with pytest.raises(Refused):
        resolve_codex_credential_path(state_root=tmp_path, mode="isolated")


def test_unknown_mode_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused):
        resolve_codex_credential_path(state_root=tmp_path, mode="mounted")


# ── key extraction (auth.json OPENAI_API_KEY, or a plain api_key file) ──

def test_bearer_from_auth_json(tmp_path: Path) -> None:
    p = _write(tmp_path / "auth.json", json.dumps({"OPENAI_API_KEY": FAKE_KEY}))
    ks = OpenAICredentialBroker(p)
    assert ks.outbound_authorization() == f"Bearer {FAKE_KEY}"
    assert ks.real_secret_value() == FAKE_KEY


def test_bearer_from_plain_api_key_file(tmp_path: Path) -> None:
    p = _write(tmp_path / "api_key", FAKE_KEY + "\n")  # trailing newline tolerated
    ks = OpenAICredentialBroker(p)
    assert ks.outbound_authorization() == f"Bearer {FAKE_KEY}"


def test_reread_picks_up_rotated_key(tmp_path: Path) -> None:
    p = _write(tmp_path / "api_key", FAKE_KEY)
    ks = OpenAICredentialBroker(p)
    assert ks.outbound_authorization() == f"Bearer {FAKE_KEY}"
    _write(tmp_path / "api_key", FAKE_KEY_2)
    assert ks.outbound_authorization() == f"Bearer {FAKE_KEY_2}"


# ── fail-closed ──

def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused):
        OpenAICredentialBroker(tmp_path / "nope").outbound_authorization()


def test_non_sk_key_refused(tmp_path: Path) -> None:
    p = _write(tmp_path / "api_key", "not-a-real-key")
    with pytest.raises(Refused):
        OpenAICredentialBroker(p).outbound_authorization()


def test_sentinel_in_store_refused(tmp_path: Path) -> None:
    """If a broker sentinel somehow leaked back into the store, never forward it
    (it carries no secret and would 401 upstream anyway)."""
    p = _write(tmp_path / "api_key", make_sentinel("t", "deadbeef"))
    with pytest.raises(Refused):
        OpenAICredentialBroker(p).outbound_authorization()


def test_world_readable_refused(tmp_path: Path) -> None:
    p = _write(tmp_path / "api_key", FAKE_KEY, mode=0o644)
    with pytest.raises(Refused):
        OpenAICredentialBroker(p).outbound_authorization()


def test_empty_auth_json_refused(tmp_path: Path) -> None:
    p = _write(tmp_path / "auth.json", json.dumps({"tokens": {"access_token": "x"}}))
    with pytest.raises(Refused):  # OAuth-only shape has no OPENAI_API_KEY (API-key mode)
        OpenAICredentialBroker(p).outbound_authorization()
