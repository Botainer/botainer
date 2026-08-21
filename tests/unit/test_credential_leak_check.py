"""Tests for credential-leak detection at compose time."""

from __future__ import annotations

import pytest

from botainer.core import credential_leak_check as clc
from botainer.core.refusal import Refused

# ────────── detect_credential_env_keys ──────────


def test_exact_match_anthropic_api_key() -> None:
    keys = clc.detect_credential_env_keys({"ANTHROPIC_API_KEY": "sk-..."})
    assert keys == ["ANTHROPIC_API_KEY"]


def test_exact_match_openai_api_key() -> None:
    keys = clc.detect_credential_env_keys({"OPENAI_API_KEY": "sk-..."})
    assert "OPENAI_API_KEY" in keys


def test_exact_match_aws_keys() -> None:
    env = {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_SESSION_TOKEN": "...",
    }
    keys = clc.detect_credential_env_keys(env)
    assert sorted(keys) == [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ]


def test_pattern_match_anything_with_api_key_suffix() -> None:
    keys = clc.detect_credential_env_keys({"MY_INTERNAL_API_KEY": "..."})
    assert "MY_INTERNAL_API_KEY" in keys


def test_pattern_match_secret_suffix() -> None:
    keys = clc.detect_credential_env_keys({"FOO_SECRET": "..."})
    assert "FOO_SECRET" in keys


def test_pattern_match_password_suffix() -> None:
    keys = clc.detect_credential_env_keys({"DB_PASSWORD": "..."})
    assert "DB_PASSWORD" in keys


def test_pattern_match_access_token_suffix() -> None:
    keys = clc.detect_credential_env_keys({"GITHUB_ACCESS_TOKEN": "..."})
    assert "GITHUB_ACCESS_TOKEN" in keys


def test_allowlist_path_vars_not_flagged() -> None:
    """REQUESTS_CA_BUNDLE / SSL_CERT_FILE are paths, not secrets."""
    keys = clc.detect_credential_env_keys({
        "REQUESTS_CA_BUNDLE": "/etc/ssl/ca-bundle.pem",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    })
    assert keys == []


def test_safe_env_vars_not_flagged() -> None:
    keys = clc.detect_credential_env_keys({
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "PYTHONPATH": "/packages/pip",
        "MODEL_NAME": "claude-3-5-sonnet",
        "DEBUG": "1",
    })
    assert keys == []


def test_huggingface_token_caught() -> None:
    keys = clc.detect_credential_env_keys({"HF_TOKEN": "..."})
    assert "HF_TOKEN" in keys


def test_case_insensitive_pattern_match() -> None:
    """The patterns are case-insensitive; lowercase env names get caught."""
    keys = clc.detect_credential_env_keys({"my_api_key": "..."})
    assert "my_api_key" in keys


# ────────── check_env_for_leaks ──────────


def test_check_env_no_leaks_silent() -> None:
    """Empty env or safe env: no exception."""
    clc.check_env_for_leaks({}, source="test")
    clc.check_env_for_leaks({"DEBUG": "1", "USER": "agent"}, source="test")


def test_check_env_one_leak_refuses() -> None:
    with pytest.raises(Refused, match="credential-shaped"):
        clc.check_env_for_leaks(
            {"ANTHROPIC_API_KEY": "sk-..."},
            source=".botainer/config.yaml `env:`",
        )


def test_check_env_refusal_mentions_proxy_plugin() -> None:
    """Refusal message points the user at the right remediation."""
    try:
        clc.check_env_for_leaks(
            {"ANTHROPIC_API_KEY": "sk-..."},
            source=".botainer/config.yaml",
        )
        raise AssertionError("expected Refused")
    except Refused as exc:
        msg = str(exc)
        assert "agent-claude-proxy" in msg
        assert "login" in msg


def test_check_env_refusal_lists_all_matches() -> None:
    try:
        clc.check_env_for_leaks(
            {
                "ANTHROPIC_API_KEY": "...",
                "AWS_SECRET_ACCESS_KEY": "...",
                "PATH": "/usr/bin",
            },
            source="test",
        )
        raise AssertionError("expected Refused")
    except Refused as exc:
        msg = str(exc)
        assert "ANTHROPIC_API_KEY" in msg
        assert "AWS_SECRET_ACCESS_KEY" in msg


# ────────── integration: compose refuses on credential leak ──────────


def test_compose_refuses_credential_env_in_config(tmp_path, monkeypatch) -> None:
    """End-to-end: a credential in .botainer/config.yaml's env: causes
    `compose_session` to refuse."""
    from botainer.core import composition, identity
    from botainer.core import config as config_module

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    # Inject a credential into the project config (post-init).
    cfg_path = proj / ".botainer" / "config.yaml"
    text = cfg_path.read_text()
    text = text.replace("env: {}", 'env:\n  ANTHROPIC_API_KEY: leak-me')
    cfg_path.write_text(text)
    # Make sure tests can find an image regardless.
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)

    with pytest.raises(Refused, match="credential-shaped"):
        composition.compose_session(
            proj, runtime_choice="mock", identity_accept=False
        )


# ── broker sentinel exception (#T0-3 broker) ──


def test_broker_sentinel_value_is_not_a_leak() -> None:
    """A credential-shaped NAME holding a provably-fake broker SENTINEL value
    carries no secret — the broker puts it in the container instead of a real
    token. It must pass the leak guard (which is why the old proxy was refused)."""
    from botainer.core.broker_sentinel import make_sentinel
    sent = make_sentinel("t-abc", "deadbeef")
    clc.check_env_for_leaks({"ANTHROPIC_AUTH_TOKEN": sent}, source="broker test")  # no raise
    clc.check_env_for_leaks({"ANTHROPIC_API_KEY": sent}, source="broker test")     # no raise


def test_broker_exception_does_not_weaken_the_guard() -> None:
    """A REAL-looking value under the same name is STILL refused — the exception
    only spares the unmistakable sentinel."""
    with pytest.raises(Refused):
        clc.check_env_for_leaks({"ANTHROPIC_AUTH_TOKEN": "sk-ant-realtokenvalue123"},
                                source="broker test")
    with pytest.raises(Refused):
        clc.check_env_for_leaks({"ANTHROPIC_AUTH_TOKEN": "not-a-sentinel-just-text"},
                                source="broker test")
