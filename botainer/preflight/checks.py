"""Preflight / self-test check definitions — the closed enum + registry map.

Per DN-029 §4.6: preflight checks are core-code constants
(a closed `PreflightCheck` enum), NOT file-loaded, so coverage is mechanical.
This module is the KEYSTONE that turns the free-string `SELFTEST_*` labels
(scattered across `capabilities/registry.py` `verification=` and `spec.py`
`Bind.self_test=`) into a closed, testable set.

At v0.1 the actual runner (`botainer selftest`) lands incrementally on top of
this; this module is the pure, fully-unit-testable foundation:
  - `PreflightCheck` — the closed enum.
  - `CheckResult` — one check's outcome (pass/fail/skip + sanitized detail).
  - `VERIFICATION_TO_CHECKS` — the authoritative map from every registry/bind
    `SELFTEST_*` free-string to the enum member(s) that verify it. A closure
    test (`tests/unit/test_preflight_checks.py`) asserts every free-string in
    the codebase is either mapped here or an explicit host-side `(none — …)`
    verification — so the labels can never silently drift again (this is
    exactly what caught the `SELFTEST_EXTRA_BINDS`/`SELFTEST_EXTRA_BIND`
    plural drift between the registry and composition).

Design authored via a verified Fable-5 subagent (wf_fecc8dc3-aa7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PreflightCheck(str, Enum):
    """Closed set of runtime security-posture checks (DN-029 §4.6).

    A member is verified either INSIDE the container (a write/stat/read probe)
    or HOST-side (mount-plan readback). Members marked "v0.2 slot" have no
    runnable check yet but exist so the enum stays the single closed inventory.
    """

    # ── filesystem: writability + read-only overlays ──
    WORKSPACE_RW = "workspace-rw"                 # /workspace is writable
    META_RO = "meta-ro"                           # meta/AGENT_ACCESS overlays refuse writes
    SECRET_RO = "secret-ro"                       # credential dir present + refuses creation
    STATE_RW = "state-rw"                         # plugin state dir writable
    DATA_RO = "data-ro"                           # mounts.extra RO binds refuse writes
    GIT_HOOKS_RO = "git-hooks-ro"                 # .git/hooks + config snapshot refuse writes
    MODULE_BINDS_RO = "module-binds-ro"           # #160 / inner-load binds present + RO
    # ── mount plan vs the runtime's actual mount table (host-verified) ──
    MOUNT_PLAN_READBACK = "mount-plan-readback"
    # ── environment the launcher promises ──
    # HOME must resolve to the writable per-project home bind. Apptainer can
    # refuse `--env HOME=`, leaving HOME elsewhere despite the mounted bind.
    # That would make cache and credential writes ephemeral. See §4bo.
    HOME_IS_WRITABLE_BIND = "home-is-writable-bind"
    # ── kernel posture ──
    CAPS_DROPPED = "caps-dropped"                 # CapEff == 0
    NO_NEW_PRIVS = "no-new-privs"                 # PR_SET_NO_NEW_PRIVS set
    # ── negative checks: nothing sensitive leaked in ──
    NEGATIVE_ETC_SHADOW = "negative-etc-shadow"   # host /etc/shadow not readable
    NEGATIVE_SSH_HOME = "negative-ssh-home"       # no host SSH material in $HOME/.ssh, /root/.ssh
    NEGATIVE_ENV_INJECTION = "negative-env-injection"  # no exec-injection env vars leaked
    # ── network (mostly v0.2; NONE is docker-only at v0.1) ──
    NETWORK_NONE = "network-none"                 # egress blocked when network.mode=none
    NETWORK_API_ONLY = "network-api-only"         # v0.2 slot (endpoint-ip-allowlist refused at v0.1)
    # ── sidecar ──
    SIDECAR_READY = "sidecar-ready"               # v0.2 slot: unix-socket handshake


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome. `detail` is a short, SANITIZED reason — never a
    file's contents, env value, or credential (the probe strips those)."""

    check: PreflightCheck
    result: str  # "pass" | "fail" | "skip"
    detail: str = ""
    # The probe TARGET this result is for (bind path for write probes; "" for
    # always-on checks). Codex Priority-A MEDIUM: completeness must key on the
    # (check, target) IDENTITY, not the enum alone — multiple targets collapse
    # to one enum (e.g. two RO binds both emit `data-ro`), so an enum-set check
    # would pass even if one target's probe silently dropped.
    target: str = ""

    @property
    def ok(self) -> bool:
        return self.result in ("pass", "skip")


# Sentinel prefix for a capability whose verification is deliberately NOT a
# runnable in-container/host check (host-side OS-level, compose-time, or a
# runtime route). The closure test accepts these verbatim.
HOST_SIDE_PREFIX = "(none —"


# Authoritative map: every `SELFTEST_*` free-string used in the codebase (both
# `capabilities/registry.py` CapabilityDef.verification AND `spec.py`
# Bind.self_test) → the enum member(s) that verify it. An empty tuple means
# "recognized but no runnable check at v0.1" — with the reason recorded in
# RESERVED_REASON so the closure test can assert it's intentional, not an
# oversight.
VERIFICATION_TO_CHECKS: dict[str, tuple[PreflightCheck, ...]] = {
    "SELFTEST_WORKSPACE_BIND": (PreflightCheck.WORKSPACE_RW, PreflightCheck.MOUNT_PLAN_READBACK),
    "SELFTEST_EXTRA_BIND": (PreflightCheck.DATA_RO, PreflightCheck.MOUNT_PLAN_READBACK),
    "SELFTEST_AGENT_ACCESS_RO": (PreflightCheck.META_RO, PreflightCheck.MOUNT_PLAN_READBACK),
    "SELFTEST_NULL_BIND": (),  # verify_against_plan skips null-binds; anchor-emptiness probe is v0.2
    "SELFTEST_MODULE_SOFTWARE_BIND": (PreflightCheck.MODULE_BINDS_RO, PreflightCheck.MOUNT_PLAN_READBACK),
    "SELFTEST_MODULE_INNER_LOAD": (PreflightCheck.MODULE_BINDS_RO, PreflightCheck.MOUNT_PLAN_READBACK),
    "SELFTEST_NETWORK_MODE": (PreflightCheck.NETWORK_NONE, PreflightCheck.NETWORK_API_ONLY),
    "SELFTEST_ENV_VARS": (PreflightCheck.NEGATIVE_ENV_INJECTION,),
    "SELFTEST_PORT_FORWARD_LOOPBACK": (),  # host-side (docker -p bind addr); docker-inspect probe is v0.2
    "SELFTEST_SCHED_SLURM_ALLOWED": (),  # compose-time policy gate; no runtime artifact to probe
}

# Reason each empty-tuple mapping is intentionally unrunnable at v0.1.
RESERVED_REASON: dict[str, str] = {
    "SELFTEST_NULL_BIND": "verify_against_plan skips null-binds; anchor-emptiness check is a v0.2 slot",
    "SELFTEST_PORT_FORWARD_LOOPBACK": "host-side property (docker -p bind address); docker-inspect probe is a v0.2 slot",
    "SELFTEST_SCHED_SLURM_ALLOWED": "compose-time policy gate; there is no runtime artifact to probe",
}


def checks_for(verification: str) -> tuple[PreflightCheck, ...]:
    """Enum members that verify a given `SELFTEST_*` / host-side string.

    Host-side `(none — …)` strings and any unmapped string return `()`. Callers
    that need to distinguish "host-side, no runner" from "mapped to nothing"
    consult HOST_SIDE_PREFIX / RESERVED_REASON."""
    return VERIFICATION_TO_CHECKS.get(verification, ())


# ── directed probe plan (pure function of a composed spec) ──
#
# The in-container probe script (botainer/preflight/probe_script.py) takes one
# argv item per directed check, three fields joined by the ASCII unit
# separator (0x1f): "<enum-value>\x1f<probe-kind>\x1f<path>". build_probe_plan
# turns a composed SessionSpec into that list. Pure + fully unit-testable
# without a runtime (the applicability matrix is the test).
US = "\x1f"

# Map a Bind.self_test → (PreflightCheck, probe-kind) for the RO/RW file probes.
# Only binds carrying these self_test labels get a directed filesystem probe;
# MOUNT_PLAN_READBACK covers presence+mode for ALL binds host-side separately.
_SELFTEST_BIND_PROBE: dict[str, tuple[PreflightCheck, str]] = {
    "SELFTEST_WORKSPACE_BIND": (PreflightCheck.WORKSPACE_RW, "write_rw"),
    "SELFTEST_AGENT_ACCESS_RO": (PreflightCheck.META_RO, "write_ro"),
    "SELFTEST_EXTRA_BIND": (PreflightCheck.DATA_RO, "write_ro"),
    "SELFTEST_MODULE_SOFTWARE_BIND": (PreflightCheck.MODULE_BINDS_RO, "write_ro"),
    "SELFTEST_MODULE_INNER_LOAD": (PreflightCheck.MODULE_BINDS_RO, "write_ro"),
    # SELFTEST_NULL_BIND: no directed probe (anchor-emptiness is v0.2).
}


def _probe_arg(check: PreflightCheck, kind: str, path: str = "") -> str:
    return f"{check.value}{US}{kind}{US}{path}"


def build_probe_plan(spec) -> list[str]:
    """Directed probe args for the in-container script, derived from `spec`.

    Always-on: CAPS_DROPPED, NO_NEW_PRIVS, NEGATIVE_ETC_SHADOW,
    NEGATIVE_SSH_HOME, NEGATIVE_ENV_INJECTION. Per-bind: a write_rw/write_ro
    probe for each bind whose self_test is in `_SELFTEST_BIND_PROBE`.
    NETWORK_NONE only when spec.network.mode == none (docker-only at v0.1;
    apptainer refuses that mode, so it never appears there). Paths containing
    any C0 control char (incl. the 0x1f separator) or a newline are refused
    by `validate_mount_plan` before launch (mount_plan/validation.py); the
    defensive skip here is unreachable for a validated spec but kept so a
    caller that hands us an unvalidated spec can't smuggle a separator.
    Runner completeness check (`planned_probe_ids`) turns any skipped/dropped
    probe into an exit-3 incompleteness, not a false pass."""
    from botainer.core.spec import BindMode
    args: list[str] = []
    # Always-on kernel + negative checks (path field unused → "").
    args.append(_probe_arg(PreflightCheck.CAPS_DROPPED, "capeff"))
    args.append(_probe_arg(PreflightCheck.NO_NEW_PRIVS, "nonewprivs"))
    args.append(_probe_arg(PreflightCheck.NEGATIVE_ETC_SHADOW, "shadow"))
    args.append(_probe_arg(PreflightCheck.NEGATIVE_SSH_HOME, "sshdir"))
    args.append(_probe_arg(PreflightCheck.NEGATIVE_ENV_INJECTION, "env_unset"))
    # HOME: compare the RUNTIME's $HOME against the value composition promised,
    # and require it writable. The expected path travels in the `path` field, so
    # the probe checks the launcher's actual promise, not a constant.
    _home = (spec.env.values or {}).get("HOME") or ""
    if _home and US not in _home and "\n" not in _home:
        args.append(_probe_arg(PreflightCheck.HOME_IS_WRITABLE_BIND, "home", _home))

    seen_targets: set[str] = set()
    for b in spec.mount_plan.binds:
        st = getattr(b, "self_test", "") or ""
        mapping = _SELFTEST_BIND_PROBE.get(st)
        if mapping is None:
            continue
        target = b.target
        if not target or US in target or "\n" in target or target in seen_targets:
            continue
        seen_targets.add(target)
        check, _default_kind = mapping
        # T1-5: derive the probe kind from the bind's ACTUAL mode,
        # not a hardcoded per-label default. `SELFTEST_EXTRA_BIND` rides BOTH RW
        # binds (/packages, /scratch, shared-auth, the agent profile dir) AND RO
        # binds — the old hardcoded `write_ro` made EVERY rw one report a bogus
        # "data-ro writable" failure on both runtimes.
        # RW → probe write_rw (expect writable); RO → probe write_ro
        # (expect NOT writable). Socket/FIFO/null-bind have no RO/RW write
        # semantics → skip. (The `check` enum is the label's category id; the
        # KIND is authoritative for pass/fail. planned_probe_ids re-derives from
        # this same builder so the completeness invariant stays intact.)
        if b.mode == BindMode.RW:
            kind = "write_rw"
        elif b.mode == BindMode.RO:
            kind = "write_ro"
        else:
            continue
        args.append(_probe_arg(check, kind, target))

    # Network: only meaningful when the mode is enforceable as "none".
    # Narrow except: only a missing network/mode attribute is tolerated (a
    # spec shape without a network) — any other error must surface, not
    # silently drop the network check (a dropped check would read as
    # "network fine" without the runner's completeness guard). Because the
    # plan is deterministic, planned_probe_ids() re-derives the same set, so a
    # dropped NETWORK_NONE here is caught as incompleteness downstream.
    from botainer.core.spec import NetworkMode
    if getattr(getattr(spec, "network", None), "mode", None) == NetworkMode.NONE:
        args.append(_probe_arg(PreflightCheck.NETWORK_NONE, "tcp_must_fail", "1.1.1.1:443"))
    return args


def planned_probe_ids(spec) -> set[tuple[PreflightCheck, str]]:
    """The exact set of (check, target) IDENTITIES `build_probe_plan(spec)`
    emits — one per probe arg, keyed on BOTH the enum and the target path.

    The runner compares this to what the probe actually reported: any planned
    (check, target) with no matching result means the probe silently didn't run
    that specific probe (arg mangling, image sh quirk, a future refactor
    dropping one target) — the runner treats that as a RUNTIME error (exit 3),
    never a pass. Keying on (check, target) — not the enum alone — is the fix
    for Codex Priority-A MEDIUM: several targets collapse to one enum (two RO
    binds both emit `data-ro`), so an enum-only set would be satisfied by ONE of
    them. This makes "exit 0 ⇒ every planned PROBE ran" a real invariant.
    Excludes MOUNT_PLAN_READBACK (folded in host-side, not probe-emitted)."""
    out: set[tuple[PreflightCheck, str]] = set()
    for arg in build_probe_plan(spec):
        parts = arg.split(US)
        val = parts[0]
        target = parts[2] if len(parts) > 2 else ""
        try:
            out.add((PreflightCheck(val), target))
        except ValueError:
            continue
    return out
