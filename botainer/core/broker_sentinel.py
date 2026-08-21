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
_RE = re.compile(
    r"^BROKER-SENTINEL\.tenant=(?P<tenant>[A-Za-z0-9_-]+)"
    r"\.nonce=(?P<nonce>[0-9a-f]+)\.NOT-A-REAL-CREDENTIAL$"
)


def make_sentinel(tenant_id: str, nonce: str) -> str:
    return f"{PREFIX}.tenant={tenant_id}.nonce={nonce}.{SUFFIX}"


def is_sentinel(value: object) -> bool:
    return isinstance(value, str) and bool(_RE.match(value.strip()))


def sentinel_tenant(value: object) -> str | None:
    m = _RE.match((value or "").strip()) if isinstance(value, str) else None
    return m.group("tenant") if m else None
