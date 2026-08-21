"""Host-side OAuth refresh for the broker.

Ported from the proven spike at
``DN-015/broker/oauth_refresh.py``.

The broker holds the OAuth REFRESH token host-side (never in the container)
and mints/refreshes the short-lived ACCESS token as needed, injecting
``Authorization: Bearer <access>`` on the outbound leg. This matches the
empirical result: an expired access token → 401; a live one → auth OK.

:class:`RefreshingOAuthKeystore` is a drop-in keystore for
``daemon.handle_request`` (it has ``outbound_authorization()`` +
``real_secret_value()``). ``token_endpoint``, ``client_id`` and the HTTP
transport are REQUIRED/injectable — no guessed security constants are baked
in, and the transport is mockable so the refresh LOGIC is unit-tested without
touching a real endpoint or consuming a real (rotating) refresh token.

For Claude Code's subscription OAuth, the endpoint + client_id are the values
Claude Code itself uses; verify them against a THROWAWAY ``claude`` login
before production use (rotating the primary login's refresh token would log
the operator out).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Callable

HttpPost = Callable[[str, dict[str, str]], tuple[int, dict]]


def _default_http_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
    """Real transport: application/JSON POST → parsed JSON.

    Claude Code's OAuth token endpoint expects a JSON body (NOT
    form-urlencoded — verified against three independent implementations). A
    4xx/5xx is returned as (status, parsed-body) so the caller fails closed with
    the endpoint's own error rather than an opaque exception."""
    import urllib.error

    data = json.dumps(form).encode()
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


class OAuthRefreshError(RuntimeError):
    """The token endpoint refused / returned garbage. Fail closed: the caller
    must NOT fall back to any weaker credential path."""


class RefreshingOAuthKeystore:
    """Host-only keystore that refreshes the access token on demand.

    State (all host-side): the current refresh token, the current access
    token, and its expiry (epoch SECONDS). ``outbound_authorization()``
    returns a *fresh* Bearer value, refreshing first if the access token is
    missing or within ``skew_seconds`` of expiry. ``refresh_on_unauthorized()``
    lets a caller force one refresh + retry when the upstream returns 401
    despite a non-expired cached token (clock skew / server-side early expiry).
    """

    def __init__(
        self,
        refresh_token: str,
        *,
        token_endpoint: str,
        client_id: str,
        access_token: str | None = None,
        expires_at: float = 0.0,
        http_post: HttpPost | None = None,
        skew_seconds: int = 120,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not refresh_token:
            raise ValueError(
                "RefreshingOAuthKeystore requires a refresh token (host-side only)"
            )
        if not token_endpoint or not client_id:
            raise ValueError(
                "token_endpoint and client_id are required (verify them empirically)"
            )
        # The durable refresh token is POSTed here — never over plaintext http
        # (defense-in-depth vs an operator/config mistake; sharp-edges LOW
        #). Mirror the daemon's upstream https gate + its test escape.
        if not token_endpoint.startswith("https://") and not os.environ.get(
                "BOTAINER_TESTING"):
            raise ValueError(
                f"token_endpoint must be https:// (got {token_endpoint!r}); the "
                f"refresh token is a durable secret."
            )
        self._refresh_token = refresh_token
        self._access_token = access_token
        self._expires_at = float(expires_at or 0.0)
        self._endpoint = token_endpoint
        self._client_id = client_id
        self._post = http_post or _default_http_post
        self._skew = skew_seconds
        self._now = now

    # ── the keystore interface daemon.handle_request expects ──

    def outbound_authorization(self) -> str:
        """A live ``Bearer <access>`` value, refreshing first if needed."""
        if self._access_token is None or self._now() >= (self._expires_at - self._skew):
            self._refresh()
        return f"Bearer {self._access_token}"

    def real_secret_value(self) -> str:
        """FOR HOST-SIDE TEST/SCANNER USE ONLY: the durable secret whose
        ABSENCE in the container is asserted. It is the REFRESH token — never
        the access token, and never sent into a container."""
        return self._refresh_token

    # ── state the credential source reads back after a refresh ──

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def expires_at(self) -> float:
        """Epoch seconds when the current access token expires."""
        return self._expires_at

    # ── refresh ──

    def _refresh(self) -> None:
        try:
            status, resp = self._post(
                self._endpoint,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                },
            )
        except OAuthRefreshError:
            raise
        except Exception as exc:  # transport failure → fail closed
            raise OAuthRefreshError(f"refresh transport failed: {exc}") from exc
        if status != 200 or not isinstance(resp, dict) or not resp.get("access_token"):
            raise OAuthRefreshError(
                f"refresh failed (status {status}): {str(resp)[:200]}"
            )
        self._access_token = resp["access_token"]
        self._expires_at = self._now() + float(resp.get("expires_in", 3600))
        # refresh-token ROTATION: if the server returned a new refresh token,
        # the broker (the sole holder) adopts it. In production this is fine;
        # during testing against a shared login it would invalidate the other
        # holder — which is why any real-endpoint test must use a throwaway
        # login.
        if resp.get("refresh_token"):
            self._refresh_token = resp["refresh_token"]

    def refresh_on_unauthorized(self) -> None:
        """Force a refresh (caller saw a 401 despite a non-expired cache)."""
        self._access_token = None
        self._refresh()
