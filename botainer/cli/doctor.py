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
    # Deploy integrity: a correct repo can still produce a broken install if a
    # copy stripped permissions. Grace hit exactly that.
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

    # Install-introspection: where is the running code coming from?
    # This is the diagnostic that would have ended the two-clone
    # debugging episode in an internal debugging note in seconds.
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
    """WHICH botainer is actually running, and is anything shadowing it?

    Built after an hour was lost to this exact question. A dev
    container had BOTH an editable install (a .pth pointing at the checkout)
    and a plain copied install in site-packages. The copy shadows the .pth, so
    `import botainer` got code SEVEN WEEKS OLD, while every surface — `pip
    list`, `pip show`, the version string — reported the editable install and
    looked correct. Tests run from the checkout passed; the CLI ran the copy.

    The symptom is the worst kind: a fix is applied, committed, verified by
    tests, and then "doesn't work", because the thing being run is not the
    thing being edited. Nothing in botainer could answer "which install is
    live?", so answering it meant hand-inspecting site-packages. That is a
    missing capability, not a user error (CLAUDE.md).

    Pure on purpose: all the environment probing happens in
    `collect_install_findings`, so the DECISION logic is unit-testable without
    a shadowed install to hand.
    """
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
    """A `botainer` on PATH whose shebang names a deleted interpreter.

    Same evening, same container: the console script pointed at a venv under
    /tmp that no longer existed, so running `botainer` gave
    "bad interpreter: no such file or directory" — which reads like botainer is
    broken rather than like the launcher script is stale.
    """
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
    """Are the INSTALLED plugins' hooks actually runnable on THIS machine?

    Grace,: `start --agent codex` refused with "hook script not
    executable: .../agent-codex-broker/hooks/start_broker.py". Two hooks had
    shipped without the executable bit, so codex broker mode had never worked
    anywhere since the day it was written.

    The repo-side guard is a test on git's recorded mode
    (tests/integration/test_plugin_hook_integrity.py). This is the deploy-side
    half: an rsync that drops permissions, a zip round-trip, a
    `chmod -R` gone wrong, or a copy onto a filesystem that handles modes
    differently all produce the same broken install from a correct repo. Only a
    check ON the target machine can see that — which is the whole reason the
    failure surfaced on a cluster and not here.

    Reported by doctor rather than only at session start so the user learns
    their install is broken while they are already diagnosing, instead of at
    the moment they wanted to start work.
    """
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
            elif not os.access(path, os.X_OK):
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
            f"{len(broken)} hook script(s) are not executable: "
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


def collect_image_findings() -> list[Finding]:
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
    images_dir = paths.root / "images"
    for p in list_installed():
        dockerfile = p.plugin_dir / "Dockerfile"
        sif_def = p.plugin_dir / f"{p.name}.def"
        if not (dockerfile.exists() or sif_def.exists()):
            continue
        tag = f"botainer/{p.name}:0.1"
        # Check Docker first if available.
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
                continue
        # Check Apptainer if available.
        if apptainer and sif_def.exists():
            # Accept BOTH naming conventions because they exist in the
            # codebase today: `botainer image build` writes the prefixed
            # form, `botainer hpc build` writes the unprefixed one. Drift
            # tracked in DN-036 ("unify .sif naming").
            sif_candidates = [
                images_dir / f"botainer-{p.name}.sif",
                images_dir / f"{p.name}.sif",
                p.plugin_dir / f"botainer-{p.name}.sif",
                p.plugin_dir / f"{p.name}.sif",
            ]
            for candidate in sif_candidates:
                if candidate.exists():
                    findings.append(Finding(
                        "ok", f"image.{p.name}.apptainer",
                        f"{candidate} built ({candidate.stat().st_size // 1024 // 1024} MiB)",
                    ))
                    break
            else:
                findings.append(Finding(
                    "warn", f"image.{p.name}.apptainer",
                    f"{p.name} not built",
                    f"run `botainer image build {p.name} --runtime apptainer`.",
                ))
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
@click.option("--strict", is_flag=True, help="Warnings cause exit nonzero.")
@handle_refusals
def doctor(as_json: bool, auth_only: bool, strict: bool) -> None:
    """Diagnose runtime + state-dir + plugin issues on this host.

    Exits 0 unless an actionable error is found (or --strict + warnings).
    """
    if auth_only:
        findings = collect_auth_findings()
    else:
        findings = collect_findings(for_setup=False)
        findings.extend(collect_image_findings())
        findings.extend(collect_auth_findings())
    if as_json:
        click.echo(render_findings_json(findings))
    else:
        render_findings(findings)
    if any(f.is_actionable() for f in findings):
        sys.exit(1)
    if strict and any(f.severity == "warn" for f in findings):
        sys.exit(1)
