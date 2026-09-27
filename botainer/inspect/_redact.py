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
    """True if `key` looks like an env var holding a credential.

    THE UNION OF TWO PREDICATES, and that is the whole point.

    botainer had two and they disagreed on eight names, in one command's output.
    Measured on one config: `config get env` printed `DB_PASS`, `SLACK_WEBHOOK`
    and `STRIPE_SK` IN FULL, with a note saying one value had been redacted,
    while `botainer config check` on the same file seconds later said
    "credential-shaped env vars in config: ['ANTHROPIC_API_KEY', 'DB_PASS',
    'SLACK_WEBHOOK', 'STRIPE_SK'] — `botainer start` will refuse this config".

    So the surface that DISPLAYS a value was narrower than the surface that
    REFUSES TO LAUNCH over it. That ordering is the wrong way round: a name the
    launcher considers dangerous enough to refuse must not be one this prints.

    Neither list is a superset of the other, so the fix is the union, not a
    replacement:

      * `credential_leak_check` matches ANCHORED forms — `_API_KEY$`,
        `^SECRET[_A-Z0-9]`, `^STRIPE_`, plus ~40 exact vendor names. It knows
        `DB_PASS` and `MAILGUN_API` and this module never did.
      * the hints below match a word ANYWHERE, so they catch `MY_TOKEN_HERE`
        and `some_password_2` that no anchored pattern reaches.

    Taking the union makes "display is at least as wide as refusal" a PROPERTY
    rather than a rule someone has to remember when editing either list — and
    `tests/unit/test_display_redaction_is_never_narrower_than_refusal.py`
    asserts it over the launcher's own name corpus, so adding a pattern there
    can never again leave this one behind.
    """
    lowered = key.lower()
    if any(hint in lowered for hint in _CREDENTIAL_NAME_HINTS):
        return True
    # Function-local: `botainer.core` must not be import-time coupled to
    # `botainer.inspect`, and this is the only call.
    from botainer.core.credential_leak_check import detect_credential_env_keys
    return bool(detect_credential_env_keys({key: ""}))


#: Config FIELD names that mention credentials but do not HOLD one.
#:
#: `looks_credential` answers "does this ENV VAR name suggest it holds a
#: secret" — its own docstring says so. Applied to botainer's own config
#: schema the word changes meaning: `credential_scope` is WHICH login store a
#: broker opens, and `inject_credentials` is WHOSE credentials get bound into
#: the cage. Both are settings ABOUT credentials. Redacting them hides exactly
#: the two fields someone debugging a cross-project login problem needs to
#: read, and asserts they are secrets.
#:
#: THIS IS A FILTER BACKING A STRUCTURAL GAP, and the gap is that nothing in
#: the type system distinguishes "a field botainer defines" from "a
#: user-supplied env var name" — both arrive here as `str`. Until they are
#: different types, this list is the backup. `tests/unit/
#: test_config_get_redacts_nested_credentials.py` asserts every name here is a
#: REAL field of the shipped schema, so it cannot rot into a permission slip
#: for a name nobody uses.
_CONFIG_SETTING_NAMES = frozenset({
    "credential_scope",
    "inject_credentials",
})


def looks_credential_config_key(key: str) -> bool:
    """True if a CONFIG key names a value that is itself a credential.

    Use this — not `looks_credential` — when walking `.botainer/config.yaml`.
    The env-var predicate over-fires on botainer's own schema; see
    `_CONFIG_SETTING_NAMES`.
    """
    return key not in _CONFIG_SETTING_NAMES and looks_credential(key)


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
