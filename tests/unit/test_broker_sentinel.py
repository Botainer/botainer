"""The sentinel format: one charset, owned by the constructor (#216).

WHAT WENT WRONG. Three places spelled the charset independently — the regex
here, and a `c.isalnum() or c in "_-"` filter copied into each broker hook.
`str.isalnum()` is Unicode-aware and `[A-Za-z0-9_-]` is not, so a tenant
containing `Ⅷ` (or an Arabic-Indic digit, or a full-width letter) produced a
value `make_sentinel` emitted and `is_sentinel` then refused to recognise.

That mismatch is not cosmetic, and it does NOT fail in one direction:

  * `broker/openai_credential.py`, `broker/openai_oauth.py` and
    `broker/credential_source.py` REFUSE to forward a value they recognise as a
    sentinel. Unrecognised → the guard fails OPEN, and a placeholder is sent
    upstream as though it were the real credential.
  * `core/credential_leak_check.py` ALLOWS a value it recognises as a sentinel.
    Unrecognised → fails CLOSED, and the launch is refused for "leaking"
    something that carries no secret at all.

Only reachable through a non-ASCII project uuid, which botainer never mints —
so this is a latent bug fixed as structure rather than an incident. The point of
these tests is the structural property, not the Unicode: after the fix there is
no input at all for which the constructor and the recogniser disagree.
"""
from __future__ import annotations

import secrets

import pytest

from botainer.core.broker_sentinel import (
    PREFIX,
    SUFFIX,
    is_sentinel,
    make_sentinel,
    sentinel_tenant,
)

NONCE = "deadbeef" * 4


# Anything a caller could plausibly hand us as a tenant, plus the shapes that
# broke the old filter. A tenant is a project uuid today; it is a LABEL, and
# nothing downstream should be able to make it structural.
HOSTILE_TENANTS = [
    pytest.param("proj-1234abcd", id="ordinary"),
    pytest.param("Ⅷ", id="roman-numeral-is-alnum-to-python"),
    pytest.param("٣٤٥", id="arabic-indic-digits"),
    pytest.param("ｆｕｌｌｗｉｄｔｈ", id="fullwidth-latin"),
    pytest.param("Ç∂é", id="accented"),
    pytest.param("a\nb", id="newline"),               # `(?!X).` would miss this
    pytest.param("a.nonce=x.", id="format-injection"),
    pytest.param("../../etc/passwd", id="path-traversal"),
    pytest.param("", id="empty"),
    pytest.param("x" * 500, id="very-long"),
    pytest.param("tab\there", id="tab"),
    pytest.param("sp ace", id="space"),
]


@pytest.mark.parametrize("tenant", HOSTILE_TENANTS)
def test_every_sentinel_we_build_is_one_we_recognise(tenant: str) -> None:
    """THE STRUCTURAL PROPERTY. Not "the charset filter works" — that is a rule,
    and a rule in three files drifts. This says the disagreement is
    unrepresentable: there is no tenant for which the two halves differ."""
    value = make_sentinel(tenant, NONCE)
    assert is_sentinel(value), f"built a sentinel we do not recognise: {value!r}"
    assert sentinel_tenant(value) is not None


@pytest.mark.parametrize("tenant", HOSTILE_TENANTS)
def test_the_tenant_can_never_alter_the_structure(tenant: str) -> None:
    """A tenant is a label. It must not be able to introduce a second
    `nonce=` field, break the value across lines, or run past the suffix."""
    value = make_sentinel(tenant, NONCE)
    assert value.count(".nonce=") == 1, value
    assert value.startswith(PREFIX + ".tenant="), value
    assert value.endswith("." + SUFFIX), value
    assert "\n" not in value and "\t" not in value and " " not in value, repr(value)
    assert f".nonce={NONCE}." in value


def test_the_nonce_is_validated_not_sanitised() -> None:
    """The asymmetry is deliberate and worth pinning, because the obvious
    symmetric implementation is dangerous. On the TCP transport the sentinel IS
    the daemon's required access token; silently stripping characters would
    hand the container a token the daemon rejects, and the session would fail
    authentication with nothing printed to say why."""
    with pytest.raises(ValueError, match="hex"):
        make_sentinel("t", "NOT-HEX-AT-ALL")
    with pytest.raises(ValueError):
        make_sentinel("t", "")
    with pytest.raises(ValueError):
        make_sentinel("t", "DEADBEEF")      # uppercase: the regex wants [0-9a-f]
    # The nonce that survives is byte-identical — never quietly rewritten.
    assert f"nonce={NONCE}." in make_sentinel("t", NONCE)


def test_a_real_looking_credential_is_not_mistaken_for_a_placeholder() -> None:
    """The leak guard ALLOWS values this returns True for, so a false positive
    here would let a real credential through it."""
    for value in [
        "sk-ant-api03-" + "x" * 40,
        "sk-proj-" + "y" * 40,
        "eyJhbGciOiJub25lIn0.eyJleHAiOjk5OTk5OTk5OTl9.",   # the removed JWT wrap
        PREFIX,
        SUFFIX,
        f"{PREFIX}.tenant=t.nonce={NONCE}",                # suffix missing
        f"tenant=t.nonce={NONCE}.{SUFFIX}",                # prefix missing
        f"{PREFIX}.tenant=t.nonce=zzzz.{SUFFIX}",          # nonce not hex
        f"{PREFIX}.tenant=.nonce={NONCE}.{SUFFIX}",        # empty tenant
        "",
    ]:
        assert not is_sentinel(value), f"accepted a non-sentinel: {value!r}"
        assert sentinel_tenant(value) is None


def test_nothing_may_be_appended_to_a_sentinel() -> None:
    """`is_sentinel` strips before matching, so anchoring on `$` — which in
    Python also matches before a trailing newline — is not enough on its own.
    A value that is a sentinel PLUS something else is not a sentinel."""
    good = make_sentinel("t", NONCE)
    assert is_sentinel(good) and is_sentinel(f"  {good}\n")   # whitespace only
    for tail in ["\nsk-ant-real", " sk-ant-real", "sk-ant-real", "\n\nx"]:
        assert not is_sentinel(good + tail), repr(tail)


def test_a_freshly_minted_nonce_is_accepted() -> None:
    """What the hooks actually pass. If secrets.token_hex ever stopped being
    lowercase hex, make_sentinel would raise inside a pre_session hook and
    every broker launch would fail — so assert the real call, not a literal."""
    assert is_sentinel(make_sentinel("proj", secrets.token_hex(16)))
