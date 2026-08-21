"""Tests for MountPlan validation (allowlist, denylist, conflicts, normalization)."""

from __future__ import annotations

import pytest

from botainer.core.policy import SitePolicy
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    Provenance,
)
from botainer.mount_plan.validation import (
    _normalize_path,
    validate,
)


def _basic_plan(extras: list[Bind] | None = None) -> MountPlan:
    base = Bind(
        source="/host/proj",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
    )
    # RW /workspace REQUIRES the .botainer mask (assert_mask_invariants) — mirror
    # what core_base always adds in production.
    mask = Bind(
        source="/state/anchor",
        target="/workspace/.botainer",
        mode=BindMode.NULL_BIND,
        provenance=Provenance.CORE,
        nested_under="/workspace",
    )
    return MountPlan(binds=tuple([base, mask] + (extras or [])))


def test_validate_refuses_rw_workspace_without_botainer_mask() -> None:
    # The structural fix (sharp-edges): a plan binding /workspace RW
    # with NO /workspace/.botainer mask must be REFUSED by the session validator —
    # the class of the config-exposure bug, now enforced not just "remembered".
    plan = MountPlan(binds=(Bind(source="/host/proj", target="/workspace",
                                 mode=BindMode.RW, provenance=Provenance.CORE),))
    with pytest.raises(Refused):
        validate(plan, policy=SitePolicy())


def test_validate_refuses_mask_ordered_before_rw_parent() -> None:
    # Ordering is load-bearing: a mask ordered BEFORE its RW parent is shadowed by
    # the parent (apptainer applies binds in argv order) → must be refused.
    mask = Bind(source="/state/anchor", target="/workspace/.botainer",
                mode=BindMode.NULL_BIND, provenance=Provenance.CORE,
                nested_under="/workspace")
    parent = Bind(source="/host/proj", target="/workspace", mode=BindMode.RW,
                  provenance=Provenance.CORE)
    with pytest.raises(Refused):
        validate(MountPlan(binds=(mask, parent)), policy=SitePolicy())


def test_validate_rejects_empty_plan() -> None:
    with pytest.raises(Refused) as exc:
        validate(MountPlan(), policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_CONFLICT


def test_validate_accepts_minimal_plan() -> None:
    validate(_basic_plan(), policy=SitePolicy())


def test_target_must_be_absolute() -> None:
    bad = Bind(source="/x", target="rel/path", mode=BindMode.RO, provenance=Provenance.USER)
    plan = MountPlan(binds=(bad,))
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


def test_target_with_dotdot_refused() -> None:
    bad = Bind(
        source="/x", target="/data/../etc/shadow", mode=BindMode.RO, provenance=Provenance.USER
    )
    plan = MountPlan(binds=(bad,))
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


def test_target_with_newline_refused() -> None:
    bad = Bind(source="/x", target="/data\nfoo", mode=BindMode.RO, provenance=Provenance.USER)
    plan = MountPlan(binds=(bad,))
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


@pytest.mark.parametrize("ctrl", ["\x1f", "\x01", "\x07", "\x1b", "\r"])
def test_target_with_c0_control_char_refused(ctrl: str) -> None:
    """Fable-5 review MEDIUM-4: ANY C0 control char (not just NUL/\\n/\\t) is
    refused. 0x1f matters specifically — it's the preflight probe's field
    separator; letting it through a bind target silently skips that bind's
    self-test probe. This makes checks.py build_probe_plan's docstring true."""
    bad = Bind(source="/x", target=f"/data{ctrl}evil", mode=BindMode.RO, provenance=Provenance.USER)
    plan = MountPlan(binds=(bad,))
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


def test_denylisted_target_refused() -> None:
    bad = Bind(
        source="/var/run/docker.sock",
        target="/var/run/docker.sock",
        mode=BindMode.RW,
        provenance=Provenance.USER,
    )
    plan = _basic_plan([bad])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_TARGET_DENIED


def test_double_slash_target_refused() -> None:
    """AUDIT (H3): PurePosixPath preserves a leading '//', so
    '//etc/passwd' previously survived normalization unchanged and slipped
    past the '/etc' denylist while docker/apptainer mount it as /etc. The
    normalizer now collapses redundant slashes, so the path differs from its
    canonical form and the canonical-form guard refuses it (fail-closed)."""
    bad = Bind(
        source="/x", target="//etc/passwd", mode=BindMode.RO, provenance=Provenance.USER
    )
    plan = _basic_plan([bad])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


def test_double_slash_target_plugin_contribution_refused() -> None:
    """The critical case: PLUGIN binds SKIP the allowlist, so the denylist
    (and the canonical-form guard, which runs BEFORE the plugin branch) is
    their only backstop. A plugin contributing '//etc/passwd' must still be
    refused — the double-slash denylist bypass is closed for plugin binds."""
    bad = Bind(
        source="/etc/passwd",
        target="//etc/passwd",
        mode=BindMode.RW,
        provenance=Provenance.PLUGIN,
    )
    plan = _basic_plan([bad])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


def test_double_slash_source_refused() -> None:
    """Source side of H3: '//etc/shadow' is refused. (The source side had a
    partial pre-existing backstop — `_source_is_denied` realpath-resolves
    '//etc/shadow' → '/etc/shadow', already denied — so pre-fix it failed with
    MOUNT_SOURCE_DENIED rather than leaking; the true unguarded bypass was the
    TARGET side. Post-fix the canonical-form guard refuses it first.)"""
    bad = Bind(
        source="//etc/shadow", target="/data/x", mode=BindMode.RO, provenance=Provenance.USER
    )
    plan = _basic_plan([bad])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_PATH_NOT_NORMALIZED


def test_normalize_collapses_redundant_slashes() -> None:
    """Unit-level: the normalizer collapses '//'/'///' runs; legit single-slash
    paths are unchanged (so the canonical-form guard does not false-reject)."""
    assert _normalize_path("//etc/passwd", label="t") == "/etc/passwd"
    assert _normalize_path("///a//b", label="t") == "/a/b"
    assert _normalize_path("/", label="t") == "/"
    assert _normalize_path("/workspace/.botainer/x", label="t") == "/workspace/.botainer/x"


def test_target_not_in_allowlist_refused() -> None:
    bad = Bind(source="/x", target="/elsewhere", mode=BindMode.RO, provenance=Provenance.USER)
    plan = _basic_plan([bad])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_TARGET_OFF_ALLOWLIST


def test_target_in_allowlist_accepted() -> None:
    ok = Bind(source="/host/data", target="/data", mode=BindMode.RO, provenance=Provenance.USER)
    validate(_basic_plan([ok]), policy=SitePolicy())


def test_denylisted_source_refused() -> None:
    bad = Bind(
        source="/etc/shadow", target="/data/shadow", mode=BindMode.RO, provenance=Provenance.USER
    )
    plan = _basic_plan([bad])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_SOURCE_DENIED


# ── umbrella-bind defense (audit, #54): binding a PARENT of a denied
#    path must be refused (source=/ smuggles /etc/shadow as a child; source=$HOME
#    smuggles ~/.ssh). Default policy: extra_targets_allowlist includes /mnt,/data;
#    trusted_source_roots is empty. ─────────────────────────────────────────────


@pytest.mark.parametrize("umbrella", ["/", "/var", "/var/run"])
def test_umbrella_source_over_denylisted_refused(umbrella: str) -> None:
    """`source=/` (parent of /etc, /root) and `source=/var` (parent of
    docker.sock) are refused — the old denylist only matched the exact path or
    a descendant, so an umbrella parent slipped through."""
    bad = Bind(
        source=umbrella, target="/mnt", mode=BindMode.RW, provenance=Provenance.USER
    )
    with pytest.raises(Refused) as exc:
        validate(_basic_plan([bad]), policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_SOURCE_DENIED


def test_home_directory_itself_as_source_refused() -> None:
    """Binding the home dir ITSELF (no trailing slash) exposed ~/.ssh, ~/.aws
    rw — the old guard only matched `src.startswith(home + '/')`, missing the
    home dir itself. Now refused (parent of the sensitive subpaths)."""
    import os

    home = os.path.expanduser("~")
    bad = Bind(source=home, target="/data", mode=BindMode.RW, provenance=Provenance.USER)
    with pytest.raises(Refused) as exc:
        validate(_basic_plan([bad]), policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_SOURCE_DENIED


def test_legit_non_sensitive_sources_still_accepted() -> None:
    """The fix must NOT break binding ordinary cluster data dirs — those are the
    whole point of mounts.extra and are neither sensitive nor umbrellas."""
    for src, tgt in (("/host/data", "/data"), ("/scratch/gpfs/proj", "/scratch"),
                     ("/var/lib/mydata", "/mnt")):
        validate(
            _basic_plan([Bind(source=src, target=tgt, mode=BindMode.RW,
                              provenance=Provenance.USER)]),
            policy=SitePolicy(),
        )


def test_trusted_source_root_reallows_home() -> None:
    """An admin can still explicitly vouch for a source via the root-owned
    policy's trusted_source_roots (the escape hatch is preserved)."""
    import os

    from botainer.core.policy import MountsPolicy

    home = os.path.expanduser("~")
    ok = Bind(source=home, target="/data", mode=BindMode.RW, provenance=Provenance.USER)
    validate(
        _basic_plan([ok]),
        policy=SitePolicy(mounts=MountsPolicy(trusted_source_roots=[home])),
    )


def test_duplicate_target_refused() -> None:
    a = Bind(source="/x", target="/data/foo", mode=BindMode.RO, provenance=Provenance.USER)
    b = Bind(source="/y", target="/data/foo", mode=BindMode.RW, provenance=Provenance.USER)
    plan = _basic_plan([a, b])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_CONFLICT


def test_null_bind_plus_nested_accepted() -> None:
    ws = Bind(source="/host/proj", target="/workspace", mode=BindMode.RW,
              provenance=Provenance.CORE)
    null = Bind(
        source="/host/anchor",
        target="/workspace/.botainer",
        mode=BindMode.NULL_BIND,
        provenance=Provenance.CORE,
    )
    nested = Bind(
        source="/host/aas.txt",
        target="/workspace/.botainer/AGENT_ACCESS.txt",
        mode=BindMode.RO,
        provenance=Provenance.CORE,
        nested_under="/workspace/.botainer",
    )
    # Build directly (not via _basic_plan, which adds its own .botainer mask).
    validate(MountPlan(binds=(ws, null, nested)), policy=SitePolicy())


def test_nesting_allowed_when_consistent() -> None:
    """Container runtimes legitimately layer mounts; nesting is fine when
    targets are not duplicates."""
    a = Bind(source="/x", target="/data/foo", mode=BindMode.RW, provenance=Provenance.USER)
    b = Bind(source="/y", target="/data/foo/inner", mode=BindMode.RW, provenance=Provenance.USER)
    validate(_basic_plan([a, b]), policy=SitePolicy())


def test_bogus_nested_under_refused() -> None:
    a = Bind(
        source="/x",
        target="/data/foo",
        mode=BindMode.RO,
        provenance=Provenance.USER,
        nested_under="/does/not/exist",
    )
    plan = _basic_plan([a])
    with pytest.raises(Refused) as exc:
        validate(plan, policy=SitePolicy())
    assert exc.value.category == RefusalCategory.MOUNT_CONFLICT


def test_normalize_rejects_nul() -> None:
    with pytest.raises(Refused):
        _normalize_path("/foo\x00bar", label="x")
