"""The credential-broker sentinel (#T0-3 replacement — see DN-010
sibling work / handoff broker prototype).

A deliberately-unmistakable FAKE value the broker places in the container instead
of a real credential:

    BROKER-SENTINEL.tenant=<id>.nonce=<hex>.NOT-A-REAL-CREDENTIAL

The security boundary is NOT the sentinel — it is that the real credential is held
host-side and injected on the outbound leg by the broker daemon. The sentinel just
lets a human / the credential-leak guard / a scanner confirm INSTANTLY that the
container holds a placeholder, never a secret. Ported into core so the leak guard
can allow it (it carries no secret) while still refusing anything real-looking.
"""
from __future__ import annotations

import re

PREFIX = "BROKER-SENTINEL"
SUFFIX = "NOT-A-REAL-CREDENTIAL"
_TENANT_CHARSET = r"[A-Za-z0-9_-]"
_NONCE_CHARSET = r"[0-9a-f]"
_RE = re.compile(
    rf"\A{PREFIX}\.tenant=(?P<tenant>{_TENANT_CHARSET}+)"
    rf"\.nonce=(?P<nonce>{_NONCE_CHARSET}+)\.{SUFFIX}\Z"
)
# The NEGATION of the same string, not a second spelling of it — `(?!X).` would
# have been the obvious way and is wrong, because `.` does not match a newline,
# so a tenant containing one would survive the filter and break the format.
_NOT_TENANT = re.compile("[^" + _TENANT_CHARSET[1:])
_NONCE_OK = re.compile(rf"\A{_NONCE_CHARSET}+\Z")   # \Z not $: $ allows a trailing \n


def make_sentinel(tenant_id: str, nonce: str) -> str:
    """Build a sentinel that ``is_sentinel`` is GUARANTEED to recognise.

    #216. The charset used to live in three places: this regex, and a
    hand-written filter in each of the two broker hooks —

        "".join(c for c in tenant_id if c.isalnum() or c in "_-")

    — which is not the same charset. ``str.isalnum()`` is UNICODE-aware, so it
    keeps `Ⅷ`, `٣` and full-width `ｆ`, none of which `[A-Za-z0-9_-]` matches.
    A tenant containing one produced a value that `make_sentinel` emitted
    happily and `is_sentinel` then rejected, and the two consequences point in
    OPPOSITE directions:

      - `broker/*_credential.py` refuse to forward a value they recognise as a
        sentinel. Unrecognised → the guard fails OPEN and a placeholder goes
        upstream.
      - `core/credential_leak_check.py` allows a value it recognises as a
        sentinel. Unrecognised → fails CLOSED, and the launch is refused for
        "leaking" something that carries no secret.

    Reachable today only if a project uuid is non-ASCII, which botainer's own
    ids never are. It is fixed as STRUCTURE rather than left as a latent bug:
    the constructor now owns the charset, so a sentinel that the recogniser
    rejects cannot be built. The hooks pass the raw value and keep no filter of
    their own — a rule replicated in two siblings is the drift shape (#136),
    and this one had already drifted.

    THE TWO ARGUMENTS ARE TREATED DIFFERENTLY, ON PURPOSE:

      tenant  a cosmetic label, for a human reading an env dump → SANITISED.
              Losing a character costs nothing.
      nonce   SECURITY-BEARING. On the TCP transport the sentinel doubles as
              the daemon's required access token, so quietly rewriting it would
              hand the container a token the daemon does not accept — a working
              session turned into an authentication failure with no cause
              printed. Validated and RAISED on instead.
    """
    tenant = _NOT_TENANT.sub("", str(tenant_id))[:32] or "session"
    if not _NONCE_OK.match(str(nonce)):
        # Not sanitised: see above. Callers use secrets.token_hex().
        raise ValueError(
            "broker sentinel nonce must be lowercase hex "
            f"(got {len(str(nonce))} chars that do not match {_NONCE_CHARSET}+)")
    return f"{PREFIX}.tenant={tenant}.nonce={nonce}.{SUFFIX}"


# THE SENTINEL IS ONE LITERAL SHAPE, and deliberately stays that way.
#
# A JWT-wrapped variant briefly existed here (2026-09-04): agent-codex-broker
# wrapped the sentinel in an unsigned `alg: none` JWT with a far-future `exp`,
# on the theory that codex was refusing it because it could not parse it as a
# token and so tried to refresh. `is_sentinel` grew an unwrapper to match, and
# the credential-leak guard — which had correctly refused the unrecognisable
# wrapped value — went quiet again.
#
# The theory was wrong. Running codex 0.153.2 against a logging server showed
# it never contacted the broker at all: `OPENAI_BASE_URL` is ignored, and codex
# was sending the sentinel to OpenAI, which is what rejected it. The wrapper
# was answering a question nobody had asked, and the unwrapper existed only to
# stop a security check from noticing. Both are gone. If a future agent needs a
# token-SHAPED placeholder, that shape belongs in that plugin's own module —
# not by loosening what this one recognises.


def is_sentinel(value: object) -> bool:
    return isinstance(value, str) and bool(_RE.match(value.strip()))


def sentinel_tenant(value: object) -> str | None:
    m = _RE.match((value or "").strip()) if isinstance(value, str) else None
    return m.group("tenant") if m else None
