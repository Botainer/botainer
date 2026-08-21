"""MountPlan validation: allowlist, denylist, conflict, normalization.

The MountPlan is *closed*: any bind that violates the rules causes the launcher
to refuse with a typed category. No silent downgrade.

Validation rules:
- Path normalization: paths must be absolute and already canonical — any input
  with "..", a doubled/trailing slash, or a "." segment differs from its
  normalized form and is REFUSED (not silently collapsed) by the canonical-form
  guard. This is what stops a `//etc` denylist bypass (AUDIT H3).
- Target allowlist: targets must be inside a configured allowlist OR match an
  always-permitted pattern (e.g., `/workspace/.botainer/AGENT_ACCESS.txt`).
- Target denylist: certain target paths are refused outright
  (`/var/run/docker.sock`, `/etc/shadow`, etc.).
- Source denylist: source paths in protected host areas (`/etc`, `~/.ssh`)
  are refused unless explicitly permitted by site policy.
- Conflict: no two binds may target the same path; null-bind + nested binds
  are an explicit, narrow exception.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import PurePosixPath

from botainer.core.policy import SitePolicy
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import Bind, BindMode, MountPlan

# Targets that are always allowed (core-mounted; outside policy reach).
_CORE_ALWAYS_TARGETS: tuple[str, ...] = (
    "/workspace",
    "/workspace/.botainer",
    "/workspace/.botainer/AGENT_ACCESS.txt",
    "/.botainer",  # core also mounts at top-level for some adapters
    "/packages",   # Phase 2: per-project persistent package installs
    "/scratch",    # Phase 2: per-project ephemeral scratch
    "/home/user",  # writable HOME for the run-as uid (composition.home_bind).
    # EXACTLY /home/user — NOT a "/home" prefix, which would also allowlist
    # /home/agent (the credential dir) and defeat the child-job /home denylist.
    "/jobs/in",    # #54: job-dispatcher inbox (agent writes requests)
    "/jobs/out",   # #54: job-dispatcher outbox (agent reads results RO)
    "/usr/local/bin/botainer-job",  # #54: the in-container dispatch CLI (RO)
)


# Targets always refused (codex / ToB feedback).
DENYLISTED_TARGETS: tuple[str, ...] = (
    "/etc",
    "/etc/shadow",
    "/etc/passwd",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
)


# Source paths always refused unless on policy trusted_source_roots.
# Per sharp-edges #5: source denylist must mirror target denylist; otherwise
# `source=/etc, target=/mnt/x` exposes all of /etc to the container.
DENYLISTED_SOURCES: tuple[str, ...] = (
    "/etc",
    "/etc/shadow",
    "/etc/passwd",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/var/run/docker.sock",
    "/run/docker.sock",
)


# Sensitive user-home subpaths that require explicit policy allowance.
SENSITIVE_USER_HOME_SUBPATHS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
)


def _normalize_path(p: str, *, label: str) -> str:
    if not p:
        raise Refused(
            RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
            f"{label} path is empty",
        )
    if "\x00" in p:
        raise Refused(RefusalCategory.MOUNT_PATH_NOT_NORMALIZED, f"{label} path has NUL byte")
    # Reject ALL C0 control characters (0x01–0x1f), not just NUL/newline/tab.
    # No legitimate filesystem path contains one, and letting one through lets
    # a hostile target smuggle the preflight probe's 0x1f field separator
    # (botainer/preflight/checks.py build_probe_plan) or terminal/parse-
    # breaking bytes into downstream consumers. (Fable-5 review MEDIUM-4,
    #: the old check listed only \n and \t, so \x1f slipped past
    # and a probe for such a bind was silently skipped.)
    if any(ord(c) < 0x20 for c in p):
        raise Refused(
            RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
            f"{label} path contains a C0 control character: {p!r}",
        )
    if not p.startswith("/"):
        raise Refused(
            RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
            f"{label} path must be absolute: {p!r}",
        )
    if ".." in PurePosixPath(p).parts:
        raise Refused(
            RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
            f"{label} path contains '..': {p!r}",
        )
    # Reject path patterns Docker --mount escapes interpret specially.
    for tok in ("\n", "\t", ",", ":", '"'):
        if tok in p:
            raise Refused(
                RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
                f"{label} path contains forbidden character {tok!r}: {p!r}",
            )
    # AUDIT (H3): collapse redundant '/' runs. PurePosixPath
    # PRESERVES a leading '//' (POSIX implementation-defined), so '//etc'
    # would survive normalization unchanged and slip past the '/etc'
    # denylist (`_target_is_denied` startswith) while docker/apptainer/the
    # kernel treat '//etc' as '/etc' — a denylist BYPASS (the only backstop
    # for plugin binds, which skip the allowlist). After collapsing, a
    # non-canonical input differs from its normalized form and is refused
    # by the canonical-form guard in validate_mount_plan (fail-closed), and
    # the denylist also matches the collapsed form.
    collapsed = "/" + "/".join(seg for seg in p.split("/") if seg)
    return str(PurePosixPath(collapsed))


def _target_is_allowed(target: str, allowlist: Iterable[str]) -> bool:
    if target in _CORE_ALWAYS_TARGETS:
        return True
    for root in (*_CORE_ALWAYS_TARGETS, *allowlist):
        if target == root or target.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _target_is_denied(target: str) -> bool:
    # NOTE: callers MUST pass an already-normalized (slash-collapsed) path.
    # This prefix match relies on the canonical-form guard in `validate`
    # having rejected non-canonical inputs first; a new caller that runs this
    # on a raw path would reinherit the `//etc` bypass (AUDIT H3).
    for denied in DENYLISTED_TARGETS:
        if target == denied or target.startswith(denied.rstrip("/") + "/"):
            return True
    return False


def _path_is_related(candidate: str, protected: str) -> bool:
    """True if `candidate` equals `protected`, is INSIDE it (descendant), OR is
    a PARENT of it (ancestor).

    The ancestor case is the umbrella-bind defense (audit, #54;
    same class as DN-003): a denylist that only
    matched `candidate == protected` or `candidate` under `protected` let a
    caller bind a PARENT of a protected path — `source=/` (parent of `/etc`,
    `/root`) or `source=$HOME` (parent of `~/.ssh`) — and smuggle the protected
    child into the container even though the literal source string never matched
    the denylist. Matching in BOTH directions closes that: you can neither bind
    the sensitive path, nor bind an umbrella above it.
    """
    c = candidate.rstrip("/") or "/"
    p = protected.rstrip("/")
    if c == p:
        return True
    if c == "/":  # `/` is a parent of everything
        return True
    if c.startswith(p + "/"):  # candidate is INSIDE protected (descendant)
        return True
    if p.startswith(c + "/"):  # candidate is a PARENT of protected (ancestor)
        return True
    return False


def _source_is_denied(source: str, *, trusted_roots: Iterable[str]) -> bool:
    # Task #183: check BOTH the literal source AND the realpath. A
    # symlink at ~/.cache/safe → /etc/shadow would pass the literal
    # check (literal source isn't related to /etc) but the bind would
    # still mount /etc/shadow. Resolve realpath and check it too.
    sources_to_check = [source]
    try:
        real = os.path.realpath(source)
        if real and real != source:
            sources_to_check.append(real)
    except OSError:
        pass  # source may not exist on host (validation may still proceed)

    def _under_trusted(src: str) -> bool:
        for tr in trusted_roots:
            tr_resolved = os.path.expanduser(tr).rstrip("/")
            if tr_resolved and (
                src == tr_resolved or src.startswith(tr_resolved + "/")
            ):
                return True
        return False

    # HARD denylist — refused in EITHER direction (descendant or ancestor);
    # trusted_source_roots does NOT re-allow these (they are absolute host
    # secrets/devices). `source=/` or `source=/var` (parent of docker.sock) is
    # refused here, closing the umbrella bypass.
    for src in sources_to_check:
        for d in DENYLISTED_SOURCES:
            if _path_is_related(src, d):
                return True

    # Sensitive user-home subpaths (~/.ssh, ~/.aws, …): refused in EITHER
    # direction UNLESS the source resolves under an explicit trusted_source_root.
    # The ancestor direction closes `source=$HOME` / `source=/home` (parent of
    # ~/.ssh) — the old guard only matched `src.startswith(home + "/")`, so
    # binding the home dir ITSELF slipped past and exposed ~/.ssh rw.
    home = os.path.expanduser("~")
    if home:
        home_sensitive = [f"{home}/{sub}" for sub in SENSITIVE_USER_HOME_SUBPATHS]
        for src in sources_to_check:
            for hs in home_sensitive:
                if _path_is_related(src, hs) and not _under_trusted(src):
                    return True
    return False


# A RW bind of one of these parents is dangerous unless the listed child target
# is ALSO masked (NULL_BIND or RO) and ordered AFTER the parent (apptainer applies
# --bind in argv order, so a mask ordered BEFORE its RW parent is shadowed). This
# is the structural enforcement the `.botainer` mask was missing: it lived only in
# the session composition helper, so the JOB path silently omitted it and exposed
# config.yaml/project-id (sharp-edges, same class as the umbrella-bind
# postmortem — "principles without enforcement don't exist"). Applies to BOTH the
# session validator and the child-job composer.
_MASK_REQUIRED_UNDER_RW: dict[str, tuple[str, ...]] = {
    "/workspace": ("/workspace/.botainer",),
}


def assert_mask_invariants(binds: list[Bind]) -> None:
    """Fail-closed: any RW bind of a protected parent MUST be followed (in list
    order) by a non-passthrough mask (NULL_BIND or RO) of each required child,
    whose source is NOT inside the RW parent. Called by BOTH `validate()` (session)
    and `compose_child_job_argv` (child jobs) so neither pipeline can silently omit
    the mask."""
    for i, parent in enumerate(binds):
        req = _MASK_REQUIRED_UNDER_RW.get(parent.target.rstrip("/"))
        if not req or parent.mode != BindMode.RW:
            continue
        for child_target in req:
            ct = child_target.rstrip("/")
            masks = [(j, b) for j, b in enumerate(binds)
                     if b.target.rstrip("/") == ct
                     and b.mode in (BindMode.NULL_BIND, BindMode.RO)]
            if not masks:
                raise Refused(
                    RefusalCategory.MOUNT_PATH_NULL_BIND_VIOLATED,
                    f"{parent.target!r} is bound RW but {child_target!r} is not "
                    f"masked; a process could read/write host-sensitive state "
                    f"under it (e.g. config.yaml, project-id).")
            if not any(j > i for j, _ in masks):
                raise Refused(
                    RefusalCategory.MOUNT_PATH_NULL_BIND_VIOLATED,
                    f"mask for {child_target!r} is ordered before its RW parent "
                    f"{parent.target!r}; apptainer would shadow it.")
            psrc = parent.source.rstrip("/")
            for _, m in masks:
                msrc = m.source.rstrip("/")
                if msrc == psrc or msrc.startswith(psrc + "/"):
                    raise Refused(
                        RefusalCategory.MOUNT_PATH_NULL_BIND_VIOLATED,
                        f"mask for {child_target!r} sources from inside the RW "
                        f"parent ({m.source!r}); it must be an out-of-tree anchor.")


def _detect_conflicts(binds: list[Bind]) -> None:
    """Refuse if two binds target the same path AND neither is null-bind/nested."""
    by_target: dict[str, list[Bind]] = {}
    for b in binds:
        by_target.setdefault(b.target, []).append(b)
    for target, group in by_target.items():
        if len(group) <= 1:
            continue
        # Allow exactly one null-bind + N binds nested under it (the null-bind defense).
        nullbinds = [b for b in group if b.mode == BindMode.NULL_BIND]
        if len(nullbinds) > 1:
            raise Refused(
                RefusalCategory.MOUNT_CONFLICT,
                f"two null-binds at the same target {target!r}",
            )
        if not nullbinds:
            # Plain duplicate. Refuse.
            raise Refused(
                RefusalCategory.MOUNT_CONFLICT,
                f"multiple binds at the same target {target!r}",
            )

    # Cross-bind nesting note: container runtimes happily layer mounts on top
    # of each other. We do NOT refuse nesting on its own (the user's /workspace
    # bind is the parent of /workspace/.botainer is the parent of
    # /workspace/.botainer/AGENT_ACCESS.txt, all legitimate). We only validate
    # the `nested_under` annotation if it's set and mismatches reality.
    for b in binds:
        if not b.nested_under:
            continue
        parent_target = b.nested_under.rstrip("/")
        # The declared parent must exist in the plan.
        parents = [p for p in binds if p.target.rstrip("/") == parent_target]
        if not parents:
            raise Refused(
                RefusalCategory.MOUNT_CONFLICT,
                f"bind {b.target!r} declares nested_under {b.nested_under!r}, "
                f"but no bind has that target",
            )
        # The bind's target must actually be inside the declared parent.
        if not (b.target == parent_target or b.target.startswith(parent_target + "/")):
            raise Refused(
                RefusalCategory.MOUNT_CONFLICT,
                f"bind {b.target!r} declares nested_under {b.nested_under!r}, "
                f"but is not physically inside it",
            )


def validate(plan: MountPlan, *, policy: SitePolicy) -> None:
    """Validate a composed MountPlan against allowlist/denylist/conflicts.

    Raises `Refused` with a typed category on the first violation.
    """
    binds = list(plan.binds)
    if not binds:
        # Refuse empty plans (codex ToB: empty-list-refusals).
        raise Refused(RefusalCategory.MOUNT_CONFLICT, "MountPlan has no binds")

    allowlist = policy.mounts.extra_targets_allowlist
    trusted = policy.mounts.trusted_source_roots
    for b in binds:
        target = _normalize_path(b.target, label="target")
        if target != b.target:
            raise Refused(
                RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
                f"target not in canonical form: {b.target!r} → {target!r}",
            )
        if _target_is_denied(target):
            raise Refused(
                RefusalCategory.MOUNT_TARGET_DENIED,
                f"target {target!r} is on the denylist",
            )
        # Plugin contributions are bounded by the contributing plugin's
        # manifest envelope (mount_target_prefixes), checked at compose-
        # time by `run_pre_session_hooks`. The policy.mounts.extra_targets_
        # allowlist is for user-extras only — first-party plugins use
        # paths like /run/anthropic-proxy.sock that wouldn't be sensible
        # to require users add to the policy. The denylist (etc., proc,
        # docker.sock) still applies and is checked above.
        from botainer.core.spec import Provenance as _Prov
        is_plugin_contribution = b.provenance == _Prov.PLUGIN
        if not is_plugin_contribution and not _target_is_allowed(target, allowlist):
            raise Refused(
                RefusalCategory.MOUNT_TARGET_OFF_ALLOWLIST,
                f"target {target!r} is not in policy allowlist {allowlist}",
            )
        # Task #181: source denylist applies to ALL binds, including null-bind.
        # Previously null-bind binds skipped the source check on the theory
        # that "null-bind sources are always controlled anchors." But a
        # plugin (or user-malformed input) could contribute a null-bind with
        # source=/etc/shadow, and apptainer/docker would happily bind-mount
        # the shadow file at the target — the empty-anchor convention only
        # holds if the SOURCE is actually empty. Source check stays mandatory.
        source = _normalize_path(b.source, label="source")
        if source != b.source:
            raise Refused(
                RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
                f"source not in canonical form: {b.source!r} → {source!r}",
            )
        if _source_is_denied(source, trusted_roots=trusted):
            raise Refused(
                RefusalCategory.MOUNT_SOURCE_DENIED,
                f"source {source!r} is sensitive and not in trusted_source_roots "
                f"(applies to null-bind too: anchor must be empty controlled dir, "
                f"not a sensitive host path)",
            )

    _detect_conflicts(binds)
    assert_mask_invariants(binds)
