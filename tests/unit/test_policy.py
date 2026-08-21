"""Tests for site/user policy loading + intersection."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.policy import (
    CapabilitiesPolicy,
    NetworkPolicy,
    PluginsPolicy,
    SitePolicy,
    intersect,
    load_user_policy,
)
from botainer.core.refusal import RefusalCategory, Refused


def test_load_user_policy_returns_defaults_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    p = load_user_policy()
    assert p.plugins.allowed_tiers == ["first-party"]
    assert "PYTHONPATH" in p.capabilities.env_var_denylist


def test_intersection_idempotent() -> None:
    a = SitePolicy(plugins=PluginsPolicy(allowed_tiers=["first-party", "community-verified"]))
    assert intersect(a, a).plugins.allowed_tiers == ["first-party", "community-verified"]


def test_intersection_narrows_lists() -> None:
    a = SitePolicy(plugins=PluginsPolicy(allowed_tiers=["first-party", "community-verified"]))
    b = SitePolicy(plugins=PluginsPolicy(allowed_tiers=["first-party"]))
    out = intersect(a, b)
    assert out.plugins.allowed_tiers == ["first-party"]


def test_intersection_preserves_all_job_ceiling_caps() -> None:
    # sharp-edges F1 (HIGH): intersect dropped max_nodes/cpus/mem/time
    # per-job, so the site ceiling for those dims was silently NOT enforced. The
    # dispatcher always runs through intersect(), so a default user policy must
    # NOT widen the site caps.
    from botainer.core.policy import JobPolicy

    site = SitePolicy(jobs=JobPolicy(
        max_gpus_per_job=2, max_concurrent_cap=4, max_nodes_per_job=2,
        max_cpus_per_job=16, max_mem_mb_per_job=65536, max_time_seconds_per_job=3600))
    out = intersect(site, SitePolicy())  # default user policy
    assert out.jobs.max_nodes_per_job == 2
    assert out.jobs.max_cpus_per_job == 16
    assert out.jobs.max_mem_mb_per_job == 65536
    assert out.jobs.max_time_seconds_per_job == 3600
    assert out.jobs.max_gpus_per_job == 2 and out.jobs.max_concurrent_cap == 4


def test_intersection_env_denylist_is_union() -> None:
    a = SitePolicy(capabilities=CapabilitiesPolicy(env_var_denylist=["LD_PRELOAD"]))
    b = SitePolicy(capabilities=CapabilitiesPolicy(env_var_denylist=["PYTHONPATH"]))
    out = intersect(a, b)
    # Union (more restrictive)
    assert set(out.capabilities.env_var_denylist) == {"LD_PRELOAD", "PYTHONPATH"}


def test_intersection_declarative_requirement_is_most_restrictive() -> None:
    """AUDIT (MEDIUM): third_party_must_be_declarative is a
    REQUIREMENT the site imposes (True = more restrictive), so it must combine
    most-restrictively (OR) — a user policy setting it False must NOT disable
    the site's True (that was a ceiling bypass: it neutered the hooked-plugin
    consent gate). Previously this combined with AND (False stuck)."""
    a = SitePolicy(plugins=PluginsPolicy(third_party_must_be_declarative=True))
    b = SitePolicy(plugins=PluginsPolicy(third_party_must_be_declarative=False))
    assert intersect(a, b).plugins.third_party_must_be_declarative is True
    assert intersect(b, a).plugins.third_party_must_be_declarative is True  # order-independent
    # Both False → stays False (no requirement imposed at any level).
    c = SitePolicy(plugins=PluginsPolicy(third_party_must_be_declarative=False))
    assert intersect(b, c).plugins.third_party_must_be_declarative is False


def test_load_site_policy_honors_root_owned_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUDIT (H4, HPC parity): under apptainer --containall the
    host /etc/botainer/policy.yaml is invisible, so the host_helper binds the
    site policy in (root-owned, bind preserves uid) and points
    BOTAINER_SITE_POLICY at it. load_site_policy consults that override FIRST
    when it is ROOT-OWNED, so the admin ceiling binds on the compute node.
    (Ownership is monkeypatched True here — tests can't create root files.)"""
    from botainer.core import policy as policy_module
    site = tmp_path / "site-policy.yaml"
    site.write_text("network:\n  default_mode: none\n")
    monkeypatch.setenv("BOTAINER_SITE_POLICY", str(site))
    monkeypatch.setattr(policy_module, "_is_root_owned", lambda p: True)
    assert policy_module.load_site_policy().network.default_mode == "none"


def test_load_site_policy_ignores_non_root_owned_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SECURITY (H4): os.environ is user-controlled on the direct/docker path.
    A BOTAINER_SITE_POLICY pointing at a USER-owned file (not root) must be
    IGNORED — otherwise a user could point it at their own permissive policy
    and bypass the admin ceiling. The real _is_root_owned check (the tmp file
    is owned by the test user, not uid 0) rejects it, falling through to
    defaults (internet ceiling)."""
    from botainer.core import policy as policy_module
    evil = tmp_path / "evil.yaml"
    # Distinctive value that is NEITHER the internet default NOR commonly set,
    # so if it were (wrongly) loaded the result would be detectable.
    evil.write_text("network:\n  default_mode: endpoint-ip-allowlist\n")  # user-owned
    monkeypatch.setenv("BOTAINER_SITE_POLICY", str(evil))
    # Remove the real /etc path so fall-through is deterministically the default.
    monkeypatch.setattr(policy_module, "SITE_POLICY_PATHS", [])
    # No monkeypatch of _is_root_owned → real check → user-owned → IGNORED.
    pol = policy_module.load_site_policy()
    assert policy_module._is_root_owned(evil) is False
    assert pol.network.default_mode == "internet", (
        "user-owned BOTAINER_SITE_POLICY override was loaded — a user could "
        "bypass the admin ceiling. It must be ignored (not root-owned)."
    )


def test_load_site_policy_env_override_missing_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BOTAINER_SITE_POLICY pointing at a non-existent file falls back to
    the hardcoded path / defaults (no crash)."""
    from botainer.core.policy import load_site_policy
    monkeypatch.setenv("BOTAINER_SITE_POLICY", "/no/such/site-policy.yaml")
    assert load_site_policy().network.default_mode in {
        "none", "endpoint-ip-allowlist", "internet",
    }


def test_default_network_ceiling_is_internet() -> None:
    """AUDIT (H4): default_mode is a CEILING (max the site allows);
    with no admin-deployed policy it must default to the most-permissive
    ("internet") so out-of-the-box sessions aren't refused once the ceiling is
    enforced. An admin LOWERS it to restrict. Pinned so a revert to a "none"
    default (which would refuse every default-config session) is caught."""
    assert SitePolicy().network.default_mode == "internet"
    assert intersect(SitePolicy(), SitePolicy()).network.default_mode == "internet"


def test_intersection_network_default_mode_picks_most_restrictive() -> None:
    """Per sharp-edges #7 fix: site policy is the ceiling; intersection
    of disagreeing network.default_mode values yields the most-restrictive,
    not a refusal."""
    a = SitePolicy(network=NetworkPolicy(default_mode="none"))
    b = SitePolicy(network=NetworkPolicy(default_mode="internet"))
    out = intersect(a, b)
    assert out.network.default_mode == "none"


def test_intersection_network_default_mode_three_levels() -> None:
    a = SitePolicy(network=NetworkPolicy(default_mode="internet"))
    b = SitePolicy(network=NetworkPolicy(default_mode="endpoint-ip-allowlist"))
    c = SitePolicy(network=NetworkPolicy(default_mode="internet"))
    out = intersect(a, b, c)
    assert out.network.default_mode == "endpoint-ip-allowlist"


def test_intersection_network_default_mode_unknown_refused() -> None:
    a = SitePolicy(network=NetworkPolicy(default_mode="bogus-mode"))
    b = SitePolicy(network=NetworkPolicy(default_mode="none"))
    with pytest.raises(Refused) as exc:
        intersect(a, b)
    assert exc.value.category == RefusalCategory.POLICY_INVALID


def test_load_user_policy_rejects_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state.mkdir()
    (state / "policy.yaml").write_text("- not a mapping")
    with pytest.raises(Refused) as exc:
        load_user_policy()
    assert exc.value.category == RefusalCategory.POLICY_INVALID
