"""Tests for the stale-user-ceiling upgrade guard + site-vs-user provenance.

Reproduces the real bug: an old policy.yaml pinning network.default_mode=none is
carried forward by setup's add-missing-only merge, so the next `botainer start`
refuses — and the old refusal blamed "admin/site policy" even with no site
policy present. These pin the non-destructive detection + provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import policy as pol


def _write_user_policy(tmp: Path, network_mode: str) -> None:
    (tmp / "policy.yaml").write_text(
        f"version: policy-v1\nnetwork:\n  default_mode: {network_mode}\n",
        encoding="utf-8",
    )


@pytest.fixture
def user_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    # No site policy in the test env (SITE_POLICY_PATHS point at /etc; unset override).
    monkeypatch.delenv("BOTAINER_SITE_POLICY", raising=False)
    return tmp_path


def test_stale_none_ceiling_no_site_warns(user_state) -> None:
    _write_user_policy(user_state, "none")
    warn = pol.stale_restrictive_user_ceiling()
    assert warn is not None
    assert "network.default_mode='none'" in warn or "default_mode=none" in warn or "'none'" in warn
    assert "botainer policy set network.default_mode internet" in warn


def test_internet_ceiling_is_fine(user_state) -> None:
    _write_user_policy(user_state, "internet")
    assert pol.stale_restrictive_user_ceiling() is None


def test_site_policy_forcing_restriction_is_not_flagged(user_state, monkeypatch) -> None:
    """If a real (admin) SITE policy caps the network, the restrictive user
    ceiling is NOT a stale-user footgun — don't tell the user to raise it (they
    can't; it's the admin's constraint)."""
    _write_user_policy(user_state, "none")
    monkeypatch.setattr(pol, "site_policy_present", lambda: True)
    monkeypatch.setattr(
        pol, "load_site_policy",
        lambda: pol.SitePolicy(network=pol.NetworkPolicy(default_mode="none")),
    )
    assert pol.stale_restrictive_user_ceiling() is None


def test_site_policy_present_false_with_no_etc(user_state) -> None:
    # No /etc/botainer/policy.yaml and no root-owned override → not present.
    assert pol.site_policy_present() is False
