"""Unit tests for agent-codex-shared's OAuth back-fill gate (P3a + Codex
Priority-A MEDIUM).

`_oauth_identity` gates whether a per-project `auth.json` may overwrite the
host-wide SHARED credential. The tightening: API-key shape is NOT back-fillable
at all, and OAuth requires the full {access_token, refresh_token, account_id}
shape (the caller additionally requires account_id to MATCH the shared cred — a
refresh, not a swap). The plugin hook is a stand-alone script; import by path."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "plugins" / "agent-codex-shared" / "hooks" / "pre_session.py"

_LONG = "x" * 200  # a plausible token length


def _load():
    spec = importlib.util.spec_from_file_location("agent_codex_shared_pre", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_accepts_full_oauth_bundle() -> None:
    m = _load()
    ident = m._oauth_identity(
        {"tokens": {"access_token": _LONG, "refresh_token": "r" * 40,
                    "account_id": "acc-123"},
         "last_refresh": "2026-07-02T00:00:00Z"}
    )
    assert ident == (_LONG, "r" * 40, "acc-123")


def test_api_key_shape_is_not_backfillable() -> None:
    """Codex MEDIUM #1: API keys are never back-fillable (not refreshed
    in-container), so a forged `OPENAI_API_KEY` can't poison the shared cred."""
    m = _load()
    assert m._oauth_identity({"OPENAI_API_KEY": "sk-" + "a" * 60}) is None


@pytest.mark.parametrize("bad", [
    None, {}, [], "not a dict",
    {"tokens": {}},                                   # no fields
    {"tokens": {"access_token": _LONG}},              # missing refresh + account
    {"tokens": {"access_token": _LONG, "refresh_token": "r" * 40}},  # missing account_id
    {"tokens": {"access_token": "tiny", "refresh_token": "r" * 40, "account_id": "a"}},  # at too short
    {"tokens": {"access_token": _LONG, "refresh_token": "short", "account_id": "a"}},    # rt too short
    {"tokens": {"access_token": _LONG, "refresh_token": "r" * 40, "account_id": ""}},    # empty acct
    {"tokens": "not-a-dict"},
    {"unrelated": "x" * 100},                         # non-empty but not codex
])
def test_rejects_incomplete_or_non_oauth(bad) -> None:
    m = _load()
    assert m._oauth_identity(bad) is None


def test_account_id_is_the_swap_guard() -> None:
    """The account_id is what the caller compares to block a credential SWAP:
    two valid bundles with DIFFERENT account_ids must be distinguishable."""
    m = _load()
    a = m._oauth_identity({"tokens": {"access_token": _LONG, "refresh_token": "r" * 40, "account_id": "acct-A"}})
    b = m._oauth_identity({"tokens": {"access_token": _LONG, "refresh_token": "r" * 40, "account_id": "acct-B"}})
    assert a is not None and b is not None and a[2] != b[2]
