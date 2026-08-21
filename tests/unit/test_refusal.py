"""Tests for the refusal category enum + Refused exception."""

from __future__ import annotations

from botainer.core.refusal import RefusalCategory, Refused


def test_all_categories_have_kebab_case_values() -> None:
    for cat in RefusalCategory:
        assert "-" in cat.value or cat.value.islower(), cat.value
        assert cat.value == cat.value.lower()
        assert " " not in cat.value


def test_refused_carries_category() -> None:
    try:
        raise Refused(RefusalCategory.CONFIG_INVALID, "boom")
    except Refused as exc:
        assert exc.category == RefusalCategory.CONFIG_INVALID
        assert "boom" in str(exc)
        assert "config-invalid" in str(exc)


def test_refusal_categories_have_raise_sites() -> None:
    """Task #273: every declared category must have at least one
    raise-Refused site OR be on an explicit deprecation allowlist.

    Was: len >= 30. That passed even after deleting all 17 dead
    categories — the lower bound was the only floor. Now: walk every
    declared category and grep production code (botainer/) for a
    'RefusalCategory.<NAME>' reference outside the enum file itself.
    """
    import re
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    code_root = repo_root / "botainer"
    # Categories that are intentionally reserved for future-impl features
    # and have no raise site yet. Adding a name here is a conscious
    # decision; removing one is a TODO to wire it. Task #236 tracks
    # wiring these into real raise sites.
    _DEPRECATED_OR_FUTURE: set[str] = {
        "CROSS_PLUGIN_MOUNT_CONFLICT",     # #95 sidecar wiring pending
        "HOST_HELPER_REQUIRES_CONSENT",    # host-helper consent UI pending
        "HOST_PORT_DENIED",                # port policy pending
        "IDENTITY_AMBIGUOUS",              # #182 has IDENTITY_CHANGE_REFUSED; keep slot
        "PLUGIN_DEPENDENCY_UNRESOLVED",    # depends_on enforcement pending
        "PLUGIN_HOST_HOOKS_REQUIRE_CONSENT",  # host-hook consent UI pending
        "PLUGIN_IMAGE_DIGEST_MISMATCH",    # #143 image-digest verification pending
        "POLICY_MISSING",                  # policy loader returns defaults silently
        "PREFLIGHT_FAILED",                # #115 host preflight not wired
        "PROVENANCE_MISMATCH",             # used inline; keep reserved
        "SELFTEST_FAILED",                 # #201 self-test runner pending
        "TAMPER_DETECTED",                 # #74 TOFU enforcement pending
    }
    code_text = ""
    for py in code_root.rglob("*.py"):
        if py.name == "refusal.py":
            continue  # don't count the definition itself
        code_text += py.read_text(encoding="utf-8", errors="ignore")
    dead: list[str] = []
    for cat in RefusalCategory:
        if cat.name in _DEPRECATED_OR_FUTURE:
            continue
        # Look for RefusalCategory.<NAME> or just .NAME with the enum's
        # namespace already imported.
        pattern = re.compile(rf"RefusalCategory\.{re.escape(cat.name)}\b")
        if not pattern.search(code_text):
            dead.append(cat.name)
    assert not dead, (
        f"{len(dead)} RefusalCategory entries have NO raise site in "
        f"production code: {sorted(dead)}. Either wire each (add a "
        f"raise Refused(...) somewhere) OR add to _DEPRECATED_OR_FUTURE."
    )
