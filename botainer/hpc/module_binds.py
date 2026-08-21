"""Pure derivation of read-only software-root binds for hpc-modules (#160).

GA-blocker / HPC parity: `module load` on the host sets PATH, LD_LIBRARY_PATH,
etc. pointing at `/apps/.../{bin,lib,include}`, but those host dirs are not
mounted into the container, so module-loaded software is unreachable. This
module derives the MINIMAL set of bind mounts that makes them reachable, under
a strict security model, whose four guards are the whole protection and
must not be relaxed individually:
  1. BASELINE-DIFF — only paths the module system ADDS get considered; the
     pre-existing environment is never a source of binds.
  2. CEILING CONTAINMENT — every candidate must sit under a root the
     ROOT-OWNED site policy allows. A project config cannot widen this.
  3. SYSTEM-ROOT DENYLIST — /, /etc, /usr, /var and friends are refused
     outright, however they were derived.
  4. MIN-DEPTH + REALPATH — a candidate must be deep enough to be a real
     software root, and is resolved before checking, so a symlink cannot
     point a shallow path at a deep one.
Relax any single guard and this becomes an umbrella bind of /apps — or of a
system root — into every session. Full write-up in docs/CAPABILITY-SURFACE.md
§4h.

Why this lives here (stdlib-only, no botainer imports):
  - botainer.core.composition imports it for the direct/docker apptainer flow.
  - the standalone hpc-launcher host_helper imports it for the sbatch flow
    (it runs under botainer's interpreter via the plugin dispatcher's
    sys.executable, so the import resolves).
  - the hpc-modules HOOK (load_modules.py) does NOT import it — that hook runs
    via its own shebang (`/usr/bin/env python3`, which on HPC login nodes may
    lack botainer's deps), so it stays standalone and emits the RAW
    baseline+loaded env; composition (trusted botainer code) calls THIS to
    derive + validate the binds. One derivation, two flows, no duplication.

Security model (the umbrella-bind bar, DN-003):
  1. Trust anchor is the SitePolicy ceiling `mounts.cluster_software_roots`
     (root-owned /etc/botainer/policy.yaml). The caller passes those prefixes
     in as `policy_prefixes`; an empty list yields NO binds (feature OFF).
  2. Baseline-diff: only directories that `module load` ADDED (present in
     `loaded_env` but not `baseline_env`) are candidates. A poisoned
     pre-existing `PATH` entry is in the baseline and is never a candidate.
  3. Path-var allowlist: only a curated set of location vars is diffed;
     PYTHONPATH / PYTHONHOME / R_LIBS_USER / JULIA_DEPOT_PATH are EXCLUDED so a
     modulefile can't redirect package routing or smuggle a bind.
  4. Each candidate is realpath'd (symlink-farm hardening) and must (a) be
     absolute, (b) clear the coarse-root denylist AND the sensitive-per-user
     denylist (~/.ssh, ~/.aws, … — never module software), (c) satisfy a
     min-depth floor, (d) sit within a policy prefix (matched against BOTH the
     literal and the realpath). Structurally cannot collapse to an umbrella
     even under a misconfigured shallow policy prefix.
  5. Binds are READ-ONLY IDENTITY binds (source == target); never rw.
  6. CONSERVATIVE default (DESIGN Q1): NO common-ancestor widening. Each added
     dir is bound on its own; bin/lib/include arrive via their own path-var
     diffs (PATH/LD_LIBRARY_PATH/CPATH), so the common case is covered without
     ever binding broader than what `module load` added. Coalescing for the
     dlopen-sibling case is deferred pending a real-cluster check.
  7. `max_roots` is a LOUD ceiling: exceeding it RAISES (never silent
     truncation, which would yield an unreachable binary with no signal).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Location env vars whose `module load` additions name real software dirs we
# may need to bind. Deliberately EXCLUDES the package-routing vars that are
# botainer-managed or loader-sensitive (PYTHONPATH, PYTHONHOME, R_LIBS_USER,
# JULIA_DEPOT_PATH, NODE_PATH, ...) — a modulefile must not be able to
# redirect those or smuggle a bind through them.
PATH_VARS: frozenset[str] = frozenset({
    "PATH",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CPATH",
    "CMAKE_PREFIX_PATH",
    "PKG_CONFIG_PATH",
    "MANPATH",
    "INFOPATH",
    "CUDA_HOME",
    "CUDA_PATH",
    "MPI_HOME",
    "OMPI_DIR",
    "JAVA_HOME",
    "R_HOME",
})

# The colon-SEPARATED ("list") subset of PATH_VARS — the vars where order
# matters and a value must be PREPENDED (so the container's own entries survive),
# NOT replaced. The scalar home-dir vars (CUDA_HOME, JAVA_HOME, …) are excluded:
# they hold a single path and SET is correct for them. Single source of truth so
# the in-container prepend (cli/start._apply_module_env_file) and the
# clobber-warning (core/composition) can't drift (re-audit round 3 #1).
PATH_LIST_VARS: frozenset[str] = frozenset({
    "PATH",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CPATH",
    "CMAKE_PREFIX_PATH",
    "PKG_CONFIG_PATH",
    "MANPATH",
    "INFOPATH",
})

# Container-owned FHS roots. These binds are RO IDENTITY binds (source ==
# target), so a candidate at /usr/lib would mount the HOST's /usr/lib over the
# CONTAINER's /usr/lib — shadowing the base image's own libraries/tools (or
# leaking host system files). Module software lives at /apps, /opt/<pkg>,
# /vast, /gpfs, /software, ... — never under these. Reject any candidate that
# is, or is below, a container-owned root, regardless of policy (the backstop
# that makes an umbrella structurally impossible even under a misconfigured
# shallow policy prefix). Bare mount-point dirs (/opt, /home, /mnt, /srv, ...)
# are caught by the min-depth floor; their subtrees (/opt/intel) stay allowed.
_SYSTEM_SUBTREES: tuple[str, ...] = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libexec",
    "/etc", "/var", "/dev", "/proc", "/sys", "/run", "/boot", "/root",
)

# Sensitive per-user credential/config dirs. Module software NEVER lives here,
# but a foolish/over-broad SitePolicy ceiling (e.g. `$HOME` or `/home`) plus a
# modulefile that prepends `~/.ssh` to a path var could otherwise bind these
# RO into the container. Mirrors mount_plan.validation.SENSITIVE_USER_HOME_
# SUBPATHS so BOTH bind flows (composition + sbatch) refuse them at this single
# shared chokepoint — the derivation — rather than via per-flow backstops that
# can drift (adversarial-review S1). Matched against the candidate's REALPATH so
# a symlink into ~/.ssh is caught too.
_HOME_SENSITIVE_SUBDIRS: tuple[str, ...] = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
)

_DEFAULT_MIN_DEPTH = 2
_DEFAULT_MAX_ROOTS = 8


class SoftwareRootBindError(ValueError):
    """Raised when the derivation cannot produce a safe, bounded bind set
    (e.g. more roots than `max_roots`). Fail loud — never silently drop."""


def _split_paths(value: str) -> list[str]:
    """Split a PATH-style (`:`-separated) value into entries. Single-dir vars
    (CUDA_HOME=/apps/cuda) yield a one-element list. Empty entries dropped."""
    if not value:
        return []
    return [p for p in value.split(os.pathsep) if p]


def _depth(path: str) -> int:
    """Number of non-root components, e.g. /apps/python/3.11 -> 3."""
    return len([p for p in path.split("/") if p])


def _realpath(path: str) -> str:
    """Canonicalize for matching + denylist. os.path.realpath is pure (no
    raise) and resolves symlink farms (/apps -> /vast). Normalizes `..`."""
    return os.path.realpath(path)


def _is_system_root(realpath: str) -> bool:
    """True if the realpath is the FS root or is/contained-in a container-owned
    FHS subtree (would shadow the base image on an identity bind)."""
    if realpath == "/":
        return True
    for s in _SYSTEM_SUBTREES:
        if realpath == s or realpath.startswith(s + "/"):
            return True
    return False


def _is_sensitive_home(realpath: str) -> bool:
    """True if `realpath` is, or is inside, a sensitive per-user dir
    (~/.ssh, ~/.aws, …). These are never module software and must never be
    bound, regardless of the policy ceiling. Mirrors validation's
    _source_is_denied home-subpath check (sans the trusted_source_roots
    exception, which doesn't apply to module binds)."""
    home = os.path.expanduser("~")
    if not home or home == "~":
        return False
    home = home.rstrip("/")
    if not realpath.startswith(home + "/"):
        return False
    rel = realpath[len(home) + 1:]
    first = rel.split("/")[0] if rel else ""
    return first in _HOME_SENSITIVE_SUBDIRS


# caps.modules_inner_load: source subtrees that must never be an Lmod tree /
# MODULEPATH root. TWO groups, both checked against the realpath:
#   (1) sensitive host dirs — mirrors mount_plan.validation.DENYLISTED_SOURCES.
#   (2) container-critical FHS lib/bin trees — an identity bind of the HOST's
#       /usr/lib etc. over the container's would SHADOW the base image's own
#       libraries/tools (or leak host files). We enumerate the DEEP critical
#       subtrees explicitly (rather than blanket-refusing /usr like the #160
#       derivation does) because a real Lmod install commonly lives at
#       /usr/share/lmod/lmod (the EPEL RPM default), which must be permitted.
#       usrmerge makes /lib etc. symlinks to /usr/lib, so listing the /usr/*
#       forms + realpath resolution catches the bare /lib,/bin,… too.
_MODULE_TREE_SOURCE_DENYLIST: tuple[str, ...] = (
    # (1) sensitive
    "/etc", "/proc", "/sys", "/dev", "/boot", "/root",
    "/run/docker.sock", "/var/run/docker.sock",
    # (2) container-critical library/binary trees (shadow risk)
    "/usr/lib", "/usr/lib64", "/usr/bin", "/usr/sbin",
    "/usr/local/lib", "/usr/local/lib64", "/usr/local/bin", "/usr/local/sbin",
    "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/libexec",
)


def is_unsafe_module_tree_source(path: str) -> tuple[bool, str]:
    """caps.modules_inner_load shared guard for a Lmod-tree / MODULEPATH root
    coming from the root-owned SitePolicy. Returns (unsafe, reason).

    Used by ALL THREE code paths so they enforce IDENTICAL semantics (no
    standalone-mirror drift — the exact pattern CLAUDE.md gates):
      - Flow 1 composition (_compute_inner_load_contribution),
      - Flow 2 sbatch resolver (_inner_load_contribution_for_plan),
      - the inner --in-container disclosure (_inject_disclosed_inner_load_binds).

    Refuses:
      - a sensitive subtree (/etc, /proc, /sys, /dev, /boot, /root, docker.sock),
      - a container-critical FHS lib/bin tree (/usr/lib, /bin, … — identity
        bind would shadow the base image),
      - a sensitive per-user dir (~/.ssh, ~/.aws, …),
      - the bare FS root or a SHALLOW (< min-depth) system mount point.
    PERMITS a deep, non-sensitive system-subtree path (e.g. /usr/share/lmod/lmod).
    The input is the trusted root-owned ceiling, so this is defense-in-depth
    against an admin typo + the parity/claims-vs-impl guarantee — not an
    attacker boundary. The denylist is checked against the realpath (resolves
    usrmerge symlinks + `..`); the min-depth check uses the LITERAL normalized
    path so a bare `/lib` (depth 1) is caught before symlink resolution
    inflates it."""
    literal = os.path.normpath(path)
    real = _realpath(path)
    if _is_sensitive_home(real):
        return True, f"{path!r} is a sensitive per-user dir (never a module tree)"
    for d in _MODULE_TREE_SOURCE_DENYLIST:
        if real == d or real.startswith(d.rstrip("/") + "/"):
            return True, f"{path!r} resolves under the refused subtree {d!r}"
    if _depth(literal) < _DEFAULT_MIN_DEPTH and _is_system_root(literal):
        return True, (
            f"{path!r} is a shallow system mount point; an identity bind there "
            f"would shadow the container base image (a real Lmod tree is a deep "
            f"admin-chosen path)"
        )
    return False, ""


def _within_prefix(path: str, prefixes: list[str]) -> bool:
    """True if `path` is the prefix itself or strictly below it. A root prefix
    ('/') admits any absolute path — the system-root denylist + min-depth floor
    (not the prefix) are what then make an umbrella impossible."""
    for p in prefixes:
        if not p:
            continue
        if p == "/":
            if path.startswith("/"):
                return True
            continue
        pp = p.rstrip("/")
        if path == pp or path.startswith(pp + "/"):
            return True
    return False


def _unacceptable_reason(realpath: str, prefixes: list[str],
                         real_prefixes: list[str], min_depth: int) -> str:
    """Return "" if the candidate root is acceptable, else a short human reason
    it was dropped (for the OFF/unreachable diagnostic — adversarial-review T1).
    Acceptable iff: absolute, clears the coarse-root + sensitive-home denylist,
    meets the min-depth floor, and sits within a policy prefix (literal OR
    realpath — symlink-farm hardening, DESIGN guardrail 10)."""
    if not realpath.startswith("/"):
        return "not an absolute path"
    if _is_system_root(realpath):
        return "is/under a container-owned system root (would shadow the image)"
    if _is_sensitive_home(realpath):
        return "is/under a sensitive per-user dir (never module software)"
    if _depth(realpath) < min_depth:
        return f"below the min-depth floor ({min_depth})"
    if not (_within_prefix(realpath, prefixes)
            or _within_prefix(realpath, real_prefixes)):
        return "outside the site policy ceiling (mounts.cluster_software_roots)"
    return ""


def _acceptable(realpath: str, prefixes: list[str], real_prefixes: list[str],
                min_depth: int) -> bool:
    """Thin bool wrapper over `_unacceptable_reason` (kept for callers/tests)."""
    return not _unacceptable_reason(realpath, prefixes, real_prefixes, min_depth)


def _validate_env_shape(label: str, env: object) -> None:
    """Adversarial-review T2: the shared chokepoint validates the contribution
    shape so BOTH flows (composition + sbatch) inherit it and degrade
    identically — a non-dict / non-str env previously crashed the sbatch flow
    with a raw AttributeError instead of the SoftwareRootBindError both
    call-sites already handle."""
    if not isinstance(env, dict):
        raise SoftwareRootBindError(f"hpc-modules: {label}_env is not an object")
    for k, v in env.items():
        if not (isinstance(k, str) and isinstance(v, str)):
            raise SoftwareRootBindError(
                f"hpc-modules: {label}_env entry {k!r}={v!r} is not string/string"
            )


@dataclass(frozen=True)
class SoftwareRootDerivation:
    """Verbose derivation result for diagnostics (adversarial-review T1)."""
    binds: list[dict[str, str]]          # {"source","target","mode"} — the bound roots
    added: list[str]                     # ALL module-added candidate dirs (realpath'd)
    dropped: list[tuple[str, str]]       # (realpath, reason) for added dirs NOT bound

    @property
    def is_off(self) -> bool:
        """Module load added candidate dirs but NONE were bound — the feature is
        effectively OFF for this session (empty/excluding ceiling). The caller
        should WARN: the agent will not find the module software."""
        return bool(self.added) and not self.binds


def derive_software_root_binds_verbose(
    baseline_env: dict[str, str],
    loaded_env: dict[str, str],
    policy_prefixes: list[str],
    *,
    min_depth: int = _DEFAULT_MIN_DEPTH,
    max_roots: int = _DEFAULT_MAX_ROOTS,
) -> SoftwareRootDerivation:
    """Like `derive_software_root_binds` but also reports what was ADDED and what
    was DROPPED (with reasons), so callers can give the user a signal when the
    feature is silently OFF or a tool's dir was excluded (adversarial-review T1).
    """
    _validate_env_shape("baseline", baseline_env)
    _validate_env_shape("loaded", loaded_env)

    # Baseline-diff across the allowlisted location vars: only dirs ADDED by the
    # module load are candidates (a pre-existing poisoned entry is in the
    # baseline and is excluded here). Gathered even when the ceiling is empty so
    # the OFF diagnostic can say "you loaded modules but no ceiling is set".
    added_raw: set[str] = set()
    for var in PATH_VARS:
        before = set(_split_paths(baseline_env.get(var, "")))
        for entry in _split_paths(loaded_env.get(var, "")):
            # Re-audit (SEC-6): only ABSOLUTE entries are candidates. A relative
            # entry would be realpath'd against the launcher's CWD and could
            # land inside the policy ceiling (the post-realpath absoluteness
            # guard is then dead). A module software dir is always absolute;
            # drop relative entries at the source so CWD can never inject one.
            if entry not in before and entry.startswith("/"):
                added_raw.add(entry)
    added = sorted({_realpath(d) for d in added_raw})

    prefixes = [p for p in (policy_prefixes or []) if p]
    if not prefixes:
        # Feature OFF (fail-closed default). Every added dir is "dropped" with
        # the actionable reason, so the caller can warn if any were added.
        dropped = [
            (d, "no site ceiling configured (mounts.cluster_software_roots empty)")
            for d in added
        ]
        return SoftwareRootDerivation(binds=[], added=added, dropped=dropped)

    real_prefixes = [_realpath(p) for p in prefixes]
    roots: set[str] = set()
    dropped_list: list[tuple[str, str]] = []
    # CONSERVATIVE: no common-ancestor widening — each surviving added dir is its
    # own RO identity bind.
    for rp in added:
        reason = _unacceptable_reason(rp, prefixes, real_prefixes, min_depth)
        if reason:
            dropped_list.append((rp, reason))
        else:
            roots.add(rp)

    ordered = sorted(roots)
    if len(ordered) > max_roots:
        raise SoftwareRootBindError(
            f"hpc-modules: module load added {len(ordered)} distinct software "
            f"roots within the policy ceiling, exceeding max_roots={max_roots}: "
            f"{ordered}. Refusing rather than silently truncating (a dropped "
            f"root would yield an unreachable binary with no error). Narrow the "
            f"module set, or raise the site policy's limit deliberately."
        )
    binds = [{"source": r, "target": r, "mode": "ro"} for r in ordered]
    return SoftwareRootDerivation(
        binds=binds, added=added, dropped=sorted(dropped_list)
    )


def derive_software_root_binds(
    baseline_env: dict[str, str],
    loaded_env: dict[str, str],
    policy_prefixes: list[str],
    *,
    min_depth: int = _DEFAULT_MIN_DEPTH,
    max_roots: int = _DEFAULT_MAX_ROOTS,
) -> list[dict[str, str]]:
    """Derive read-only identity binds for module-loaded software dirs.

    baseline_env: env AFTER `module purge`, BEFORE any load.
    loaded_env:   env AFTER the requested `module load`s.
    policy_prefixes: the SitePolicy `mounts.cluster_software_roots` ceiling;
      EMPTY → [] (feature OFF; the only safe default).

    Returns a list of {"source","target","mode":"ro"} dicts, sorted, deduped,
    each within a policy prefix. Never an umbrella. Raises SoftwareRootBindError
    on a malformed contribution shape or more than `max_roots` surviving roots.
    Use `derive_software_root_binds_verbose` when you also need the added/dropped
    diagnostic (the OFF / unreachable signal).
    """
    return derive_software_root_binds_verbose(
        baseline_env, loaded_env, policy_prefixes,
        min_depth=min_depth, max_roots=max_roots,
    ).binds
