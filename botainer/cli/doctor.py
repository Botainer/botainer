"""`botainer doctor` — environment diagnostics.

Per Phase 1 of v0.1.0 plan:
- Run as preflight in `botainer setup` to catch missing prerequisites early.
- Available standalone for diagnosis.
- Per prior-art review: exits 0 unless an actionable problem requires fix.
  Warnings about optional things (apptainer missing on a Mac) don't cause
  non-zero exit.

Flags:
  --json        emit machine-readable findings (for CI / IDE use)
  --auth-only   show only credential / auth-mode findings (compact)
  --strict      treat warnings as errors (exit non-zero on warnings too)
"""

from __future__ import annotations

import json as _json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from botainer.core import exec_bit
from botainer.cli._refusal_handler import handle_refusals
from dataclasses import asdict, dataclass

import click


@dataclass(frozen=True)
class Finding:
    severity: str  # ok | info | warn | err
    check: str
    detail: str
    remediation: str = ""

    def is_actionable(self) -> bool:
        """Doctor returns non-zero only if any finding is actionable (err)."""
        return self.severity == "err"


def _docker_disk_finding(df_stdout: str, remediation: str) -> "Finding":
    """Build the Docker-disk Finding from `docker system df --format
    '{{.Type}}\\t{{.Size}}\\t{{.Reclaimable}}'` output. Pure, so the parse +
    warn threshold is unit-tested without a docker daemon. WARNs when >= 3 GB is
    reclaimable (worth a prune); otherwise INFO with the usage summary. Either
    way the remediation explains the Docker-VM-disk-vs-Mac-disk distinction."""
    import re as _re
    reclaimable_gb = 0.0
    rows: list[str] = []
    for line in df_stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append(f"{parts[0]} {parts[1]} ({parts[2]} reclaimable)")
            m = _re.search(r"([\d.]+)\s*GB", parts[2])
            if m:
                reclaimable_gb += float(m.group(1))
    if reclaimable_gb >= 3.0:
        return Finding(
            "warn", "runtime.docker_disk",
            f"~{reclaimable_gb:.1f} GB reclaimable in Docker "
            f"(run `docker builder prune -af` to free build cache safely)",
            remediation,
        )
    return Finding(
        "info", "runtime.docker_disk",
        "; ".join(rows) or "usage available", remediation,
    )


def root_version_findings(recorded, code_layout: int, running_version: str) -> list[Finding]:
    """Findings about which botainer wrote this state root (#202).

    Pure on purpose — takes the parsed record rather than a path — so every
    branch below is unit-testable without building a state root, including the
    one branch that matters and is otherwise near-impossible to stage: a root
    stamped by a build that does not exist yet.

    `recorded` is a `state.root_version.RootVersion` or None.
    """
    if recorded is None:
        # Not an error. Either the root predates version recording, or the
        # record could not be written (full disk, exhausted inode quota). Both
        # mean the same thing to the reader — an upgrade cannot be reasoned
        # about here — and neither is worth a warning on its own.
        return [Finding(
            "info", "state.root_version",
            "not recorded (this root predates version recording, or the "
            "record could not be written)",
            "",
        )]

    if recorded.layout_version > code_layout:
        # THE ONE THAT MATTERS. A newer botainer has used this root, and this
        # build does not know what it changed. Say what was seen rather than
        # guessing at consequences: the specific breakage depends on the newer
        # layout, which by definition this build cannot describe.
        return [Finding(
            "warn", "state.root_version",
            f"this root was last used by botainer {recorded.last_used_by} "
            f"(on-disk layout {recorded.layout_version}); you are running "
            f"{running_version}, which knows layout {code_layout}",
            "A NEWER botainer has used this state root. This build may not "
            "read everything in it correctly — session history is the most "
            "likely thing to be missed. Either upgrade botainer, or point "
            "MY_BOTAINER at a different root for this older build.",
        )]

    origin = (f"created by {recorded.created_by}"
              if recorded.origin_is_known else "origin not recorded")
    if recorded.layout_version < code_layout:
        return [Finding(
            "info", "state.root_version",
            f"on-disk layout {recorded.layout_version}, this build uses "
            f"{code_layout} ({origin}; last used by {recorded.last_used_by})",
            "",
        )]
    return [Finding(
        "ok", "state.root_version",
        f"layout {recorded.layout_version} ({origin}; last used by "
        f"{recorded.last_used_by})",
    )]


def case_sensitivity_findings(insensitive: bool | None, where: str) -> list[Finding]:
    """Report whether the filesystem treats Foo and foo as the same name.

    Case folding can silently replace content when a package contains names that
    differ only by case. This is an informational finding because it is normal for
    many macOS filesystems; the check describes the condition without changing it.
    The optional storage remedies remain opt-in."""
    if insensitive is not True:
        # False (case-sensitive) needs no line; None means the probe could not
        # run, which is not worth alarming about on its own.
        return []
    return [Finding(
        "info", f"fs.case_insensitive.{where}",
        "folds Foo and foo together (normal on macOS)",
        "If a package installs two files whose names differ only in case, one "
        "silently overwrites the other — no error, and the result is usually a "
        "broken import. Rare, but invisible when it happens. If an install "
        "misbehaves in a way that makes no sense, check for this first.",
    )]


def sqlite_wal_findings(wal_ok: bool | None, fs_desc: str) -> list[Finding]:
    """Will codex's databases work where the state root lives? (#146)

    Pure, so the branch nobody here can stage — a filesystem with no
    shared-memory support — is still tested.

    WHY THIS IS A DOCTOR CHECK AND NOT A DOCUMENT. The question "does codex
    break on this cluster?" previously needed the maintainer to install codex,
    log in, start a session on the parallel filesystem and paste back an error.
    It needs none of that: WAL either works on a filesystem or it does not, and
    `sqlite_wal_is_safe` finds out by opening a database. A question the user
    has to ask by hand is usually a missing capability.

    NOT an auth-mode issue, which is the thing that keeps being assumed:
    isolated, shared and broker all root codex's home under the state root, so
    they succeed or fail together.
    """
    if wal_ok is True:
        return [Finding("ok", "state.sqlite_wal",
                        f"supported here ({fs_desc}) — codex databases will work")]
    if wal_ok is False:
        return [Finding(
            "warn", "state.sqlite_wal",
            f"NOT supported on {fs_desc}",
            "codex keeps its history and session state in SQLite databases that "
            "need write-ahead logging, and this filesystem does not provide the "
            "shared memory that requires. codex will fail or corrupt its state "
            "here, in every auth mode — isolated, shared and broker all keep "
            "those files under the state root. Claude is unaffected. Move the "
            "state root to local disk (`MY_BOTAINER`), or use Claude on this "
            "machine.",
        )]
    return [Finding("info", "state.sqlite_wal",
                    f"could not be tested ({fs_desc})", "")]


def tmpdir_findings(tmpdir: str, kind: str, fs_desc: str) -> list[Finding]:
    """Where does $TMPDIR actually live, and can a unix socket go there?

    WHY THIS IS A DOCTOR CHECK. The question was going to be put to the
    maintainer as "is $TMPDIR node-local on your cluster?" — and that is not a
    per-site fact anyone should have to supply. It is a property of the machine
    the command is standing on, and `fs_kind` already knows how to ask.

    THE CONSEQUENCE IS CONCRETE, not general HPC advice. When the state root is
    too long for a unix socket path (sun_path is 108 bytes, and an HPC $SCRATCH
    eats most of it), agent-claude-broker falls back to a short runtime dir:
    $XDG_RUNTIME_DIR, then $TMPDIR, then /tmp. A unix socket on a NETWORK
    filesystem is the case that does not reliably work — so this says which one
    would be used and what it is, before a session fails somewhere unhelpful.

    `pure`, taking the already-probed values, so the network branch is testable
    on a laptop that has none.
    """
    # COMPARE AGAINST THE CONSTANTS, not re-spelled literals. The first draft
    # of this said `kind == "LOCAL"`; fs_kind's values are lowercase, so the
    # branch could never be taken and every machine reported "could not tell"
    # — while printing `(overlay (local))` on the same line, contradicting
    # itself. Reading the function said it worked; running it did not.
    from botainer.state.fs_kind import LOCAL, NETWORK

    where = tmpdir or "(unset — /tmp)"
    if kind == LOCAL:
        return [Finding("ok", "state.tmpdir",
                        f"{where} is node-local ({fs_desc})")]
    if kind == NETWORK:
        return [Finding(
            "warn", "state.tmpdir",
            f"{where} is on a NETWORK filesystem ({fs_desc})",
            "Two things follow. A unix socket cannot reliably be created on a "
            "network filesystem, and this is the directory the Claude broker "
            "falls back to when the state root path is too long for one — so a "
            "broker session may fail to start here rather than on your laptop. "
            "And anything a job writes to $TMPDIR is visible to other nodes and "
            "is not the fast per-node scratch such a variable usually implies. "
            "Set TMPDIR to node-local storage for the session if your site "
            "provides one.",
        )]
    return [Finding("info", "state.tmpdir",
                    f"{where}: could not tell what filesystem it is on "
                    f"({fs_desc})",
                    "Not a failure — an unrecognised filesystem type reads as "
                    "unknown rather than being guessed at.")]


def stale_bind_findings(stale) -> list[Finding]:
    """Report single-file binds detached by host-side atomic replacement.

    A bind can retain the old inode after a host editor writes and renames a new
    file. The resulting application errors may not identify the affected mount.
    None means mount information is unavailable; an empty list means the probe
    found no stale binds. Report the difference and name host-side recovery steps."""
    if stale is None:
        return [Finding("info", "session.stale_binds",
                        "not checked (no /proc/self/mountinfo — not Linux)",
                        "This check reads the kernel's mount table, which "
                        "exists on Linux. On a Mac host there is nothing to "
                        "check: the binds that can go stale are the ones INSIDE "
                        "a running container.")]
    if not stale:
        return [Finding("ok", "session.stale_binds",
                        "no bind points at a deleted file")]
    lines = ", ".join(f"{src} → {dst}" for src, dst in stale[:4])
    more = f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
    return [Finding(
        "err", "session.stale_binds",
        f"{len(stale)} bind(s) point at a file the host has REPLACED: "
        f"{lines}{more}",
        "The kernel marked these `//deleted`: something on the host replaced or "
        "removed the file after this container started, and a file bind pins the "
        "old inode. Anything reading through them fails in a way that names "
        "neither the file nor the mount — git, for one, reports only "
        "'unknown error occurred while reading the configuration files'. "
        "FIX, ON THE HOST (not in here): make sure the source file exists, then "
        "restart the session so the bind re-resolves. There is no in-container "
        "repair — the mount is severed, not the file.",
    )]


def session_schema_findings(census: dict, schema_version: int) -> list[Finding]:
    """Findings about session records this build can no longer read (#202).

    Pure, like `root_version_findings`: the interesting input is a census that
    would take a contrived state root to produce for real.

    The unreadable COUNT is derived here, not passed in. It used to be a third
    parameter, so a caller that computed it wrongly — or passed 0 — got a green
    "N readable" while records were being dropped. A check whose truth depends
    on the caller doing the arithmetic correctly is not a check.
    """
    from botainer.state.session_record import unreadable_by_this_build

    total = sum(census.values())
    if total == 0:
        return []
    unreadable = unreadable_by_this_build(census)
    if unreadable == 0:
        return [Finding(
            "ok", "state.session_records",
            f"{total} readable (schema {schema_version})",
        )]
    versions = sorted(
        (str(v) if v is not None else "unreadable")
        for v in census
        if v not in (schema_version, schema_version - 1)
    )
    # `from_dict` accepts N and N-1, but on the FIRST schema there is no N-1 to
    # name — printing "reads 0 and 1" invents a version that never shipped.
    reads = (f"{schema_version - 1} and {schema_version}"
             if schema_version > 1 else str(schema_version))
    detail = (f"{unreadable} of {total} session records cannot be read by this "
              f"build (found schema {', '.join(versions)}; this build reads "
              f"{reads})")

    # THE REMEDY DEPENDS ON THE DIRECTION, and getting it backwards is worse
    # than saying nothing. This used to print "upgrade to the version that
    # wrote them" for every case — which, for records that are too OLD, is an
    # instruction to DOWNGRADE, and downgrading would then strand the newer
    # records instead. That was the very case this feature exists for.
    known = [v for v in census if v is not None]
    lost = ("These sessions will not appear in `botainer list` or `botainer "
            "status` — the records are still on disk, but this version cannot "
            "parse them. ")
    if known and max(known) > schema_version:
        remedy = lost + (
            "Some were written by a NEWER botainer than the one you are "
            "running, which usually means a downgrade or a second install. "
            "Upgrading botainer makes them readable again.")
    elif known and min(known) < schema_version - 1:
        remedy = lost + (
            "They were written by a botainer old enough that this build no "
            "longer reads their format, normally because a release was "
            "skipped. There is no conversion for them yet, so leave them "
            "alone: they are data, not damage, and a later botainer can still "
            "read them.")
    else:
        remedy = lost + (
            "Their format could not be determined, so they may be damaged "
            "rather than merely old. They are safe to leave in place.")

    # WARN, not ERR: nothing is broken right now, and doctor's exit code is
    # reserved for conditions that stop you working.
    return [Finding("warn", "state.session_records", detail, remedy)]


def software_root_findings(declared: list[tuple[str, str]],
                           ceiling: list[str],
                           existing: dict[str, bool],
                           *,
                           cap_held: bool,
                           dropped: list[tuple[str, str]]) -> list[Finding]:
    """Which cluster software roots would be bound, and WHERE EACH CAME FROM.

    Describe both the effective read-only binds and their configuration
    origins so the user can act on the diagnostic:

      WHAT is being mounted  — the paths, and that they are read-only.
      WHERE IT CAME FROM     — the cluster profile, or a site policy. Without
                               that a user who disagrees with a bind cannot
                               tell whether to edit their config, pick another
                               profile, or talk to their admin.

    THE TWO ARGUMENTS ARE NOT THE SAME KIND OF THING, and an earlier version of
    this function conflated them — it took the ceiling and announced it as
    "mounted read-only", which is false wherever a site policy sets one:

      `declared`  [(path, origin)] — a DECLARATION. These get bound.
      `ceiling`   the root-owned mounts.cluster_software_roots — a LIMIT. An
                  administrator writing it is drawing a boundary, not asking
                  for a mount, and saying otherwise would tell a user their
                  whole software tree is in the container when it is not.

    Also reports a declared root that DOES NOT EXIST on this host, which is the
    likeliest real failure — a profile written for a sibling cluster, or a path
    that moved. Silence there is how "I turned it on and nothing happened"
    becomes an evening.

    `declared` IS WHAT WOULD ACTUALLY BE BOUND — post-guard, and realpaths.
    `cap_held` and `dropped` are REQUIRED keyword arguments, not optional, and
    that is the fix rather than decoration:

      Until 2026-09-04 this function was handed the RAW profile list and
      announced it as "mounted read-only" without consulting the capability
      gate or a single guard. Observed on a default install — `hpc-modules`
      ships COMMENTED OUT, so no plugin holds `caps.modules_software_roots` —
      doctor reported three roots "mounted read-only" while the launcher bound
      none of them. It listed `/etc`, which is unconditionally dropped. With a
      symlinked root it named the declared path while the container got the
      target. Zero of 51 bundled profiles declare software_roots, so every user
      of this feature hand-edits cluster.yaml and then runs doctor: the entire
      user population was on the lying path.

      Making the two new arguments REQUIRED means a caller cannot reproduce the
      old behaviour by forgetting them — it is a TypeError, not a quiet
      overstatement. Same shape as `declared_software_root_binds(site_ceiling=)`.

    Pure, so the HPC-only branches are testable on a laptop.
    """
    if declared and not cap_held:
        # The roots survive the guards but nothing will mount them, because no
        # enabled plugin holds the capability. Saying "mounted" here is the
        # exact overstatement this function existed to prevent, one level up.
        return [Finding(
            "warn", "hpc.software_roots",
            f"{len(declared)} software root(s) are declared but NONE are "
            f"mounted: no enabled plugin holds caps.modules_software_roots",
            "Declared: " + ", ".join(sorted(p for p, _ in declared))
            + ". Add `hpc-modules` to plugins_enabled in .botainer/config.yaml "
              "to bind them read-only. You do NOT need to run `module load` — "
              "a declared root is bound directly.",
        )]
    if not declared:
        if not ceiling:
            # Nothing configured is the normal state off a cluster. No line: a
            # laptop user does not need to be told about a feature that has no
            # bearing on them.
            return []
        # A ceiling with nothing declared is a real and confusing state: an
        # admin allowed something and nothing is mounted. Say which half is
        # missing rather than leaving the user to guess it is broken.
        return [Finding(
            "info", "hpc.software_roots",
            "a site policy allows software roots, but nothing declares any",
            "Your site allows binding " + ", ".join(sorted(ceiling))
            + ". Nothing is mounted from that on its own — software dirs reach "
              "the container either because a host `module load` revealed them "
              "(hpc-modules), or because your cluster profile lists them under "
              "cluster.software_roots. Add them there to bind them directly, "
              "with no module load needed.",
        )]
    origins = {p: o for p, o in declared}
    paths = sorted(origins)
    missing = [p for p in paths if not existing.get(p, False)]
    present = [p for p in paths if existing.get(p, False)]
    origin_list = ", ".join(sorted({o for _, o in declared}))
    out = [Finding(
        "info", "hpc.software_roots",
        f"{len(present)} root(s) from the {origin_list}, mounted read-only: "
        + (", ".join(present) if present else "none"),
        "" if not present else
        f"These are the cluster's own software directories, bound read-only at "
        f"their original paths. They come from the {origin_list}. Note they are "
        f"built for the HOST operating system — a binary that runs on the login "
        f"node may fail inside the container with a missing-library or GLIBC "
        f"error, which is an ABI mismatch and not a broken mount."
        + ("" if not ceiling else
           f" A site policy also limits these to within "
           f"{', '.join(sorted(ceiling))}; anything outside is dropped."),
    )]
    if dropped:
        # A root REFUSED by a guard is not the same as one that does not exist,
        # and the reason is the actionable part — "outside the site policy
        # ceiling" needs an admin, "shallower than min_depth" needs a deeper
        # path. Silence here is how a ceiling escape would have gone unseen.
        out.append(Finding(
            "warn", "hpc.software_roots.refused",
            f"{len(dropped)} declared root(s) were REFUSED and are not mounted",
            "; ".join(f"{p}: {why}" for p, why in sorted(dropped)),
        ))
    if missing:
        out.append(Finding(
            "warn", "hpc.software_roots.missing",
            f"{len(missing)} declared root(s) do not exist here: "
            + ", ".join(missing),
            "Declared in the "
            + ", ".join(sorted({origins[p] for p in missing}))
            + " but not present on this host — most often a profile written "
              "for a sibling cluster, or a path that moved. Nothing is mounted "
              "for these, and nothing else is affected. Correct the path or "
              "remove it.",
        ))
    return out


def _software_root_cap_held() -> bool:
    """Does an ENABLED plugin declare caps.modules_software_roots?

    Mirrors the launcher's gate closely enough to be honest, and fails to
    FALSE on anything it cannot determine — an over-claim here is the defect
    this whole change exists to remove, so "unsure" must read as "no".
    """
    try:
        from botainer.cli import _common
        from botainer.core import config as _cfg
        from botainer.plugins.installed import list_installed
        from botainer.plugins.manifest import load_manifest

        holders = set()
        for inst in list_installed():
            try:
                if "caps.modules_software_roots" in load_manifest(
                        inst.plugin_dir).capabilities:
                    holders.add(inst.name)
            except Exception:                                    # noqa: BLE001
                continue
        if not holders:
            return False
        root = _common.find_project_root()
        if root is None:
            # No project in scope: the cap is installed but enablement is
            # per-project, so we cannot say it is held. Report the honest no.
            return False
        return bool(holders & set(_cfg.load_config(root).plugins_enabled))
    except Exception:                                            # noqa: BLE001
        return False


def collect_findings(*, for_setup: bool = False) -> list[Finding]:
    """Collect all diagnostic findings.

    If `for_setup` is True, several checks become "err" (actionable) rather
    than informational: docker is required for setup; disk space matters;
    etc.
    """
    from botainer.state import dir as state_dir

    findings: list[Finding] = []

    # WHICH botainer is running comes first. Every finding below describes the
    # behaviour of the code that is actually imported, so if a stale copy is
    # shadowing the checkout, everything after this is a report about the wrong
    # program — and the user would have no way to tell.
    findings.extend(collect_install_findings())
    # Deployment can strip permissions even when repository modes are correct.
    findings.extend(collect_plugin_hook_findings())

    # Detect both runtimes first. Either is sufficient; doctor flags
    # "no usable runtime", not "Docker missing regardless of apptainer."
    docker_bin = shutil.which("docker")
    apptainer = shutil.which("apptainer") or shutil.which("singularity")

    # Docker
    if docker_bin:
        findings.append(Finding("ok", "runtime.docker", docker_bin))
        # Audit T6: the binary on PATH != the daemon reachable. The #1 laptop
        # failure (Docker Desktop not started) otherwise surfaced much later as
        # a raw `docker build` error. Probe `docker info` with a short timeout.
        try:
            _di = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=8,
            )
            daemon_ok = _di.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            daemon_ok = False
        if daemon_ok:
            findings.append(Finding("ok", "runtime.docker_daemon", "reachable"))
            # native-Linux testability (recon T-B): rootless Docker + daemon
            # userns-remap change --user semantics + bind ownership. The
            # session/login argv passes `--user <hostuid>:<hostgid>` assuming
            # root-ful Docker maps writes to the host user; under rootless or
            # userns-remap the credential/state files land owned by a subuid,
            # which the shared-mode ownership check then refuses. Detect + warn
            # (Mac Docker Desktop is effectively rootless too but maps writes
            # to the host user, so this is a Linux-flavored caveat).
            _di_out = (_di.stdout or "") + (_di.stderr or "")
            if "rootless" in _di_out.lower():
                findings.append(Finding(
                    "warn", "runtime.docker_rootless",
                    "Docker is running ROOTLESS",
                    "Rootless Docker maps container writes to a subuid, not your "
                    "host uid — the OAuth credential + per-project state may land "
                    "owned by a subuid and shared-mode start can then refuse to "
                    "read them. If a session refuses on credential ownership, use "
                    "root-ful Docker for the host-owned-output guarantee, or accept "
                    "subuid-owned outputs. (Not an issue on macOS Docker Desktop.)",
                ))
            try:
                _daemon_json = Path("/etc/docker/daemon.json")
                if _daemon_json.exists() and "userns-remap" in _daemon_json.read_text():
                    findings.append(Finding(
                        "warn", "runtime.docker_userns_remap",
                        "/etc/docker/daemon.json sets userns-remap",
                        "userns-remap shifts container UIDs — same ownership caveat "
                        "as rootless Docker (credential/state files may be subuid-"
                        "owned). See runtime.docker_rootless remediation.",
                    ))
            except OSError:
                pass
            # Docker disk headroom. The #1 non-obvious laptop failure: on
            # macOS/Windows, Docker runs inside a hidden virtual machine with its
            # OWN disk (nothing to do with the Mac's free space in Finder). When
            # that VM disk fills up — easy once the agent image is a few GB and a
            # browser adds ~800 MB — a launch or `image build` dies with a raw
            # "no space left on device" the user can't decode. Surface usage +
            # the reclaim command so `botainer doctor` answers "why did it fail?".
            _disk_remediation = (
                "If a session or `botainer image build` ever fails with 'no space "
                "left on device', it's DOCKER's disk that's full — on macOS/Windows "
                "Docker runs in a hidden virtual machine with its own disk, separate "
                "from your Mac's free space. Reclaim it SAFELY with `docker builder "
                "prune -af` (frees build cache — usually the biggest chunk after a "
                "build — and never deletes images); `docker image prune -f` also "
                "clears dangling layers. Do NOT use `docker system prune -a` / "
                "`docker image prune -a`: the `-a` deletes botainer's built agent "
                "image and you'd have to `botainer image build` it again. Still "
                "tight? Give Docker a bigger disk in Docker Desktop → Settings → "
                "Resources → Disk."
            )
            try:
                _dfp = subprocess.run(
                    ["docker", "system", "df", "--format",
                     "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"],
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                _dfp = None
            if _dfp is not None and _dfp.returncode == 0 and _dfp.stdout.strip():
                findings.append(_docker_disk_finding(_dfp.stdout, _disk_remediation))
        else:
            findings.append(Finding(
                "warn", "runtime.docker_daemon", "installed but daemon not reachable",
                "Docker is on PATH but the daemon isn't running. macOS: start "
                "Docker Desktop. Linux: `sudo systemctl start docker`. (Needed for "
                "`botainer image build`; not for `setup` itself.)",
            ))
    elif apptainer:
        # Apptainer is present and sufficient. Missing Docker is not
        # actionable; downgrade to info, no remediation.
        findings.append(
            Finding("info", "runtime.docker", "not on PATH (using apptainer)")
        )
    else:
        # Audit T6: no runtime is BLOCKING under setup so setup doesn't install
        # plugins onto a host that can't launch. EXCEPTION (audit —
        # cluster-ease B6 + usability MAJOR): if `sbatch` is on PATH, the user
        # is on an HPC LOGIN NODE where apptainer is often compute-node-only
        # (Yale Grace + many others). Setup's actual work (state dir, policy,
        # plugin install) needs no runtime. The original blocker remediation
        # told the user to `module load apptainer` — wrong for the cluster
        # class botainer itself documents in install-hpc.sh:154-157. Downgrade
        # to warn on a login node so setup completes; the runtime requirement
        # then surfaces at `hpc build` / `start` time with a correct message.
        on_hpc_login = bool(shutil.which("sbatch"))
        if on_hpc_login:
            findings.append(
                Finding(
                    "warn",
                    "runtime.any",
                    "no container runtime on PATH (HPC login node)",
                    "sbatch is present — this looks like an HPC login node. "
                    "Many clusters (including Yale Grace) install apptainer "
                    "ONLY on compute nodes; `module load apptainer` on the "
                    "login node won't help. Setup will proceed (it needs no "
                    "runtime to install plugins + write policy); build the "
                    "agent image from a compute node later: "
                    "`salloc -t 30 -p <part>` then "
                    "`botainer image build agent-claude --runtime apptainer`.",
                )
            )
        else:
            # DIST audit: this was `err if for_setup`, which ABORTED
            # `botainer setup` on any host without a runtime — contradicting the
            # branch directly above, which says (correctly) that setup "needs no
            # runtime to install plugins + write policy". Same missing runtime,
            # opposite verdict, decided by whether `sbatch` happens to be on
            # PATH.
            #
            # Aborting also left the user with nothing and no way forward: it
            # blocked `botainer init` + `botainer inspect`, i.e. exactly the
            # look-before-you-run flow the README tells people to use to
            # evaluate botainer BEFORE installing Docker. Setup's own work is
            # cheap and reversible, so fail-fast buys nothing here.
            #
            # Warn instead, and say plainly what will and won't work. The real
            # requirement still surfaces — with an accurate message — at
            # `image build` and `start`, which genuinely cannot proceed.
            findings.append(
                Finding(
                    "warn",
                    "runtime.any",
                    "no container runtime on PATH",
                    "Setup, `botainer init` and `botainer inspect` all work "
                    "without one — but nothing will RUN until you install a "
                    "container runtime: `botainer image build` and `botainer "
                    "start` will refuse. Install Docker "
                    "(https://docs.docker.com/get-docker/) on a laptop, or on "
                    "an HPC login node run inside `salloc` so apptainer is "
                    "visible (`module load apptainer` doesn't help on most "
                    "clusters — apptainer lives on compute nodes).",
                )
            )

    # Apptainer (HPC; optional on laptop)
    findings.append(
        Finding(
            "ok" if apptainer else "info",
            "runtime.apptainer",
            apptainer or "not found (HPC backend; optional on laptop)",
        )
    )

    # SELinux (native-Linux testability, recon T-B/T-C). On an Enforcing host
    # (RHEL/Fedora/CentOS) a plain docker bind mount is blocked with a
    # permission-denied that looks like a botainer bug. The docker adapter now
    # relabels binds (`,z`) when SELinux is enforcing; surface the state here so
    # the user understands the relabel + can fall back to `sudo setenforce 0` for
    # a quick test if a bind still fails.
    if docker_bin:
        _selinux_mode = None
        try:
            _enf = Path("/sys/fs/selinux/enforce")
            if _enf.exists():
                _selinux_mode = "Enforcing" if _enf.read_text().strip() == "1" else "Permissive"
        except OSError:
            _selinux_mode = None
        if _selinux_mode == "Enforcing":
            findings.append(Finding(
                "info", "host.selinux",
                "Enforcing — docker runs get `--security-opt label=disable`",
                "Under Enforcing SELinux a plain docker bind mount is blocked "
                "with permission-denied. botainer adds `--security-opt "
                "label=disable` to the docker run on SELinux-enforcing hosts so "
                "binds work. If a bind still fails, `sudo setenforce 0` for a "
                "test run or check `ausearch -m avc -ts recent`.",
            ))
        elif _selinux_mode == "Permissive":
            findings.append(Finding("ok", "host.selinux", "Permissive"))

    # Slurm (HPC only)
    sbatch = shutil.which("sbatch")
    findings.append(
        Finding(
            "info",
            "runtime.slurm",
            sbatch or "not found (HPC scheduler; optional on laptop)",
        )
    )

    # Audit T6/#160: surface the root-owned SITE policy + the hpc-modules
    # software-root ON-switch so an admin can verify it from `botainer doctor`.
    # Host-level (the site policy is per-host); helps diagnose "module tools
    # unreachable by name" without reading source.
    from pathlib import Path as _Path
    _site = _Path("/etc/botainer/policy.yaml")
    if _site.exists():
        _roots: list = []
        try:
            import yaml as _yaml
            _data = _yaml.safe_load(_site.read_text()) or {}
            _roots = ((_data.get("mounts") or {}).get("cluster_software_roots")) or []
        except Exception:
            _roots = []
        if _roots:
            findings.append(Finding(
                "ok", "site.cluster_software_roots",
                f"{_roots} (hpc-modules software binds ENABLED)",
            ))
        else:
            findings.append(Finding(
                "info", "site.cluster_software_roots",
                "empty — hpc-modules software-root binds are OFF",
                "If users rely on `module load` software in the container, set "
                "mounts.cluster_software_roots in /etc/botainer/policy.yaml "
                "(admin/root). See docs/SITE-ADMIN.md.",
            ))
        # caps.modules_inner_load discoverability (audit MEDIUM): surface
        # whether the AGENT can run `module load` inside the container.
        try:
            _lmod = ((_data.get("mounts") or {}).get("cluster_lmod_root")) or ""
        except Exception:
            _lmod = ""
        if _lmod:
            findings.append(Finding(
                "ok", "site.cluster_lmod_root",
                f"{_lmod} (in-container `module load` ENABLED)",
            ))
        else:
            findings.append(Finding(
                "info", "site.cluster_lmod_root",
                "empty — in-container `module load` is OFF",
                "To let the agent run `module load` itself (batch-job friendly), "
                "set mounts.cluster_lmod_root + cluster_modulepath_roots in "
                "/etc/botainer/policy.yaml (admin/root). See docs/SITE-ADMIN.md §4k.",
            ))
    else:
        findings.append(Finding(
            "info", "site.policy",
            "no root-owned /etc/botainer/policy.yaml (using safe defaults)",
        ))

    # State dir + policy
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    findings.append(Finding("info", "state.dir", str(paths.root)))
    # Internal design note DN-036: warn if MY_BOTAINER resolves onto a path that
    # looks like an HPC scratch subtree (named `scratch`, `scratch60`, …,
    # anywhere in the path). The state dir holds credentials, project UUIDs,
    # .sif images, and the installed plugin tree — losses are unrecoverable.
    # Earlier `hpc setup` actively suggested redirecting state to $SCRATCH
    # (removed); this check catches users still living with the
    # old advice on disk. Heuristic = a path component literally named
    # `scratch` or `scratch<n>` (matches `/scratch/`, `/<vendor>/scratch/`,
    # `/<vendor>/scratch60/`, …). `tmp` is intentionally NOT in the list — `/tmp/`
    # is legitimately used by the pytest tmpdir mechanism for tests, and the
    # auto-purge concern this check is about is HPC scratch policy, not
    # tmpfs.
    import re as _re
    _scratch_parts = {
        p for p in paths.root.parts
        if _re.fullmatch(r"scratch\d*", p.lower())
    }
    if _scratch_parts:
        findings.append(Finding(
            "warn",
            "state.dir_on_purged_storage",
            f"state dir {paths.root} contains a scratch path component",
            "HPC scratch auto-purges (Grace: 60 days) — losing this wipes "
            "credentials, project UUIDs, .sif images, and the installed plugin "
            "tree (none recoverable). Move state back to $HOME by `unset "
            "MY_BOTAINER` (or removing it from your shell rc); leave scratch "
            "for per-session work via the profile's `scratch.template`.",
        ))
    if paths.root.exists():
        findings.append(Finding("ok", "state.exists", "yes"))
        # Which botainer wrote this root (#202). Only meaningful once the root
        # exists; on a not-yet-created root there is nothing to have written it.
        # Both readers are documented as never raising, and both are now
        # written to keep that. The belt-and-braces is deliberate anyway: this
        # is `doctor`, the command someone runs BECAUSE their state root is in
        # a bad way, and it is the one command that must not die on the file it
        # was asked to look at. A diagnostic that crashes on damage reports
        # nothing about the damage.
        from botainer import __version__ as _running
        from botainer.state import root_version as _rv
        from botainer.state import session_record as _sr
        try:
            findings.extend(root_version_findings(
                _rv.read(paths.root), _rv.LAYOUT_VERSION, _running,
            ))
            # Session records this build can no longer read (#202). The cliff
            # is two versions wide and `list_sessions` drops the fallers, so
            # this count is the only up-front notice anyone gets.
            findings.extend(session_schema_findings(
                _sr.schema_version_census(paths.root), _sr.SCHEMA_VERSION,
            ))
            # Will codex work on this filesystem? (#146) Probed, not inferred.
            from botainer.state import fs_kind as _fk
            findings.extend(sqlite_wal_findings(
                _fk.sqlite_wal_is_safe(paths.root), _fk.describe(paths.root),
            ))
            # Does this filesystem fold Foo and foo? (#167) Probed, and stated
            # rather than prevented — see case_sensitivity_findings.
            findings.extend(case_sensitivity_findings(
                _fk.is_case_insensitive(paths.root), "state_root"))
            # Has a host-side file replacement severed a bind? (#208) The
            # kernel says so; we only have to look.
            findings.extend(stale_bind_findings(_fk.read_stale_binds()))
            # Is $TMPDIR node-local? (PLAN §1) MEASURED on this machine, not
            # collected from the user per site.
            import os as _os_tmp
            _tmp = _os_tmp.environ.get("TMPDIR", "")
            _tmp_path = _tmp or "/tmp"
            findings.extend(tmpdir_findings(
                _tmp, _fk.classify(_tmp_path), _fk.describe(_tmp_path)))
            # Which cluster software roots would be bound, and from where
            # (#171/#156). Silent off a cluster: nothing configured is the
            # normal state on a laptop and needs no line.
            try:
                import os as _os

                from botainer.core.composition import declared_software_roots
                from botainer.core import policy as _pol
                _eff = _pol.intersect(_pol.load_site_policy(),
                                      _pol.load_user_policy())
                # RUN THE SAME GUARDS THE LAUNCHER RUNS, and report their
                # answers — not the raw profile list. Doctor used to print the
                # declaration and call it "mounted", which is false in the
                # default configuration and names the wrong path when a root is
                # a symlink. `declared_software_root_binds` is pure and needs
                # no session, so doctor can ask it directly.
                from botainer.core.composition import software_root_ceiling
                from botainer.hpc.module_binds import (
                    declared_software_root_binds,
                )
                _roots = declared_software_roots(_eff)
                _ceil, _ = software_root_ceiling(_eff)
                if _roots:
                    _binds, _dropped, _origins = declared_software_root_binds(
                        _roots, site_ceiling=_ceil)
                    # Realpaths, because that is what gets bound.
                    _decl = [(b["source"], _origins.get(b["source"], "cluster profile"))
                             for b in _binds]
                else:
                    _decl, _dropped = [], []
                # The capability gate, asked the same way composition asks it:
                # is there an INSTALLED plugin declaring the cap that this
                # project also ENABLES? Doctor may run outside a project, in
                # which case enablement is unknown and we report on installed.
                _cap = _software_root_cap_held()
                findings.extend(software_root_findings(
                    _decl, list(_eff.mounts.cluster_software_roots),
                    {p: _os.path.isdir(p) for p, _ in _decl},
                    cap_held=_cap, dropped=_dropped))
            except Exception as _exc:                           # noqa: BLE001
                # NOT `pass`. In a diagnostic tool a swallowed failure is
                # indistinguishable from "nothing to report", and the user
                # reads the silence as an all-clear. Say the check did not run.
                findings.append(Finding(
                    "warn", "hpc.software_roots",
                    f"could not determine which cluster software roots would "
                    f"be mounted ({type(_exc).__name__}: {_exc})",
                    "This is a diagnostic gap, not a broken session — the "
                    "binds themselves are decided at launch. Most often an "
                    "unreadable policy.yaml or cluster.yaml; `botainer where` "
                    "prints the paths to check.",
                ))
        except Exception as exc:                                # noqa: BLE001
            findings.append(Finding(
                "warn", "state.root_version",
                f"could not be read ({type(exc).__name__})",
                "botainer could not inspect this state root's version records. "
                "Everything else in this report is still valid; only the "
                "upgrade checks were skipped.",
            ))
    elif for_setup:
        findings.append(
            Finding("info", "state.exists", "no (will create)", "")
        )
    else:
        findings.append(
            Finding(
                "warn",
                "state.exists",
                "no",
                "Run `botainer setup` to initialize.",
            )
        )

    # Policy
    policy_path = paths.root / "policy.yaml"
    if policy_path.exists():
        # UX audit (B3): this only checked .exists, so doctor printed
        # a green tick on a policy file that made EVERY other botainer command
        # refuse (e.g. the docs' own `version: 1`, which must be "policy-v1").
        # The command whose job is "diagnose any issue" was blessing the broken
        # file. Parse it the same way the loader does.
        try:
            from botainer.core.policy import load_user_policy
            load_user_policy()
            findings.append(Finding("ok", "policy.yaml", str(policy_path)))
        except Exception as exc:
            first = str(exc).strip().splitlines()[0][:160]
            findings.append(Finding(
                "err", "policy.yaml", f"INVALID — {first}",
                f"Every botainer command refuses while this file is invalid. "
                f"Edit {policy_path} (a `version: 1` must be `version: policy-v1`), "
                f"or delete it to fall back to defaults.",
            ))
    elif for_setup:
        findings.append(Finding("info", "policy.yaml", "missing (will create)"))
    else:
        findings.append(
            Finding(
                "warn",
                "policy.yaml",
                "missing",
                "Run `botainer setup` to write defaults.",
            )
        )

    # Upgrade footgun: a stale user policy pinning a network ceiling more
    # restrictive than the default `internet` config `botainer init` writes will
    # make the very next `botainer start` refuse — and the old refusal blamed
    # "admin/site policy" even when it was the user's own carried-over policy.
    # Surface it here proactively (the "why is start refused" home), with the
    # exact one-liner. Non-destructive.
    try:
        from botainer.core import policy as _policy_mod
        _stale = _policy_mod.stale_restrictive_user_ceiling()
    except Exception:
        _stale = None
    if _stale:
        findings.append(
            Finding(
                "warn",
                "policy.network_ceiling",
                _stale.split(" — ")[0],  # the condition
                "botainer policy set network.default_mode internet "
                "(your own user policy — no admin needed; likely stale from an "
                "older version)",
            )
        )

    # Bundled plugins discoverable
    from botainer.plugins import builtin
    builtin_root = builtin.find_builtin_plugins_root()
    if builtin_root is not None:
        names = [p.name for p in builtin.discover_builtin_plugins()]
        findings.append(
            Finding("ok", "plugins.bundled.source", str(builtin_root))
        )
        findings.append(Finding("info", "plugins.bundled.names", ", ".join(names)))
    elif for_setup:
        findings.append(
            Finding(
                "err",
                "plugins.bundled.source",
                "not found",
                "The launcher's bundled plugins directory is missing. Reinstall botainer.",
            )
        )
    else:
        findings.append(
            Finding(
                "warn",
                "plugins.bundled.source",
                "not found (running from a non-standard install)",
            )
        )

    # Disk space (only critical for setup)
    if for_setup and paths.root.parent.exists():
        try:
            stat = shutil.disk_usage(str(paths.root.parent))
            gb_free = stat.free / 1024**3
            if gb_free < 5:
                findings.append(
                    Finding(
                        "err",
                        "disk.free",
                        f"{gb_free:.1f} GB on {paths.root.parent}",
                        f"Free at least 5 GB at {paths.root.parent} before running setup. "
                        f"The agent image is ~3.5 GB.",
                    )
                )
            else:
                findings.append(Finding("ok", "disk.free", f"{gb_free:.1f} GB free"))
        except OSError:
            pass

    # Network reachability (only for setup; non-blocking check)
    # HPC review F10: skip the Docker Hub check on HPC (no Docker daemon
    # present anyway; apptainer build pulls via a different path).
    if for_setup and docker_bin:
        if _can_reach("registry-1.docker.io", 443) or _can_reach("docker.io", 443):
            findings.append(Finding("ok", "network.docker_hub", "reachable"))
        else:
            findings.append(
                Finding(
                    "warn",
                    "network.docker_hub",
                    "unreachable",
                    "Image build pulls layers from Docker Hub. Check your network or "
                    "set up a local mirror.",
                )
            )

    # Host Claude CLI: informational only. Not required for botainer.
    # `botainer auth login` runs `claude /login` INSIDE a container —
    # the credential lands in botainer's auth dir regardless of whether
    # `claude` is installed on the host. We report this so users who
    # ALSO use `claude` directly (outside botainer) can confirm their
    # PATH is set up.
    claude_bin = shutil.which("claude")
    if claude_bin:
        findings.append(Finding("info", "host.claude_cli", claude_bin))
    else:
        findings.append(
            Finding(
                "info",
                "host.claude_cli",
                "not on PATH (not required; container bundles claude)",
            )
        )

    # §A19 + §A18: the `nudge` plugin runs `screen` on the HOST (not in
    # the container) — `botainer start` refuses if nudge is in
    # plugins_enabled and screen isn't on PATH, and `botainer nudge`
    # itself shells out to `screen -X stuff`. macOS ships /usr/bin/screen
    # built-in; Linux/BSD users typically need `apt install screen` or
    # equivalent. Doctor surfaces this BEFORE the user hits a launch-time
    # refusal. We don't know here whether any project will enable nudge,
    # so the severity is `warn` (not actionable, but visible) — installs
    # take seconds and the remediation is in the message.
    screen_bin = shutil.which("screen")
    if screen_bin:
        findings.append(Finding("ok", "host.screen", screen_bin))
    else:
        findings.append(
            Finding(
                "warn",
                "host.screen",
                "not on PATH (required by the `nudge` plugin; harmless if "
                "nudge stays disabled)",
                "macOS: ships built-in at /usr/bin/screen. "
                "Linux: `apt install screen` / `dnf install screen`. "
                "HPC: usually preinstalled; if not, `module load screen` "
                "or ask the cluster admin.",
            )
        )

    # Identify the imported code so conflicting installations can be diagnosed.
    try:
        import botainer as _botainer
        botainer_init_path = _botainer.__file__ or ""
    except Exception:
        botainer_init_path = ""
    if botainer_init_path:
        findings.append(
            Finding(
                "info",
                "install.code_loaded_from",
                botainer_init_path,
            )
        )
        # Editable install detection: same logic as
        # botainer.plugins.lifecycle._editable_source_plugins_root.
        from pathlib import Path as _Path
        here = _Path(botainer_init_path).resolve()
        editable_clone: str | None = None
        for parent in list(here.parents)[:6]:
            pyproject = parent / "pyproject.toml"
            if not pyproject.exists():
                continue
            try:
                content = pyproject.read_text(encoding="utf-8")
            except OSError:
                continue
            if 'name = "botainer"' in content or "name = 'botainer'" in content:
                editable_clone = str(parent)
                break
        if editable_clone:
            findings.append(
                Finding(
                    "ok",
                    "install.editable_clone",
                    editable_clone,
                )
            )
    # Plugin source: which directory does list_installed currently read?
    try:
        from botainer.plugins.lifecycle import (
            _editable_source_plugins_root,
            list_installed,
        )
        src_root = _editable_source_plugins_root()
        if src_root:
            findings.append(
                Finding(
                    "ok",
                    "install.plugins_source",
                    f"editable: {src_root}",
                )
            )
        else:
            # Installed (copied) path; show the directory.
            installed = list_installed()
            if installed:
                # All entries share the parent; show that.
                findings.append(
                    Finding(
                        "info",
                        "install.plugins_source",
                        f"installed: {installed[0].plugin_dir.parent}",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "warn",
                        "install.plugins_source",
                        "no plugins found",
                        "Run `botainer setup` to install bundled plugins.",
                    )
                )
    except Exception as exc:
        findings.append(
            Finding(
                "warn",
                "install.plugins_source",
                f"introspection failed: {exc}",
            )
        )

    # HPC readiness summary (one line; the individual runtime / slurm
    # checks above carry the details).
    if apptainer and sbatch:
        findings.append(
            Finding(
                "ok",
                "hpc.readiness",
                "apptainer + sbatch present; cluster usable",
            )
        )
    elif apptainer or sbatch:
        findings.append(
            Finding(
                "warn",
                "hpc.readiness",
                "partial: " + ("apptainer only" if apptainer else "sbatch only"),
                "HPC mode needs both. NOTE apptainer is usually on COMPUTE nodes only, so a login-node `module load apptainer` often does not help ("
                "your cluster's equivalent) is missing on PATH.",
            )
        )

    return findings


def install_findings(
    *,
    live_module_dir: Path | None,
    editable_target: Path | None,
    version: str,
    console_script: Path | None = None,
    script_interpreter: Path | None = None,
    foreign_package_dirs: tuple[Path, ...] = (),
) -> list[Finding]:
    """Identify the imported Botainer installation and conflicting package copies.

    A copied package in site-packages can shadow an editable installation even
    when package metadata describes the editable copy. Tests run from the checkout
    can then pass while the CLI imports different code. Report the imported path
    and detected competing copies. Environment probing happens separately in
    collect_install_findings so these decisions can be tested without site changes."""
    findings: list[Finding] = []

    if live_module_dir is None:
        return [Finding(
            "warn", "install.location",
            "cannot determine which botainer package is imported",
            "Unusual; report it with the output of "
            "`python3 -c 'import botainer; print(botainer.__file__)'`.")]

    if editable_target is None:
        # A normal (non-editable) install. There is no checkout to diverge
        # from, so shadowing is not a possible state — and `install.
        # code_loaded_from` below already prints where the code came from, so
        # saying it again here would just be a second line with the same fact.
        return _console_script_findings(console_script, script_interpreter)

    try:
        shadowed = not live_module_dir.is_relative_to(editable_target)
    except (AttributeError, ValueError):        # py<3.9 / unrelated roots
        shadowed = not str(live_module_dir).startswith(str(editable_target))

    if shadowed:
        findings.append(Finding(
            "err", "install.shadowed",
            f"botainer is installed EDITABLE from {editable_target}, but the "
            f"code actually imported lives at {live_module_dir}. A copied "
            f"install is shadowing your checkout — edits, fixes and commits "
            f"in {editable_target} are NOT what runs.",
            f"Remove the copy so the editable install takes effect:\n"
            f"      python3 -m pip uninstall -y botainer && "
            f"python3 -m pip install -e {editable_target}\n"
            f"    Then re-run `botainer doctor` — this check must say 'live'."))
    elif foreign_package_dirs:
        # Running FROM the checkout puts it first on sys.path, so the import
        # looks healthy while `botainer` launched from any other directory
        # still gets the copy. Checking only what happened to import would
        # therefore report "fine" from inside the repo — which is precisely
        # where a developer runs doctor, and precisely when they are asking
        # "why isn't my fix live?". Detect that the copy EXISTS.
        listed = ", ".join(str(p) for p in foreign_package_dirs)
        findings.append(Finding(
            "err", "install.shadowed",
            f"this run imported {live_module_dir} (your checkout, because the "
            f"current directory wins), but a SEPARATE installed copy exists at "
            f"{listed}. Run `botainer` from anywhere else and that copy is what "
            f"executes — so the same command behaves differently depending on "
            f"where you stand.",
            f"Remove the copy so the editable install is the only one:\n"
            f"      python3 -m pip uninstall -y botainer && "
            f"python3 -m pip install -e {editable_target}"))
    # No "all clear" line on the healthy path. `install.code_loaded_from` and
    # `install.editable_clone` (further down) already report the live path and
    # the clone; a third line repeating them is noise, and doctor's output is
    # only useful while every line still earns its place. This collector speaks
    # ONLY when something is wrong.
    return findings + _console_script_findings(console_script, script_interpreter)


def _console_script_findings(
    console_script: Path | None, script_interpreter: Path | None,
) -> list[Finding]:
    """Report a console script whose shebang points to a missing interpreter.

    For example, deleting a temporary virtual environment leaves its launcher on
    PATH with a stale interpreter path. Diagnose that launcher separately from the
    Botainer package it was intended to invoke."""
    if console_script is None or script_interpreter is None:
        return []
    if script_interpreter.exists():
        return []
    return [Finding(
        "err", "install.console_script",
        f"`{console_script}` runs interpreter {script_interpreter}, which does "
        f"not exist — the command fails with 'bad interpreter'.",
        f"Reinstall so the script is regenerated:\n"
        f"      python3 -m pip install -e <your botainer checkout>")]


def collect_install_findings() -> list[Finding]:
    """Gather the real environment facts and hand them to `install_findings`."""
    import botainer as _pkg

    live_module_dir: Path | None = None
    if getattr(_pkg, "__file__", None):
        live_module_dir = Path(_pkg.__file__).resolve().parent

    version = getattr(_pkg, "__version__", "?")

    # PEP 610: an editable install records its source dir in direct_url.json.
    editable_target: Path | None = None
    try:
        from importlib.metadata import distribution
        raw = distribution("botainer").read_text("direct_url.json")
        if raw:
            durl = _json.loads(raw)
            if durl.get("dir_info", {}).get("editable"):
                url = durl.get("url", "")
                if url.startswith("file://"):
                    editable_target = Path(url[len("file://"):]).resolve()
    except Exception:                            # not installed / no metadata
        editable_target = None

    console_script: Path | None = None
    script_interpreter: Path | None = None
    found = shutil.which("botainer")
    if found:
        console_script = Path(found)
        try:
            first = console_script.read_bytes().split(b"\n", 1)[0]
            if first.startswith(b"#!"):
                interp = first[2:].strip().split()[0].decode("utf-8", "replace")
                script_interpreter = Path(interp)
        except (OSError, IndexError, UnicodeDecodeError):
            script_interpreter = None

    # Look for an installed COPY independently of what this process imported.
    foreign: list[Path] = []
    if editable_target is not None:
        import sysconfig
        candidates = {sysconfig.get_paths().get("purelib"),
                      sysconfig.get_paths().get("platlib")}
        try:
            import site
            candidates.update(site.getsitepackages())
            candidates.add(site.getusersitepackages())
        except Exception:
            pass
        for base in filter(None, candidates):
            pkg = Path(base) / "botainer"
            # A real directory here is a COPY *only if it is importable*.
            #
            # The old comment claimed "an editable install leaves only a
            # .pth/finder shim, never a package directory". That is untrue for
            # THIS package: pyproject force-includes plugins/, cluster_profiles/
            # and the licence files into botainer/, and hatchling materialises
            # them as a real site-packages/botainer/ directory even for
            # `pip install -e .`. It holds four data entries and NO __init__.py.
            #
            # Python cannot import a directory with no __init__.py as `botainer`
            # (there is no namespace-package ambiguity here — the checkout's
            # regular package wins outright), so it cannot shadow anything. The
            # old check saw the directory, cried shadowing, and made
            # `botainer doctor` exit 1 and `botainer setup` ABORT on every
            # correct editable install — while advising a
            # `pip uninstall && pip install -e .` that recreates the same state.
            #
            # Asking "is there a competing IMPORTABLE package" instead of "does
            # a directory exist" is the actual question, so a false positive
            # here is not possible rather than merely unlikely.
            if (pkg.is_dir() and not pkg.is_symlink()
                    and (pkg / "__init__.py").exists()):
                resolved = pkg.resolve()
                if not resolved.is_relative_to(editable_target) \
                        and resolved not in foreign:
                    foreign.append(resolved)

    return install_findings(
        live_module_dir=live_module_dir,
        editable_target=editable_target,
        version=version,
        console_script=console_script,
        script_interpreter=script_interpreter,
        foreign_package_dirs=tuple(foreign),
    )


def collect_plugin_hook_findings() -> list[Finding]:
    """Check whether installed plugin hooks are runnable on this machine.

    Repository tests verify recorded file modes, but deployment can change them:
    archive extraction, copying, or filesystem differences can leave a hook
    unexecutable. Inspect the installed tree so doctor identifies these failures
    before the next session launch."""
    import yaml

    findings: list[Finding] = []
    try:
        from botainer.plugins.lifecycle import list_installed
        installed = list_installed()
    except Exception as exc:
        return [Finding("warn", "plugins.hooks",
                        f"could not enumerate installed plugins: {exc}")]

    broken: list[str] = []
    missing: list[str] = []
    checked = 0
    for inst in installed:
        manifest = inst.plugin_dir / "botainer-plugin.yaml"
        try:
            data = yaml.safe_load(manifest.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        for hook in data.get("hooks") or []:
            script = hook.get("script")
            if not script:
                continue
            path = inst.plugin_dir / script
            checked += 1
            if not path.is_file():
                missing.append(f"{inst.name}:{hook.get('when', '?')} → {path}")
            # MIRRORS run_hook's policy, and must keep mirroring it. A `.py`
            # hook is dispatched as [sys.executable, script] — through
            # botainer's own interpreter, never its shebang — so it does NOT
            # need the execute bit and `start` runs it fine. Reporting one as
            # broken sent the user to `chmod +x` for a session that works, and
            # since every bundled hook is `.py`, EVERY firing of this finding
            # would have been false. Found by a refuting review.
            #
            # is_executable, NOT os.access, for the hooks that DO need the bit:
            # os.access returns True for a 0o644 file on a filesystem that does
            # not enforce it (see core/exec_bit.py).
            elif (not str(path).endswith(".py")
                  and not exec_bit.is_executable(path)):
                broken.append(f"{inst.name}:{hook.get('when', '?')} → {path}")

    if missing:
        findings.append(Finding(
            "err", "plugins.hooks_missing",
            f"{len(missing)} declared hook script(s) do not exist: "
            + "; ".join(missing[:3]) + ("…" if len(missing) > 3 else ""),
            "The plugin will refuse at session start. Reinstall the plugin "
            "(`botainer setup`) or check the deploy copied every file."))
    if broken:
        findings.append(Finding(
            "err", "plugins.hooks_not_executable",
            f"{len(broken)} non-.py hook script(s) are not executable: "
            + "; ".join(broken[:3]) + ("…" if len(broken) > 3 else ""),
            "`botainer start` refuses with 'hook script not executable'. Fix "
            "with `chmod +x` on the paths above. If a deploy stripped the bit, "
            "re-copy with `rsync -a` (or `git clone`), which preserves it."))
    if checked and not (missing or broken):
        findings.append(Finding(
            "ok", "plugins.hooks", f"{checked} declared hooks present + executable"))
    return findings


def _can_reach(host: str, port: int, timeout: float = 2.0) -> bool:
    """Best-effort TCP reachability check."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def render_findings(findings: list[Finding]) -> None:
    """Print findings to stderr with color + glyph + remediation."""
    color_map = {"ok": "green", "info": "cyan", "warn": "yellow", "err": "red"}
    glyph_map = {"ok": "✓", "info": "·", "warn": "!", "err": "✗"}
    for f in findings:
        glyph = glyph_map.get(f.severity, "?")
        color = color_map.get(f.severity)
        click.secho(f"{glyph} {f.check:30s} {f.detail}", fg=color)
        if f.remediation and f.severity in ("err", "warn"):
            click.secho(f"    → {f.remediation}", fg="cyan")


def render_findings_json(findings: list[Finding]) -> str:
    """JSON output for CI / IDE consumption."""
    return _json.dumps(
        {
            "findings": [asdict(f) for f in findings],
            "ok": all(not f.is_actionable() for f in findings),
        },
        indent=2,
        sort_keys=True,
    )


#: WHO ACTUALLY REFUSES A BAD .sif — said in ONE place, because saying it in
#: two produced two different falsehoods in consecutive commits.
#:
#: Neither launcher consults the recorded digest unconditionally. Both resolve
#: an image through an ordered list, and a top-level `image:` in the project
#: config is taken EARLY — before the marker for `hpc submit`
#: (`_resolve_apptainer_image` case 2 vs case 3) and without any verification
#: call for `start` (`_resolve_session_image`'s cfg.image branch). That config
#: is what `GETTING_STARTED-HPC.md` documents and `examples/hpc-slurm.yaml`
#: ships, so it is not an edge case.
#:
#: A doctor finding may therefore state WHAT DOCTOR MEASURED and name the
#: condition under which the launchers disagree. It may NOT promise that a
#: launcher will refuse — that is a prediction about code doctor does not run,
#: and it was wrong both times it was written.
#:
#: THAT PROHIBITION IS NOW CONDITIONAL, and the condition is a pin. The
#: sentence below DOES predict what the launchers do, because the prediction is
#: checked by a test: `composition._ENFORCE_SIF_PROVENANCE` is the whole policy
#: in one dict, reached from a SINGLE exit that every resolution branch passes
#: through, and `tests/unit/test_doctor_digest_sentence_matches_the_policy.py`
#: drives the resolver per resolution source and asserts THIS STRING against the
#: behaviour it just measured. An unpinned prediction was wrong twice; a pinned
#: one is a claim something keeps honest. Predict nothing here that the test does
#: not hold — in particular, the `image:` case says "warns" because the dict says
#: False, and flipping it to True fails that test until this sentence is
#: rewritten.
#:
#: Two grep-based pins were tried first and both were evadable (a deleted call
#: with the old line left behind as a comment; the dict KEY deleted, which flips
#: the behaviour via the fail-closed default). Hence a test, not a pattern match.
#:
#: One disagreement is doctor's OWN, and stays: doctor hashes the file it found
#: under the state root, and a project whose `image:` points elsewhere makes the
#: launcher hash a DIFFERENT file. Measured fact about doctor, not a prediction.
_WHO_CHECKS_THE_DIGEST = (
    "`botainer hpc submit` refuses a mismatch, and so does `botainer start` — "
    "EXCEPT when your project config points a top-level `image:` at an existing "
    ".sif, where start warns loudly and runs it anyway (until 2026-09-12 start "
    "did not look at that .sif at all, silently). Both hash the file THEY "
    "resolve, which is this one unless an `image:` points somewhere else."
)


def _apptainer_image_finding(
        plugin_name: str, sif_path, *, verify_digest: bool) -> Finding:
    """Does this .sif still match what botainer recorded when it built it?

    `doctor` USED TO ANSWER A DIFFERENT QUESTION THAN `start` DOES. It globbed
    for a file, found one, and printed "built (N MiB)" — while
    `composition._verify_apptainer_sif_provenance` hashed the same file,
    compared it to the `apptainer:sha256:` marker in installed.lock, and
    REFUSED on a mismatch. So a replaced image gave a green `doctor --strict`
    and a refused `hpc submit`, and `doctor --strict` is exactly the command
    the HPC guide sends people to before launching jobs.

    Hashing is gated on `verify_digest` (set by `--strict`) because a real
    agent .sif is 3-5 GB: seconds here, potentially much worse on a cluster
    parallel filesystem, and plain `doctor` is run casually. What plain
    `doctor` must NOT do is imply a check it skipped, so it reports whether a
    digest is recorded and says the comparison was not made.
    """
    size_mib = sif_path.stat().st_size // 1024 // 1024
    check = f"image.{plugin_name}.apptainer"

    from botainer.plugins import provenance as _prov
    from botainer.state import dir as _sd
    try:
        paths = _sd.ensure_user_state_dir(create_if_missing=False)
        entries = _prov.read_lock(paths.installed_lock_path)
    except Exception:
        entries = []
    marker = next(
        (e.image_digest for e in entries
         if e.name == plugin_name and _prov.is_apptainer_marker(e.image_digest)),
        None,
    )

    if not marker:
        # The launcher's documented fail-open case: no baseline, so it
        # proceeds. Say that, rather than printing a tick that reads as
        # "verified".
        return Finding(
            "info", check,
            f"{sif_path} built ({size_mib} MiB); no recorded digest, so "
            f"nothing will verify it",
        )

    if not verify_digest:
        return Finding(
            "info", check,
            f"{sif_path} built ({size_mib} MiB); digest recorded, NOT checked "
            f"here (re-run with --strict to compare)",
        )

    recorded = _prov.parse_apptainer_marker(marker)
    recorded_path = _prov.apptainer_marker_path(marker)
    try:
        actual = _prov.sha256_file(sif_path)
    except OSError as exc:
        return Finding(
            "err", check,
            f"{sif_path} is unreadable: {exc}",
            "Check permissions, or rebuild the image.",
        )
    if actual != recorded:
        # THE MISMATCH IS THE ERROR. Naming the recorded path here matters when
        # the two differ: "the record is for <other>, and <this> was resolved
        # instead" is actionable, where a bare hash comparison is not.
        where = ""
        if recorded_path and Path(recorded_path) != Path(sif_path):
            where = (f" The record is for {recorded_path}, and {sif_path} was "
                     f"resolved instead.")
        return Finding(
            "err", check,
            f"{sif_path} sha256 {actual[:16]}… does NOT match the digest "
            f"recorded at build time ({recorded[:16]}…).{where} "
            f"{_WHO_CHECKS_THE_DIGEST}",
            f"If you rebuilt it: botainer hpc build {plugin_name} --force. "
            f"If you replaced it on purpose (built elsewhere and copied it "
            f"in): botainer image forget {plugin_name}.",
        )
    if recorded_path and not Path(recorded_path).exists():
        # A RECORDED PATH THAT IS GONE IS NOT AN ERROR WHEN THE HASH MATCHES, and
        # this used to be one — `err` + a remedy whose first option is a 10-20
        # minute rebuild of a multi-GiB image, for an install that works.
        #
        # Reach it by MOVING or COPYING $MY_BOTAINER, which is documented and
        # supported: `cp -a`/`mv` copies the bytes, so the file at the
        # conventional name in the new root has the recorded digest BY
        # CONSTRUCTION — and every resolver (session, dispatched job, and now the
        # hpc-launcher, which no longer prefers the recorded path) runs exactly
        # that file. The justification written here was that the launcher took the
        # recorded path ahead of the conventional filename; that has stopped being
        # true, so the finding stopped being true with it.
        #
        # It is still worth SAYING, because a user reading `image list --verify`
        # should know the record names a path that is not there — but as a fact,
        # at `info`, not as a fault whose cheap escape (`image forget`) turns off
        # the only integrity check this image has. Measured by a refuting review:
        # that escape leaves "no recorded digest, so nothing will verify it",
        # permanently, for a state that was never broken.
        return Finding(
            "info", check,
            f"{sif_path} built ({size_mib} MiB); sha256 matches what was "
            f"recorded, though the record names {recorded_path}, which is no "
            f"longer there (a moved or copied state root does this). Every "
            f"launch path resolves the file above.",
        )
    return Finding(
        "ok", check,
        f"{sif_path} built ({size_mib} MiB); sha256 matches what was recorded",
    )


def collect_image_findings(verify_digest: bool = False) -> list[Finding]:
    """Check whether the bundled agent images are actually built.

    Two paths:
    - Docker available: check `docker image inspect botainer/<plugin>:0.1`.
    - Apptainer available (HPC): glob ${state_dir}/images/<plugin>.sif AND
      common build locations next to the plugin source.

    HPC review F6 caught the gap: doctor used to return ok with no .sif
    built, then `botainer start` would refuse.
    """
    findings: list[Finding] = []
    docker = shutil.which("docker")
    apptainer = shutil.which("apptainer") or shutil.which("singularity")
    if not docker and not apptainer:
        return findings
    from botainer.plugins.lifecycle import list_installed
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    for p in list_installed():
        dockerfile = p.plugin_dir / "Dockerfile"
        sif_def = p.plugin_dir / f"{p.name}.def"
        if not (dockerfile.exists() or sif_def.exists()):
            continue
        tag = f"botainer/{p.name}:0.1"
        # Report per RUNTIME, not per plugin. The docker branch used to
        # `continue`, so on a host with BOTH runtimes a plugin buildable both
        # ways got a single docker verdict and its .sif was never examined —
        # while `start --runtime apptainer` would refuse that unexamined image.
        # `emitted` replaces both early exits: the missing-image branch below
        # is the fallback for "no runtime said anything", which is what those
        # `continue`s were really expressing.
        emitted = False
        if docker and dockerfile.exists():
            result = subprocess.run(
                ["docker", "image", "inspect", "-f", "{{.Id}}", tag],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                image_id = result.stdout.strip()[:19]
                findings.append(Finding(
                    "ok", f"image.{p.name}.docker", f"{tag} built ({image_id})"
                ))
                emitted = True
        # Check Apptainer if available.
        if apptainer and sif_def.exists():
            # ONE OWNER for "which .sif is actually here", shared with
            # `botainer image list`. This walk used to be inline, and
            # `image list` looked only at the canonical path — so the two
            # commands disagreed about whether a file existed while agreeing
            # about its digest. See `Paths.find_apptainer_sif`.
            candidate = paths.find_apptainer_sif(p.name, p.plugin_dir)
            if candidate is not None:
                findings.append(_apptainer_image_finding(
                    p.name, candidate, verify_digest=verify_digest))
            else:
                findings.append(Finding(
                    "warn", f"image.{p.name}.apptainer",
                    f"{p.name} not built",
                    f"run `botainer image build {p.name} --runtime apptainer`.",
                ))
            emitted = True
        if emitted:
            continue
        # Default: image missing. Distinguish "no usable build path"
        # (docker-only plugin on apptainer-only host) from "image just
        # not built yet on a runtime that could build it."
        if not docker and not sif_def.exists():
            findings.append(Finding(
                "info", f"image.{p.name}",
                "no apptainer recipe; cannot build on this host",
                f"Plugin has a Dockerfile but no `{p.name}.def`. "
                "Authoring the .def is a known gap, tracked internally.",
            ))
        else:
            cmd = f"botainer image build {p.name}"
            if not docker and apptainer:
                cmd += " --runtime apptainer"
            findings.append(Finding(
                "warn", f"image.{p.name}",
                f"{tag} not built",
                f"run `{cmd}`.",
            ))
    return findings


def collect_auth_findings() -> list[Finding]:
    """Auth-mode awareness: are credentials in good shape?

    Doesn't peek at credential contents; reports whether a credential file
    exists for each agent family (per-project and host-wide), and flags
    credential-shaped env vars in the shell environment.

    THE DOCSTRING USED TO SAY THAT AND THE CODE DID NOT (fixed,
    found by walking the road from a clean install). In the commonest first-run
    failure — `setup` and `init` done, not logged in — `doctor` exited 0 and
    `doctor --auth-only` printed a single green tick about shell env vars.
    `botainer auth status`, in the same directory, reported the problem
    correctly and named the fix. This is the command the product points people
    at when things break, so a confident false negative here is worse than no
    check at all.

    It calls `auth.collect_auth_rows` — the same function `auth status`
    renders — rather than re-deriving. Two readers of one fact with nothing
    comparing them is this project's most-repeated defect shape.
    """
    findings: list[Finding] = []

    # Credential presence, per family.
    try:
        from botainer.cli import _common as _c
        from botainer.cli.auth import _family_to_agent_name, collect_auth_rows
        rows = collect_auth_rows(_c.find_project_root())
    except Exception as exc:                                   # noqa: BLE001
        rows = []
        findings.append(Finding(
            "warn", "auth.creds",
            f"could not determine credential state ({exc})",
            "Run `botainer auth status` directly for the detail.",
        ))
    for row in rows:
        fam = row["family"]
        # `--agent` takes the AGENT name (claude/codex), not the family
        # (anthropic/openai). Printing `--agent anthropic` would hand the user
        # a command that fails — the defect class where a message names
        # something that does not exist.
        agent_flag = _family_to_agent_name(fam)
        mode = row["active_mode"] or ""
        # Which store this project actually reads decides which absence matters.
        # In shared/broker the host-wide file is the one that counts; in
        # isolated it is the per-project one. Reporting the wrong store's
        # absence is how a check becomes noise.
        if mode in ("shared", "broker"):
            present, path = row["shared_creds_present"], row["shared_creds_path"]
            login = f"botainer auth login --shared --agent {agent_flag}"
        elif mode == "isolated":
            present, path = (row["per_project_creds_present"],
                             row["per_project_creds_path"])
            login = f"botainer auth login --isolated --agent {agent_flag}"
        else:
            continue        # no variant of this family enabled here — not our business
        if present:
            state = row["shared_creds_state"] if mode != "isolated" else row["per_project_creds_state"]
            detail = row["shared_creds_detail"] if mode != "isolated" else row["per_project_creds_detail"]
            if state == "expired":
                findings.append(Finding(
                    "warn", f"auth.creds.{fam}",
                    f"{fam}: credential present but EXPIRED ({detail})",
                    f"Log in again:\n    {login}",
                ))
            else:
                findings.append(Finding(
                    "ok", f"auth.creds.{fam}",
                    f"{fam}: {mode} credential present ({state})"))
        else:
            findings.append(Finding(
                "warn", f"auth.creds.{fam}",
                f"{fam}: no {mode} credential — this project cannot authenticate",
                f"Expected at: {path}\nRun (on this host, in your shell):\n"
                f"    {login}",
            ))
    # Shell env credential leaks (defense-in-depth: even if config.yaml
    # is clean, the user's shell might have ANTHROPIC_API_KEY set).
    import os as _os

    from botainer.core import credential_leak_check
    shell_leaks = credential_leak_check.detect_credential_env_keys(
        dict(_os.environ)
    )
    if shell_leaks:
        findings.append(Finding(
            "info", "auth.shell_env",
            f"shell has credential-shaped vars: {sorted(shell_leaks)[:5]}",
            "These are in YOUR shell, not in any container. If you keep "
            "them in your shell, that's fine; just confirm you didn't "
            "accidentally export them in .botainer/config.yaml.",
        ))
    else:
        findings.append(Finding(
            "ok", "auth.shell_env", "no credential-shaped vars in shell"
        ))
    return findings


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="JSON output for CI / IDE.")
@click.option("--auth-only", is_flag=True, help="Show only auth findings.")
@click.option("--strict", is_flag=True,
              help="Warnings cause exit nonzero, and each built image's "
                   "sha256 is compared to what was recorded — that reads the "
                   "whole .sif (GBs), so it is slower.")
@handle_refusals
def doctor(as_json: bool, auth_only: bool, strict: bool) -> None:
    """Diagnose runtime + state-dir + plugin issues on this host.

    Exits 0 unless an actionable error is found (or --strict + warnings).
    """
    if auth_only:
        findings = collect_auth_findings()
    else:
        findings = collect_findings(for_setup=False)
        findings.extend(collect_image_findings(verify_digest=strict))
        findings.extend(collect_auth_findings())
    if as_json:
        click.echo(render_findings_json(findings))
    else:
        render_findings(findings)
    if any(f.is_actionable() for f in findings):
        sys.exit(1)
    if strict and any(f.severity == "warn" for f in findings):
        sys.exit(1)
