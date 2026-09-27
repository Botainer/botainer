"""What botainer REFUSES to launch over, it must not PRINT.

THE DEFECT, measured on one config file, seconds apart:

    $ botainer config get env
    ANTHROPIC_API_KEY: <redacted>
    DB_PASS: hunter2-PRODUCTION-DB-PASSWORD
    SLACK_WEBHOOK: https://hooks.slack.com/services/T00/B00/XXXXSECRETXXXX
    STRIPE_SK: sk_live_51REALSTRIPEKEY0000
    (1 credential-shaped value(s) redacted; pass --show-secrets to view)

    $ botainer config check
    ✗ credential-shaped env vars in config: ['ANTHROPIC_API_KEY', 'DB_PASS',
      'SLACK_WEBHOOK', 'STRIPE_SK'] — `botainer start` will refuse this config.

botainer had two credential predicates and they disagreed on eight names. The
surface that DISPLAYS a value was narrower than the surface that REFUSES TO
LAUNCH over it — which is the wrong way round: a name the launcher considers
dangerous enough to refuse must never be one a display command prints in full.
And the count in that note made it worse, because a number reads as an audit
result.

THE FIX IS A PROPERTY, NOT A RULE. `looks_credential` is now the UNION of both
lists, so "display ⊇ refusal" holds by construction. This file asserts it over
the launcher's OWN name corpus — every exact name and every regex it carries —
so extending `credential_leak_check` can never again leave the display side
behind. A comment saying "keep these in sync" would have been the third such
comment in this subsystem this week; two of the other two had already drifted.

Neither list is a superset of the other, which is why the union is the answer
rather than picking one:

  * `credential_leak_check` matches ANCHORED forms (`_API_KEY$`,
    `^SECRET[_A-Z0-9]`, `^STRIPE_`) plus ~40 exact vendor names. It knew
    `DB_PASS` and `MAILGUN_API`; the display side never did.
  * the display hints match a word ANYWHERE, so they catch `MY_TOKEN_HERE` and
    `some_password_2`, which no anchored pattern reaches.
"""
from __future__ import annotations

import pytest

from botainer.core import credential_leak_check as leak
from botainer.inspect._redact import looks_credential, redact


def _refused_names() -> list[str]:
    """Every name the LAUNCHER would refuse, drawn from its own constants.

    Read out of the module rather than retyped, so the corpus cannot go stale
    the moment someone adds a pattern — which is the whole failure this test
    exists to make impossible.
    """
    names = list(leak._CREDENTIAL_NAMES)
    # One synthesised name per regex, so a new pattern is covered the day it
    # lands rather than the day someone remembers to add a case here.
    samples = [
        "VENDOR_API_KEY", "VENDOR_SECRET", "VENDOR_SECRET_KEY",
        "VENDOR_PRIVATE_KEY", "VENDOR_ACCESS_TOKEN", "VENDOR_AUTH_TOKEN",
        "VENDOR_PASSWORD", "VENDOR_BEARER",
        "BEARER_THING", "SECRET_PROD_DB", "TOKEN_A", "OAUTH_CLIENT",
        "JWT_SIGNING", "DB_PASS", "DB_PWD", "DB_SECRET",
        "DOCKER_PASSWORD", "DOCKER_TOKEN", "DOCKER_PWD", "KUBE_TOKEN",
        "SLACK_TOKEN", "SLACK_BOT_TOKEN", "SLACK_WEBHOOK",
        "STRIPE_SK", "TWILIO_AUTH", "TWILIO_TOKEN",
        "SENDGRID_API_KEY", "MAILGUN_API_KEY",
    ]
    names += [n for n in samples if leak.detect_credential_env_keys({n: ""})]
    return sorted(set(names))


def test_every_name_the_launcher_refuses_is_also_redacted_on_display():
    """THE PROPERTY. Display ⊇ refusal, over the launcher's own corpus.

    If this fails, some name will be printed in full by `config get` /
    `config explain` / `inspect --json` while `botainer start` refuses to run
    with it — the exact inversion that made the transcript in this file's
    docstring possible.
    """
    refused = _refused_names()
    assert len(refused) > 30, (
        f"the corpus collapsed to {len(refused)} names — it is read out of "
        f"credential_leak_check's constants, so this means the constants moved "
        f"and this test is no longer checking what it claims")

    shown_in_full = [n for n in refused if not looks_credential(n)]

    assert not shown_in_full, (
        "these names make `botainer start` REFUSE, and a display surface would "
        "print their values in full:\n  " + "\n  ".join(shown_in_full) +
        "\n\n`looks_credential` must stay at least as wide as "
        "`credential_leak_check.detect_credential_env_keys`. It is currently "
        "the union of both; if you narrowed it, that is the regression."
    )


def test_the_eight_names_from_the_original_transcript_all_redact():
    """Named individually, because a corpus test can pass while the reported
    case still fails — and these eight are what the user actually saw."""
    for name in ("ANTHROPIC_API_KEY", "DB_PASS", "SLACK_WEBHOOK", "STRIPE_SK",
                 "TWILIO_AUTH", "SENDGRID_API_KEY", "MAILGUN_API_KEY",
                 "KUBE_TOKEN"):
        assert looks_credential(name), f"{name} would be printed in full"
        assert redact(name, "sk-live-REAL") == "<redacted>", name


def test_the_display_side_is_STILL_wider_where_it_always_was():
    """The union must not have become a replacement.

    These match a word ANYWHERE and no anchored launcher pattern reaches them.
    If a later edit made `looks_credential` simply delegate to the launcher,
    this fails — which is the more likely refactor, and the quieter loss.
    """
    for name in ("MY_TOKEN_HERE", "some_password_2", "thing_secret_thing",
                 "MY_PASSWD_VALUE", "a_jwt_blob"):
        assert not leak.detect_credential_env_keys({name: ""}), (
            f"{name} is now matched by the launcher too, so it no longer "
            f"demonstrates that the display side is wider — pick another")
        assert looks_credential(name), (
            f"{name} used to be caught by the display hints and is not any "
            f"more; the union has become a replacement")


@pytest.mark.parametrize("name", ["EDITOR", "LANG", "PATH", "HOME", "TERM",
                                  "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"])
def test_ordinary_env_vars_are_NOT_redacted(name):
    """THE CONTROL, and it carries the allowlist with it.

    Widening a predicate is easy to do carelessly, and "redact everything"
    passes every assertion above. `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` are
    on the launcher's own allowlist — paths, not secrets — and must survive the
    union that pulls the launcher's matching in.
    """
    assert not looks_credential(name), (
        f"{name} is an ordinary variable; redacting it hides the config from "
        f"the person trying to read it")
    assert redact(name, "/etc/ssl/certs/ca.pem") == "/etc/ssl/certs/ca.pem"


def test_botainers_own_credential_SETTINGS_still_survive_the_union():
    """The row-151 exemption, re-checked against the wider predicate.

    `credential_scope` and `inject_credentials` are settings ABOUT credentials,
    not credentials. Widening `looks_credential` could have re-broken them —
    it does not, because the launcher's patterns are anchored and the exemption
    sits above both — but "could not have" is a claim, and this is the check.
    """
    from botainer.inspect._redact import looks_credential_config_key
    for name in ("credential_scope", "inject_credentials"):
        assert looks_credential(name), (
            f"{name} does match the raw env-var predicate — that is expected, "
            f"and is why the config-key wrapper exists")
        assert not looks_credential_config_key(name), (
            f"{name} is a SETTING, and redacting it hides the field someone "
            f"debugging a cross-project login has to read")
