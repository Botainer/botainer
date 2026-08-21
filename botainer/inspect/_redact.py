"""Shared credential-redaction helper used by every render surface.

Tasks #229 + #298: was duplicated as `_redact_if_credential` in
preflight.py only, leaving json_out / config get / etc. to leak raw
values. Now a single source of truth.

#298 finding (information leakage via first-4/last-4): if the
caller wants a brute-force-safe view they pass `mode='full'`. Default
mode is `safe` which returns `<redacted>` for anything credential-y
regardless of length.
"""

from __future__ import annotations

_CREDENTIAL_NAME_HINTS = (
    "token", "key", "secret", "password", "credential",
    "bearer", "oauth", "jwt", "passwd", "pwd",
)


def looks_credential(key: str) -> bool:
    """True if `key` looks like an env var holding a credential."""
    lowered = key.lower()
    return any(hint in lowered for hint in _CREDENTIAL_NAME_HINTS)


def redact(key: str, value: str, *, mode: str = "safe") -> str:
    """Redact `value` if `key` looks credential-y.

    mode='safe' (default): always returns '<redacted>' for credential keys.
                            Used by inspect --json + config get (no leak
                            even for short tokens; closes #298 oracle).
    mode='preview':         returns 'AAAA…ZZZZ (N chars)' for values > 8.
                            Preflight terminal view uses this; user has
                            already consented to displaying full env.
    """
    if not looks_credential(key):
        return value
    if mode == "preview" and len(value) > 8:
        return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"
    return "<redacted>"
