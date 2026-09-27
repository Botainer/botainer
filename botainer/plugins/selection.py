"""Which plugins may be enabled TOGETHER — one implementation, several callers.

This logic lived inline in `compose_session`, which meant it ran at LAUNCH and
nowhere else. `botainer plugin enable` would happily add a second same-family
agent plugin, `botainer config check` would call the result "no issues found",
and the user learned the truth from a refusal when they tried to start:

    $ botainer plugin enable agent-claude-broker
    Enabled 'agent-claude-broker' for this project.
    $ botainer config check
    ✓ no issues found
    $ botainer start
    refused: plugins 'agent-claude-broker' and 'agent-claude' are mutually
    exclusive. Pick one with `botainer auth use <mode>`.

Observed by running, in that order. The enforcement was never missing — it was
only reachable at the last possible moment.

Extracted rather than copied, deliberately. A second implementation of an
exclusion rule agrees on the day it is written and drifts silently afterwards;
that is the #218 defect (a host check carrying its own copy of a product
answer). `compose_session` now calls this, so the validator and the enforcer
cannot disagree — if they ever do, one of them stopped calling this function,
which is a visible change rather than a silent one.

NOT INCLUDED HERE: "the project has no agent plugin at all". That is a
LEGITIMATE state — `composition.py` has a branch for it, and a preflight with
zero agent plugins gets past every plugin check — so refusing it centrally
would break a real use case. It is handled where it belongs, at the point of
action in `plugin disable`, as a question the user can answer.
"""
from __future__ import annotations

from botainer.auth_modes import mode_list_hint
from botainer.core.refusal import RefusalCategory, Refused


def check_family_exclusion(enabled: list[str] | set[str]) -> None:
    """Raise `Refused` if `enabled` names two plugins of one auth family.

    `enabled` is the plugin-name list as it WOULD be — callers pass the
    prospective set, so `plugin enable` can refuse before writing rather than
    after.

    A manifest that fails to load REFUSES rather than being skipped: a silent
    skip would let a malformed manifest bypass the exclusion, which is the
    sharp-edges F8 finding this check exists for.
    """
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest

    enabled_set = set(enabled)
    family_to_enabled: dict[str, list[str]] = {}
    for inst in list_installed():
        if inst.name not in enabled_set:
            continue
        try:
            man = load_manifest(inst.plugin_dir)
        except Refused as exc:
            raise Refused(
                RefusalCategory.PLUGIN_MANIFEST_INVALID,
                f"enabled plugin {inst.name!r} has invalid manifest: {exc}. "
                f"Cannot proceed without manifest to check exclusion rules.",
            ) from exc
        if man.auth_family:
            family_to_enabled.setdefault(man.auth_family, []).append(inst.name)
        # Also honour mutually_exclusive_with (catches non-auth-family
        # exclusions or third-party-declared conflicts).
        for sibling in man.mutually_exclusive_with:
            if sibling in enabled_set and sibling != inst.name:
                hint = (
                    "Pick one with `botainer auth use <mode>`"
                    if man.auth_family
                    else "Disable one with `botainer plugin disable <name>`"
                )
                raise Refused(
                    RefusalCategory.PLUGIN_HOOK_FAILED,
                    f"plugins {inst.name!r} and {sibling!r} are mutually "
                    f"exclusive. {hint}.",
                )
    # Refuse if any family has >1 enabled member (catches author-oversight
    # cases where mutually_exclusive_with isn't symmetric).
    for fam, plugins in family_to_enabled.items():
        if len(plugins) > 1:
            raise Refused(
                RefusalCategory.PLUGIN_HOOK_FAILED,
                f"{len(plugins)} plugins enabled in auth_family={fam!r}: "
                f"{sorted(plugins)}. Exactly one per family allowed. "
                f"Use `botainer auth use <{mode_list_hint()}> "
                f"--family {fam}` to pick.",
            )


def agent_plugins_among(enabled: list[str] | set[str]) -> list[str]:
    """Those of `enabled` that are agent plugins (i.e. declare an auth family).

    Used by `plugin disable` to notice it is about to remove the last one. A
    manifest that will not load is simply not counted here — unlike the
    exclusion check above, guessing low only makes the warning MORE likely to
    fire, which is the safe direction for a prompt.
    """
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest

    enabled_set = set(enabled)
    out: list[str] = []
    for inst in list_installed():
        if inst.name not in enabled_set:
            continue
        try:
            man = load_manifest(inst.plugin_dir)
        except Refused:
            continue
        if man.auth_family:
            out.append(inst.name)
    return sorted(out)
