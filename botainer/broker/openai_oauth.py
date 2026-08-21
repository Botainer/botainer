"""Host-side Codex ChatGPT-SUBSCRIPTION credential source for the broker.

The subscription-mode analog of ``openai_credential.OpenAICredentialBroker``
(which is API-key only). A ChatGPT (OAuth) codex login stores tokens — not an
api key — and codex talks to the ChatGPT backend, not api.openai.com. This
keystore lets the broker replicate that: it reads botainer's OWN codex OAuth
bundle host-side, refreshes the short-lived access token when needed (writing the
ROTATED refresh token back), and hands the daemon a fresh
``Authorization: Bearer <access>`` plus the ``chatgpt-account-id`` header the
ChatGPT backend requires. The real tokens never enter the container.

Everything here is pinned to the open-source ``openai/codex`` implementation
(`codex-rs/login/`), verified:

* auth.json shape (ChatGPT login)::

    {"OPENAI_API_KEY": null,
     "tokens": {"id_token": "<jwt>", "access_token": "<jwt>",
                "refresh_token": "<opaque>", "account_id": "<id>"},
     "last_refresh": "<rfc3339>"}

* Refresh: ``POST https://auth.openai.com/oauth/token``, JSON body
  ``{client_id, grant_type: "refresh_token", refresh_token}`` (NO scope),
  client_id ``app_EMoamEEZ73f0CkXaXp7hrann``. Response
  ``{id_token?, access_token?, refresh_token?}`` (no ``expires_in``). The
  refresh token ROTATES — the response's ``refresh_token`` MUST be persisted or
  the next call trips the backend's ``refresh_token_reused`` lockout.
* Expiry: decode the access_token JWT ``exp``; refresh within a 5-minute window
  (``auth.openai.com`` issues JWTs). Fallback when ``exp`` is unreadable:
  ``last_refresh + 8 days`` (codex's ``TOKEN_REFRESH_INTERVAL``).
* ``account_id``: ``tokens.account_id``; if absent, the ``id_token`` JWT claim
  ``["https://api.openai.com/auth"]["chatgpt_account_id"]``. Preserved on refresh.

The endpoint + client_id are PINNED constants (never from the untrusted project
config): the durable refresh token is sent there, so a hostile config must not be
able to redirect it. ``http_post`` is injectable so the refresh LOGIC is
unit-tested without a real endpoint or consuming a real (rotating) token.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from botainer.broker.refresh_lock import refresh_lock
from botainer.core.broker_sentinel import is_sentinel
from botainer.core.refusal import RefusalCategory, Refused
from botainer.state.secure_write import write_secure

# After a failed refresh, don't re-POST the OAuth endpoint on every request (a
# revoked token + a request flood would otherwise hammer the provider and get
# the account rate-limited). Serve the cached refusal for this cooldown.
_REFRESH_FAIL_COOLDOWN_S = 30.0

# PINNED (see module docstring). The refresh token is the durable secret; its
# destination is a trusted constant, never a config key.
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_ACCOUNT_CLAIM_NS = "https://api.openai.com/auth"
_REFRESH_WINDOW_SECONDS = 300           # codex CHATGPT_ACCESS_TOKEN_REFRESH_WINDOW
_FALLBACK_TTL_SECONDS = 8 * 24 * 3600   # codex TOKEN_REFRESH_INTERVAL (8 days)

HttpPost = Callable[[str, dict], "tuple[int, dict]"]


def _json_post(url: str, body: dict) -> tuple[int, dict]:
    """Real transport: application/json POST → (status, parsed json). A 4xx/5xx
    body is returned parsed so the caller fails closed with the endpoint's own
    error rather than an opaque exception."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": "non-json-error-body"}


def _jwt_payload(token: str) -> dict | None:
    """Decode a JWT's payload segment (base64url, no signature check — we only
    read non-secret claims like ``exp`` / account id). Returns None on anything
    malformed."""
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    seg = token.split(".")[1]
    seg += "=" * (-len(seg) % 4)  # restore base64 padding
    try:
        return json.loads(base64.urlsafe_b64decode(seg))
    except (binascii.Error, ValueError, TypeError):
        return None


class CodexOAuthCredentialBroker:
    """Keystore over botainer's own codex ChatGPT-OAuth bundle.

    daemon contract: ``outbound_authorization()`` → ``Bearer <access>`` (refreshing
    host-side when near expiry, persisting rotation); ``outbound_headers()`` →
    ``{"chatgpt-account-id": <id>}`` (the ChatGPT backend 401s without it);
    ``real_secret_value()`` → the durable refresh token for absence-scanning.
    """

    def __init__(
        self,
        path: Path,
        *,
        token_endpoint: str = TOKEN_ENDPOINT,
        client_id: str = CLIENT_ID,
        http_post: HttpPost | None = None,
        skew_seconds: int = _REFRESH_WINDOW_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        if not token_endpoint.startswith("https://") and not os.environ.get(
                "BOTAINER_TESTING"):
            raise ValueError(
                f"token_endpoint must be https:// (got {token_endpoint!r}); the "
                f"refresh token is a durable secret."
            )
        self._endpoint = token_endpoint
        self._client_id = client_id
        self._post = http_post or _json_post
        self._skew = skew_seconds
        self._now = now
        self._refresh_failed_at = 0.0  # backoff clock (see _REFRESH_FAIL_COOLDOWN_S)

    # ── file access + validation ──

    def _load(self) -> tuple[dict, dict]:
        """Read + validate auth.json; return (document, tokens). Ownership/mode
        hygiene mirrors the API-key broker: refuse a wrong-uid or group/other
        accessible secret."""
        try:
            st = os.stat(self._path)
        except OSError as exc:
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential not found at {self._path}: {exc}")
        if st.st_uid != os.getuid():
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential {self._path} owned by uid "
                          f"{st.st_uid}, not {os.getuid()}; refusing")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential {self._path} is group/other "
                          f"accessible; chmod 600 it")
        try:
            doc = json.loads(Path(self._path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"could not parse codex credential {self._path}: {exc}")
        tokens = doc.get("tokens") if isinstance(doc, dict) else None
        if not isinstance(tokens, dict):
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"codex credential {self._path} has no `tokens` (not a ChatGPT "
                f"login?); subscription broker mode needs a ChatGPT codex login")
        rt = tokens.get("refresh_token")
        if not isinstance(rt, str) or not rt or is_sentinel(rt):
            raise Refused(RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                          f"codex credential {self._path} has no usable "
                          f"refresh_token; re-run codex login")
        return doc, tokens

    # ── daemon keystore contract ──

    def outbound_authorization(self) -> str:
        doc, tokens = self._load()
        access = tokens.get("access_token")
        if (isinstance(access, str) and access
                and not self._stale(access, doc.get("last_refresh"))):
            return f"Bearer {access}"
        return self._refresh_and_write_back()

    def outbound_headers(self) -> dict[str, str]:
        _, tokens = self._load()
        acct = self._account_id(tokens)
        if not acct:
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"codex credential {self._path} has no account_id (nor a "
                f"chatgpt_account_id id_token claim); the ChatGPT backend "
                f"requires the chatgpt-account-id header")
        return {"chatgpt-account-id": acct}

    def real_secret_value(self) -> str:
        _, tokens = self._load()
        return tokens["refresh_token"]

    # ── helpers ──

    def _account_id(self, tokens: dict) -> str | None:
        acct = tokens.get("account_id")
        if isinstance(acct, str) and acct:
            return acct
        claims = _jwt_payload(tokens.get("id_token"))
        ns = claims.get(_ACCOUNT_CLAIM_NS) if isinstance(claims, dict) else None
        if isinstance(ns, dict):
            cid = ns.get("chatgpt_account_id")
            if isinstance(cid, str) and cid:
                return cid
        return None

    def _stale(self, access: str, last_refresh) -> bool:
        """True if the access token should be refreshed. Primary signal: the JWT
        ``exp`` (refresh within ``skew`` of it). Fallback (unreadable exp):
        ``last_refresh + 8 days``."""
        claims = _jwt_payload(access)
        exp = claims.get("exp") if isinstance(claims, dict) else None
        if isinstance(exp, (int, float)) and exp > 0:
            return self._now() >= (float(exp) - self._skew)
        # Fallback: codex's last_refresh + TOKEN_REFRESH_INTERVAL.
        ts = _parse_iso(last_refresh)
        if ts is None:
            return True  # no exp, no last_refresh → refresh to be safe
        return self._now() >= (ts + _FALLBACK_TTL_SECONDS)

    def _refresh_and_write_back(self) -> str:
        """Refresh at the pinned endpoint and persist the (rotated) tokens back
        to auth.json — atomic, 0600, host-side only. Serialized across processes
        by ``refresh_lock`` (flock, or an O_EXCL lockfile on lock-less
        filesystems; fail-closed if it can't serialize) so two sessions can't
        both spend the rotating refresh token. Re-checks under the lock so a
        concurrent refresh isn't spent twice."""
        with refresh_lock(self._path, now=self._now):
            doc, tokens = self._load()
            access = tokens.get("access_token")
            if (isinstance(access, str) and access
                    and not self._stale(access, doc.get("last_refresh"))):
                self._refresh_failed_at = 0.0
                return f"Bearer {access}"  # another writer already refreshed

            # Backoff: after a failed refresh (e.g. a revoked token), don't
            # re-POST on every request — that hammers the provider's OAuth
            # endpoint and can get the account rate-limited. Serve the cached
            # refusal for a short cooldown (audit MEDIUM).
            if self._now() - self._refresh_failed_at < _REFRESH_FAIL_COOLDOWN_S:
                raise Refused(
                    RefusalCategory.BROKER_REFRESH_FAILED,
                    "codex token refresh recently failed; backing off before "
                    "retry (the refresh token may be expired/revoked — re-run "
                    "codex login)")

            rt = tokens["refresh_token"]
            try:
                status, resp = self._post(self._endpoint, {
                    "client_id": self._client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                })
            except Exception as exc:  # transport failure → fail closed
                self._refresh_failed_at = self._now()
                raise Refused(RefusalCategory.BROKER_REFRESH_FAILED,
                              f"codex token refresh transport failed: {exc}")
            if (status != 200 or not isinstance(resp, dict)
                    or not resp.get("access_token")):
                self._refresh_failed_at = self._now()
                raise Refused(
                    RefusalCategory.BROKER_REFRESH_FAILED,
                    f"codex token refresh failed (status {status}); the stored "
                    f"refresh token may be expired/revoked — re-run codex login")

            new_access = resp["access_token"]
            tokens["access_token"] = new_access
            if resp.get("id_token"):
                tokens["id_token"] = resp["id_token"]
            # ROTATION: the response's refresh_token MUST be adopted+persisted, or
            # the next refresh trips the backend's refresh_token_reused lockout.
            if resp.get("refresh_token"):
                tokens["refresh_token"] = resp["refresh_token"]
            # account_id is NOT touched on refresh (codex preserves it).
            doc["tokens"] = tokens
            doc["last_refresh"] = _utc_now_iso(self._now)
            write_secure(self._path, json.dumps(doc), mode=0o600)
            self._refresh_failed_at = 0.0
            return f"Bearer {new_access}"


def _parse_iso(value) -> float | None:
    """RFC3339/ISO-8601 → epoch seconds; None on anything unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _utc_now_iso(now: Callable[[], float]) -> str:
    return datetime.fromtimestamp(now(), tz=timezone.utc).isoformat()
