"""TEST-QUALITY AUDIT (B1): the broker daemon's env scrub had no test.

The sibling scrub in the (retired) proxy hook was MUTATION-VERIFIED deletable:
replacing it with `dict(os.environ)` leaked AWS_SECRET_ACCESS_KEY and GITHUB_TOKEN
into a long-lived daemon while its test stayed green, because that test asserted
only `returncode == 0` and never looked at the child's environment.

The LIVE broker runs the same pattern and holds the real OAuth credential, so the
property is pinned here — as an assertion on the scrub's OUTPUT, not on the
presence of a string in the source (the B7 trap).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "plugins" / "agent-claude-broker" / "hooks" / "start_broker.py"


def _hook_module():
    spec = importlib.util.spec_from_file_location("start_broker_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Credential-shaped names a real developer shell plausibly carries.
_HOSTILE = {
    "ANTHROPIC_API_KEY": "sk-ant-real",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "AWS_SESSION_TOKEN": "aws-token",
    "GITHUB_TOKEN": "ghp_real",
    "OPENAI_API_KEY": "sk-openai",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "NPM_TOKEN": "npm-secret",
    "BOTAINER_PROXY_UPSTREAM": "https://evil.example",
    "LD_PRELOAD": "/tmp/evil.so",
}
_BENIGN = {"PATH": "/usr/bin", "HOME": "/home/u", "TZ": "UTC"}


def test_scrub_drops_every_credential_shaped_var() -> None:
    mod = _hook_module()
    got = mod.safe_inherited_env({**_BENIGN, **_HOSTILE})
    for name in _HOSTILE:
        assert name not in got, f"{name} reached the broker daemon's environment"


def test_scrub_keeps_exactly_the_allowlist_and_nothing_else() -> None:
    """Set-equality, so ADDING a passthrough fails this test rather than
    silently widening what the daemon inherits."""
    mod = _hook_module()
    got = mod.safe_inherited_env({**_BENIGN, **_HOSTILE})
    assert set(got) == set(_BENIGN) & mod.INHERITABLE_ENV_KEYS
    assert set(got) <= mod.INHERITABLE_ENV_KEYS


def test_allowlist_contains_no_credential_shaped_names() -> None:
    """The allowlist itself is the trust decision — keep it auditable."""
    mod = _hook_module()
    for key in mod.INHERITABLE_ENV_KEYS:
        upper = key.upper()
        assert not any(t in upper for t in ("KEY", "TOKEN", "SECRET", "PASSWORD",
                                            "CREDENTIAL", "AUTH")), key


@pytest.mark.parametrize("needed", ["PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV"])
def test_scrub_retains_compatible_runtime_context(needed: str) -> None:
    """These keys remain allowed; isolated Python ignores PYTHONPATH.

    This checks the environment contract, not import selection. Behavioral
    subprocess isolation is covered separately.
    """
    mod = _hook_module()
    assert mod.safe_inherited_env({needed: "value"}) == {needed: "value"}
