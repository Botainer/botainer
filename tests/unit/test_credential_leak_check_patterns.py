"""Tasks #157 + #295: credential_leak_check denylist patterns must
catch prefix-form names and vendor-prefixed common cases.

Old patterns only matched suffix forms ('_API_KEY$', '_SECRET$', etc.),
so 'BEARER_TOKEN', 'SECRET_PROD_DB', 'DB_PASS', 'OAUTH_FOO',
'SLACK_BOT_TOKEN', etc. passed through to the container.
"""

from __future__ import annotations

import pytest

from botainer.core.credential_leak_check import detect_credential_env_keys


@pytest.mark.parametrize(
    "name",
    [
        # Task #157 representative cases
        "BEARER_TOKEN",
        "BEARER_API",
        "SECRET_PROD_DB",
        "SECRET_FOO",
        "DB_PASS",
        "DB_PWD",
        "DB_SECRET",
        "OAUTH_TOKEN",
        "OAUTH_CLIENT_SECRET",
        "JWT",
        "JWT_SECRET",
        "TOKEN_FOO",
        # Task #295 vendor-prefixed
        "SLACK_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_WEBHOOK",
        "STRIPE_KEY",
        "TWILIO_AUTH_TOKEN",
        "SENDGRID_API_KEY",
        "MAILGUN_API_KEY",
        "DOCKER_PASSWORD",
        "KUBE_TOKEN",
        # Original suffix forms must still match
        "ANTHROPIC_API_KEY",
        "FOO_SECRET",
        "BAR_ACCESS_TOKEN",
        "BAZ_PASSWORD",
    ],
)
def test_credential_pattern_matches(name: str) -> None:
    """All these names must be flagged as credential-like."""
    assert detect_credential_env_keys({name: "x"}) == [name]


@pytest.mark.parametrize(
    "name",
    [
        # Benign names that should pass through
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "TMPDIR",
        # Allowlist
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        # Things that LOOK like prefixes but aren't credential-y in the strict sense
        "TOKENIZER_LIBRARY",  # ^TOKEN matches; but TOKEN should match — do not weaken the pattern
    ],
)
def test_credential_pattern_doesnt_match_benign(name: str) -> None:
    """Benign env vars must NOT be flagged."""
    # NB: TOKENIZER_LIBRARY does match ^TOKEN[_A-Z0-9]?, but library
    # name isn't a secret. We accept this as a false-positive over the
    # false-negative of letting BEARER_TOKEN through. Allowlist it
    # explicitly if it ever causes real grief.
    if name == "TOKENIZER_LIBRARY":
        # Document the known false-positive — test asserts it WOULD trip
        # the new pattern; future commit can decide to allowlist.
        assert detect_credential_env_keys({name: "x"}) == [name]
        return
    assert detect_credential_env_keys({name: "x"}) == []
