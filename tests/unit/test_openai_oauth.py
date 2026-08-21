"""agent-codex-broker SUBSCRIPTION mode: read botainer's OWN codex ChatGPT-OAuth
bundle host-side, refresh + rotate + write-back, inject chatgpt-account-id. The
subscription analog of test_openai_credential.py (API-key mode)."""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

from botainer.broker.openai_oauth import (
    CLIENT_ID,
    TOKEN_ENDPOINT,
    CodexOAuthCredentialBroker,
    _jwt_payload,
)
from botainer.core.broker_sentinel import make_sentinel
from botainer.core.refusal import Refused


def _jwt(claims: dict) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    pl = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{hdr}.{pl}.sig"


def _fresh_access(ttl: int = 3600) -> str:
    return _jwt({"exp": int(time.time()) + ttl})


_ID_TOKEN = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}})


def _write(path: Path, tokens: dict, *, last_refresh: str = "2026-07-01T00:00:00Z",
           mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"OPENAI_API_KEY": None, "tokens": tokens, "last_refresh": last_refresh}))
    os.chmod(path, mode)
    return path


def _tokens(**over) -> dict:
    t = {"id_token": _ID_TOKEN, "access_token": _fresh_access(),
         "refresh_token": "rt-old", "account_id": "acct-123"}
    t.update(over)
    return t


# ── JWT helper ──

def test_jwt_payload_decodes_and_rejects_garbage() -> None:
    assert _jwt_payload(_jwt({"exp": 42}))["exp"] == 42
    assert _jwt_payload("not.a.jwt") is None
    assert _jwt_payload("onlyonesegment") is None
    assert _jwt_payload(None) is None


# ── fresh token: no refresh ──

def test_fresh_access_token_used_without_refresh(tmp_path: Path) -> None:
    access = _fresh_access()
    p = _write(tmp_path / "auth.json", _tokens(access_token=access))

    def _no_call(url, body):  # must not be called
        raise AssertionError("refresh should not happen for a fresh token")

    ks = CodexOAuthCredentialBroker(p, http_post=_no_call)
    assert ks.outbound_authorization() == f"Bearer {access}"
    assert ks.outbound_headers() == {"chatgpt-account-id": "acct-123"}
    assert ks.real_secret_value() == "rt-old"


# ── expired token: refresh + rotate + write-back ──

def test_expired_token_refreshes_rotates_and_persists(tmp_path: Path) -> None:
    p = _write(tmp_path / "auth.json",
               _tokens(access_token=_jwt({"exp": 1000})))  # long-expired
    new_access = _fresh_access()
    calls = {}

    def _post(url, body):
        calls["url"], calls["body"] = url, body
        return 200, {"access_token": new_access, "refresh_token": "rt-new",
                     "id_token": _ID_TOKEN}

    ks = CodexOAuthCredentialBroker(p, http_post=_post)
    assert ks.outbound_authorization() == f"Bearer {new_access}"
    # correct endpoint + body (pinned)
    assert calls["url"] == TOKEN_ENDPOINT
    assert calls["body"] == {"client_id": CLIENT_ID, "grant_type": "refresh_token",
                             "refresh_token": "rt-old"}
    # ROTATION persisted (else next refresh trips refresh_token_reused)
    doc = json.loads(p.read_text())
    assert doc["tokens"]["refresh_token"] == "rt-new"
    assert doc["tokens"]["access_token"] == new_access
    assert doc["tokens"]["account_id"] == "acct-123"   # preserved
    assert doc["last_refresh"] != "2026-07-01T00:00:00Z"  # updated


def test_refresh_failure_fails_closed(tmp_path: Path) -> None:
    p = _write(tmp_path / "auth.json", _tokens(access_token=_jwt({"exp": 1000})))
    ks = CodexOAuthCredentialBroker(p, http_post=lambda u, b: (400, {"error": "bad"}))
    with pytest.raises(Refused):
        ks.outbound_authorization()


def test_refresh_failure_backs_off_no_hammering(tmp_path: Path) -> None:
    """After a failed refresh, a second call within the cooldown must NOT re-POST
    the OAuth endpoint (audit MEDIUM — otherwise a request flood on a
    revoked token hammers the provider)."""
    p = _write(tmp_path / "auth.json", _tokens(access_token=_jwt({"exp": 1000})))
    calls = {"n": 0}

    def _post(url, body):
        calls["n"] += 1
        return 400, {"error": "invalid_grant"}

    ks = CodexOAuthCredentialBroker(p, http_post=_post)
    with pytest.raises(Refused):
        ks.outbound_authorization()
    with pytest.raises(Refused):
        ks.outbound_authorization()   # within cooldown → cached refusal
    assert calls["n"] == 1            # endpoint hit exactly once, not twice


# ── account_id derivation ──

def test_account_id_falls_back_to_id_token_claim(tmp_path: Path) -> None:
    # no account_id field → derive from the id_token JWT claim
    p = _write(tmp_path / "auth.json",
               _tokens(access_token=_fresh_access(), account_id=None))
    ks = CodexOAuthCredentialBroker(p)
    assert ks.outbound_headers() == {"chatgpt-account-id": "acct-123"}


def test_missing_account_id_refused(tmp_path: Path) -> None:
    bare_id = _jwt({"sub": "x"})  # no chatgpt_account_id claim
    p = _write(tmp_path / "auth.json",
               _tokens(access_token=_fresh_access(), account_id=None, id_token=bare_id))
    with pytest.raises(Refused):
        CodexOAuthCredentialBroker(p).outbound_headers()


# ── fallback expiry (unreadable exp → last_refresh + 8 days) ──

def test_unreadable_exp_uses_last_refresh_fallback(tmp_path: Path) -> None:
    opaque = "opaque-not-a-jwt"  # no decodable exp
    # last_refresh well within 8 days → treated as fresh, no refresh
    recent = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600))
    p = _write(tmp_path / "auth.json",
               _tokens(access_token=opaque), last_refresh=recent)

    def _no_call(url, body):
        raise AssertionError("should not refresh within the 8-day fallback window")

    ks = CodexOAuthCredentialBroker(p, http_post=_no_call)
    assert ks.outbound_authorization() == f"Bearer {opaque}"


# ── fail-closed validation ──

def test_no_tokens_object_refused(tmp_path: Path) -> None:
    p = tmp_path / "api_key"
    p.write_text("sk-plain-api-key")  # API-key store, not a ChatGPT login
    os.chmod(p, 0o600)
    with pytest.raises(Refused):
        CodexOAuthCredentialBroker(p).outbound_authorization()


def test_sentinel_refresh_token_refused(tmp_path: Path) -> None:
    p = _write(tmp_path / "auth.json",
               _tokens(refresh_token=make_sentinel("t", "deadbeef")))
    with pytest.raises(Refused):
        CodexOAuthCredentialBroker(p).outbound_authorization()


def test_world_readable_refused(tmp_path: Path) -> None:
    p = _write(tmp_path / "auth.json", _tokens(), mode=0o644)
    with pytest.raises(Refused):
        CodexOAuthCredentialBroker(p).outbound_authorization()


def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused):
        CodexOAuthCredentialBroker(tmp_path / "nope").outbound_authorization()
