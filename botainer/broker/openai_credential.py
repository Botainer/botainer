"""Host-side OpenAI/Codex credential source for the broker (agent-codex-broker).

The broker daemon (daemon.py) is provider-agnostic: it strips the client's
credential headers and injects an ``Authorization`` (+ optional extra headers)
on the outbound leg. This is the OpenAI keystore it uses — the exact analog of
``credential_source.BotainerCredentialBroker`` but for Codex.

API-KEY MODE (implemented here): reads botainer's OWN codex credential host-side
and injects ``Authorization: Bearer <sk-...>`` toward ``api.openai.com``. Two
on-disk shapes (both are botainer's own login store, NOT the container):
  * shared:   ``<state_root>/shared-auth/agent-codex/auth.json`` — JSON that
    carries ``OPENAI_API_KEY`` (and, for ChatGPT-subscription logins, a
    ``tokens`` block — that path is the SUBSCRIPTION-mode follow-up, not here).
  * isolated: ``<state_root>/state/<uuid>/data/agent-codex/profiles/<profile>/
    api_key`` — a plain one-line api-key file.
Re-read per call (so a key rotated by another writer is picked up); fail CLOSED
if no usable key. The real key never enters the container (the container holds a
sentinel). NO refresh (API keys don't expire); the ChatGPT-subscription OAuth
refresh + host/body rewrite is the documented follow-up (DN-033
§E is the plan; this file is API-key only).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from botainer.core.broker_sentinel import is_sentinel
from botainer.core.refusal import RefusalCategory, Refused

DEFAULT_AGENT = "agent-codex"
DEFAULT_PROFILE = "default"


def resolve_codex_credential_path(
    *, state_root: Path, mode: str, project_uuid: str | None = None,
    profile: str = DEFAULT_PROFILE, agent: str = DEFAULT_AGENT,
) -> Path:
    """Locate botainer's OWN codex credential file for ``mode``. Mirrors the
    paths the agent-codex(-shared) pre_session hooks bind — do not invent new
    ones. shared → ``shared-auth/<agent>/auth.json``; isolated →
    ``state/<uuid>/data/<agent>/profiles/<profile>/api_key``."""
    if mode == "shared":
        return state_root / "shared-auth" / agent / "auth.json"
    if mode == "isolated":
        if not project_uuid:
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                "isolated codex broker mode requires a project uuid")
        return (state_root / "state" / project_uuid / "data" / agent
                / "profiles" / profile / "api_key")
    raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                  f"unknown codex credential mode {mode!r}")


class OpenAICredentialBroker:
    """Keystore for daemon.serve_* — ``outbound_authorization()`` returns
    ``Bearer <sk-...>`` read fresh from botainer's codex credential file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    # ── daemon keystore contract ──

    def outbound_authorization(self) -> str:
        key = self._read_api_key()
        return f"Bearer {key}"

    def real_secret_value(self) -> str:
        """FOR HOST-SIDE TEST/SCANNER USE ONLY: the api key whose ABSENCE inside
        the container is asserted."""
        return self._read_api_key()

    # ── read + validate ──

    def _read_api_key(self) -> str:
        try:
            st = os.stat(self._path)
        except OSError as exc:
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential not found at {self._path}: {exc}")
        # Ownership + mode hygiene (mirror the anthropic broker): refuse a
        # wrong-uid or group/other-readable secret.
        if st.st_uid != os.getuid():
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential {self._path} owned by uid "
                          f"{st.st_uid}, not {os.getuid()}; refusing")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential {self._path} is group/other "
                          f"accessible; chmod 600 it")
        raw = Path(self._path).read_text(encoding="utf-8")
        key = self._extract_key(raw)
        if not key or not key.startswith("sk-"):
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"no usable OpenAI api key in {self._path} "
                          f"(need an sk-… key; re-run codex login)")
        if is_sentinel(key):
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          "codex credential holds a broker sentinel, not a real "
                          "key; refusing to forward a placeholder")
        return key

    @staticmethod
    def _extract_key(raw: str) -> str:
        """auth.json → OPENAI_API_KEY; else a plain api_key file (one line)."""
        raw = raw.strip()
        try:
            doc = json.loads(raw)
        except ValueError:
            return raw  # plain api_key file
        if isinstance(doc, dict):
            k = doc.get("OPENAI_API_KEY")
            if isinstance(k, str) and k:
                return k.strip()
        return ""
