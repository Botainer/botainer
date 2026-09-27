"""The keystore the broker daemon uses: botainer's OWN stored credential.

The credential is the one ``botainer auth login`` wrote — NEVER the host's
``~/.claude``. Path conventions mirror the plugin pre_session hooks exactly
(single source of truth for where the login flows write):

- shared mode (``plugins/agent-claude-shared/hooks/pre_session.py``):
      ``<state_root>/shared-auth/agent-claude/.credentials.json``
- isolated / per-project (``plugins/agent-claude/hooks/pre_session.py``):
      ``<state_root>/state/<uuid>/data/agent-claude/profiles/<profile>/
      .credentials.json``

File shape (written by Claude Code / the login flow)::

    {"claudeAiOauth": {"accessToken": "sk-ant-oat…",
                       "refreshToken": "sk-ant-ort…",
                       "expiresAt": <epoch MILLISECONDS>,
                       "scopes": [...], "subscriptionType": "max", ...}}

``expiresAt`` is epoch milliseconds (see the ``_MAX_REFRESH_DELTA_MS``
validation in agent-claude-shared's pre_session hook).

:class:`BotainerCredentialBroker`:

- RE-READS the credential file on every ``outbound_authorization()`` call, so
  a token refreshed by another writer (a shared-mode session's in-container
  Claude, another broker, a fresh ``botainer auth login``) is picked up.
- If the file's access token is expired/near-expiry AND a ``token_endpoint``
  + ``client_id`` are configured, refreshes host-side via
  :class:`~botainer.broker.oauth_refresh.RefreshingOAuthKeystore` and writes
  the refreshed (possibly ROTATED) tokens BACK to the botainer credential
  file — atomically, 0600, never into the container.
- Fails CLOSED (:class:`~botainer.core.refusal.Refused`) when no valid token
  exists and no refresh is configured. There is no silent downgrade.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from botainer.broker.oauth_refresh import (
    HttpPost,
    OAuthRefreshError,
    RefreshingOAuthKeystore,
)
from botainer.broker.refresh_lock import refresh_lock
from botainer.core.broker_sentinel import is_sentinel
from botainer.core.refusal import Refused, RefusalCategory
from botainer.state.secure_write import write_secure

DEFAULT_AGENT = "agent-claude"
DEFAULT_PROFILE = "default"

# After a failed refresh, serve the cached refusal for this cooldown instead of
# re-POSTing the OAuth endpoint on every request (rate-limit protection).
_REFRESH_FAIL_COOLDOWN_S = 30.0


def resolve_credential_path(
    *,
    state_root: Path,
    mode: str,
    project_uuid: str | None = None,
    profile: str = DEFAULT_PROFILE,
    agent: str = DEFAULT_AGENT,
) -> Path:
    """Resolve the botainer credential file for ``mode`` (shared | isolated).

    These are the SAME paths the pre_session hooks bind: do not invent new
    ones. Shared mirrors ``plugins/agent-claude-shared/hooks/pre_session.py``
    (``shared_dir = state_root / "shared-auth" / "agent-claude"``); isolated
    mirrors ``plugins/agent-claude/hooks/pre_session.py``
    (``state_root / "state" / uid / "data" / "agent-claude" / "profiles" /
    profile``).
    """
    if mode == "shared":
        return state_root / "shared-auth" / agent / ".credentials.json"
    if mode == "isolated":
        if not project_uuid:
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                "isolated auth mode requires a project uuid to locate the "
                "per-project credential",
            )
        return (
            state_root / "state" / project_uuid / "data" / agent
            / "profiles" / profile / ".credentials.json"
        )
    raise Refused(
        RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
        f"unknown auth mode {mode!r} (expected 'shared' or 'isolated')",
    )


class BotainerCredentialBroker:
    """Keystore over botainer's own stored Claude OAuth credential.

    Satisfies the daemon's :class:`~botainer.broker.daemon.Keystore` contract:
    ``outbound_authorization()`` returns ``Bearer <access>`` (refreshing when
    needed), ``real_secret_value()`` returns the durable refresh token for
    absence-scanning. Both re-read the file on every call.
    """

    def __init__(
        self,
        credential_path: Path,
        *,
        token_endpoint: str | None = None,
        client_id: str | None = None,
        http_post: HttpPost | None = None,
        skew_seconds: int = 120,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(credential_path)
        self._endpoint = token_endpoint
        self._client_id = client_id
        self._post = http_post
        self._skew = skew_seconds
        self._now = now
        self._refresh_failed_at = 0.0  # backoff clock (see _REFRESH_FAIL_COOLDOWN_S)

    # ── file access ──

    def _load_oauth(self) -> tuple[dict, dict]:
        """Read + validate the credential file. Returns (document, oauth).

        Ownership/mode validation mirrors agent-claude-shared's pre_session
        hook: refuse a file owned by another uid or readable by group/other.
        """
        if not self._path.exists():
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"no botainer credential at {self._path}; "
                f"run `botainer auth login` first",
            )
        st = self._path.stat()
        if st.st_uid != os.getuid():
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"refusing credential file owned by uid={st.st_uid} "
                f"(expected {os.getuid()}): {self._path}",
            )
        if (st.st_mode & 0o077) != 0:
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"refusing credential file with mode "
                f"{oct(st.st_mode & 0o777)} (expected 0600 or stricter). "
                f"Fix: chmod 600 {self._path}",
            )
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"could not parse credential file {self._path}: {exc}",
            ) from exc
        oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
        if not isinstance(oauth, dict):
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"credential file {self._path} has no claudeAiOauth section",
            )
        # A sentinel in the HOST store means nobody ever logged in — the
        # placeholder leaked back somehow. Never forward a sentinel upstream.
        for field in ("accessToken", "refreshToken"):
            if is_sentinel(oauth.get(field)):
                raise Refused(
                    RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                    f"credential file {self._path} holds a broker sentinel in "
                    f"{field} — not a real credential; run `botainer auth login`",
                )
        return doc, oauth

    @staticmethod
    def _expires_at_seconds(oauth: dict) -> float:
        """``expiresAt`` is epoch MILLISECONDS in the file; we work in seconds.
        Missing/zero/malformed ⇒ 0.0 (treated as already expired: the token is
        then only usable via a configured refresh — fail toward safety)."""
        raw = oauth.get("expiresAt")
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw) / 1000.0
        return 0.0

    # ── the daemon keystore contract ──

    def outbound_authorization(self) -> str:
        """``Bearer <access>``, re-reading the botainer file on every call.

        Order: fresh file token wins (another writer may have refreshed it);
        else refresh host-side when configured (writing rotated tokens back);
        else fail closed.
        """
        _, oauth = self._load_oauth()
        access = oauth.get("accessToken")
        if isinstance(access, str) and access and not self._token_stale(oauth):
            return f"Bearer {access}"
        return self._refresh_and_write_back()

    # The Anthropic OAuth auth style: a Bearer OAuth access token
    # (``sk-ant-oat…``) is only accepted when accompanied by this beta flag.
    # Claude Code, when it thinks it is talking to a custom endpoint via
    # ANTHROPIC_AUTH_TOKEN, may NOT send it — so the broker guarantees it. See
    # the broker prototype's own notes: it requires Authorization: Bearer
    # <token> PLUS anthropic-beta: oauth-2025-04-20. The daemon MERGES it into any
    # anthropic-beta the client already sent (it never overwrites feature flags).
    OAUTH_BETA = "oauth-2025-04-20"

    def outbound_headers(self) -> dict[str, str]:
        """Extra headers the daemon injects host-side alongside the Bearer.

        Only the OAuth beta flag — required for the subscription OAuth token to
        be accepted (and billed as subscription, not pay-as-you-go API)."""
        return {"anthropic-beta": self.OAUTH_BETA}

    def real_secret_value(self) -> str:
        """FOR HOST-SIDE TEST/SCANNER USE ONLY: the durable refresh token,
        whose ABSENCE inside the container is asserted."""
        _, oauth = self._load_oauth()
        refresh = oauth.get("refreshToken")
        if not isinstance(refresh, str) or not refresh:
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                f"credential file {self._path} has no refreshToken",
            )
        return refresh

    # ── refresh + write-back ──

    def _token_stale(self, oauth: dict) -> bool:
        return self._now() >= (self._expires_at_seconds(oauth) - self._skew)

    def _refresh_and_write_back(self) -> str:
        """Refresh host-side via RefreshingOAuthKeystore; persist the rotated
        tokens back to the botainer credential file (0600, atomic).

        Concurrency: takes a best-effort exclusive flock on the credential
        file for the read→refresh→write critical section, and re-checks after
        locking — if another process already refreshed while we waited, its
        (newer) token is used and no second refresh is spent. flock is
        advisory + best-effort here (write_secure's rename is atomic either
        way); the shared-mode hook applies the same discipline.
        """
        doc, oauth = self._load_oauth()
        refresh_token = oauth.get("refreshToken")
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or not self._endpoint
            or not self._client_id
        ):
            raise Refused(
                RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                "access token expired and no refresh available "
                "(missing refreshToken or refresh not configured); "
                "re-authenticate",
            )

        # Serialize the read→refresh→write section across processes. flock when
        # the filesystem supports it, else an O_EXCL lockfile; FAIL CLOSED if it
        # can't serialize — two sessions must never both spend the rotating
        # refresh token (the second trips the provider's reuse lockout). Re-check
        # under the lock so a concurrent refresh isn't spent twice.
        with refresh_lock(self._path, now=self._now):
            doc, oauth = self._load_oauth()
            access = oauth.get("accessToken")
            if isinstance(access, str) and access and not self._token_stale(oauth):
                self._refresh_failed_at = 0.0
                return f"Bearer {access}"
            refresh_token = oauth.get("refreshToken")
            if not isinstance(refresh_token, str) or not refresh_token:
                raise Refused(
                    RefusalCategory.BROKER_CREDENTIAL_UNAVAILABLE,
                    f"credential at {self._path} lost its refreshToken",
                )

            # Backoff: don't re-POST the OAuth endpoint on every request after a
            # failed refresh (a revoked token + a request flood would hammer the
            # provider and risk rate-limiting the account). Serve the cached
            # refusal for a short cooldown.
            if self._now() - self._refresh_failed_at < _REFRESH_FAIL_COOLDOWN_S:
                raise Refused(
                    RefusalCategory.BROKER_REFRESH_FAILED,
                    "token refresh recently failed; backing off before retry "
                    "(the refresh token may be expired/revoked — re-authenticate)",
                )

            ks = RefreshingOAuthKeystore(
                refresh_token,
                token_endpoint=self._endpoint,
                client_id=self._client_id,
                http_post=self._post,
                skew_seconds=self._skew,
                now=self._now,
            )
            try:
                header = ks.outbound_authorization()
            except OAuthRefreshError as exc:
                self._refresh_failed_at = self._now()
                raise Refused(
                    RefusalCategory.BROKER_REFRESH_FAILED,
                    f"OAuth token refresh failed ({exc}); the stored refresh "
                    f"token may be expired/revoked — re-authenticate",
                ) from exc

            # Write the refreshed (possibly rotated) tokens BACK to the
            # botainer file — host-side only, atomic, 0600 from byte 1.
            oauth["accessToken"] = ks.access_token
            oauth["refreshToken"] = ks.refresh_token  # rotation adopted
            oauth["expiresAt"] = int(ks.expires_at * 1000)  # file unit is ms
            doc["claudeAiOauth"] = oauth
            write_secure(self._path, json.dumps(doc), mode=0o600)
            self._refresh_failed_at = 0.0
            return header
