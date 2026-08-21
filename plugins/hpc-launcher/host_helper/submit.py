#!/usr/bin/env python3
"""hpc-launcher submit — write a sbatch script and submit it.

Easy / clear / transparent / clean (per user direction):
- ONE command. No subcommand chain.
- `--dry-run` shows the exact sbatch script (transparent).
- Auto-detects cluster defaults via ~/.botainer/cluster.yaml. Unknown cluster
  → refuses cleanly with structured error.
- Submission modes:
    submit (default) — fresh sbatch
    attach           — `srun --jobid=<X> --overlap`
    here             — run inside current allocation (no sbatch)

Per codex review 45:
  - Confirmation gate runs on the LOGIN NODE before sbatch (45#4). The
    in-container `botainer start` gets --yes so it doesn't block in the
    batch context where no human is present.
  - After successful `sbatch`, stdout is parsed for the jobid and a
    "next commands" hint is printed (45#5).
  - Documented flag names match the actual CLI (45#2). A small set of
    Slurm-style aliases is accepted and normalized.
"""
from __future__ import annotations

import argparse
import os
import re as _re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from _common import SubmissionPlan, have_slurm, make_plan  # type: ignore[import-not-found]

# Sharp-edges F7 + insecure-defaults L9: validate sbatch-bound strings
# to prevent newline injection into #SBATCH directives. Validate
# numeric ranges to prevent --time 0 (= site default, surprise),
# --gpus -1 (= weird sbatch behavior), etc.
_SBATCH_TOKEN_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


def _positive_int(name: str, lower: int = 1):
    def _check(s: str) -> int:
        try:
            v = int(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name}: must be an integer") from None
        if v < lower:
            raise argparse.ArgumentTypeError(
                f"{name}: must be >= {lower} (got {v}). Use a positive "
                f"value; 0 is unsafe (some sbatch configs treat 0 as 'site "
                f"default cap', which may be days)."
            )
        return v
    return _check


def _gpu_count(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError("--gpus: must be an integer >= 0") from None
    if v < 0:
        raise argparse.ArgumentTypeError("--gpus: must be >= 0 (0 = no GPU)")
    return v


def _sbatch_token(name: str):
    def _check(s: str) -> str:
        if not _SBATCH_TOKEN_RE.fullmatch(s):
            raise argparse.ArgumentTypeError(
                f"{name}: {s!r} contains characters outside [A-Za-z0-9._-]. "
                f"This prevents newline injection into the sbatch script."
            )
        return s
    return _check


_TIME_HHMMSS_RE = _re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
_TIME_HM_RE = _re.compile(r"^(\d{1,3}):([0-5]\d)$")
_TIME_SUFFIX_RE = _re.compile(r"^(\d+)([hHmM])$")


def _parse_time_to_minutes(s: str) -> int:
    """Accept botainer-native minutes (`120`), Slurm-style HH:MM:SS or
    HH:MM, or `2h`/`30m` suffix forms. Normalize to int minutes.

    Codex 45#2: docs were showing Slurm spelling that the CLI rejected.
    Now we accept the common Slurm shapes (in addition to plain
    integer minutes) and normalize internally. Refuse on anything else.
    """
    s = s.strip()
    if s.isdigit():
        v = int(s)
        if v < 1:
            raise argparse.ArgumentTypeError(
                "--time: must be >= 1 minute"
            )
        return v
    m = _TIME_HHMMSS_RE.fullmatch(s)
    if m:
        h, mm, ss = (int(g) for g in m.groups())
        total_seconds = h * 3600 + mm * 60 + ss
        if total_seconds < 60:
            raise argparse.ArgumentTypeError(
                f"--time={s!r} parses to <1 minute; sbatch needs at least 1"
            )
        # Round UP so users asking for 1:30:45 get 91 minutes, not 90.
        return (total_seconds + 59) // 60
    m = _TIME_HM_RE.fullmatch(s)
    if m:
        h, mm = (int(g) for g in m.groups())
        return h * 60 + mm
    m = _TIME_SUFFIX_RE.fullmatch(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if n < 1:
            raise argparse.ArgumentTypeError("--time: must be >= 1")
        return n * 60 if unit == "h" else n
    raise argparse.ArgumentTypeError(
        f"--time={s!r}: expected integer minutes (e.g. 120), HH:MM:SS "
        f"(e.g. 02:00:00), HH:MM (02:00), or a suffix form (2h, 90m)."
    )


def _parse_mem_to_gb(s: str) -> int:
    """Accept botainer-native GB integer or Slurm-style memory string
    (`16G`, `16000M`, `16GB`). Normalize to int GB."""
    s = s.strip()
    if s.isdigit():
        v = int(s)
        if v < 1:
            raise argparse.ArgumentTypeError(
                "--memory-gb: must be >= 1"
            )
        return v
    m = _re.fullmatch(r"(\d+)\s*(GB?|MB?|TB?)", s, _re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("t"):
            return n * 1024
        if unit.startswith("g"):
            return n
        if unit.startswith("m"):
            # Round UP to nearest GB; surface integer-only contract.
            if n < 1024:
                return 1
            return (n + 1023) // 1024
    raise argparse.ArgumentTypeError(
        f"--memory-gb={s!r}: expected integer GB (e.g. 16), or Slurm-style "
        f"`16G`, `16000M`, `1T`."
    )


def _parse_gres_to_gpus(s: str) -> tuple[int, str | None]:
    """Parse Slurm-style `gpu:N` or `gpu:type:N` → (count, type | None)."""
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 2 and parts[0].lower() == "gpu" and parts[1].isdigit():
        return int(parts[1]), None
    if len(parts) == 3 and parts[0].lower() == "gpu" and parts[2].isdigit():
        gpu_type = parts[1]
        if not _SBATCH_TOKEN_RE.fullmatch(gpu_type):
            raise argparse.ArgumentTypeError(
                f"--gres: gpu type {gpu_type!r} contains disallowed chars"
            )
        return int(parts[2]), gpu_type
    raise argparse.ArgumentTypeError(
        f"--gres={s!r}: expected `gpu:N` or `gpu:<type>:N` (e.g. gpu:a100:2)"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="botainer hpc submit")
    p.add_argument("--mode", choices=("submit", "attach", "here"), default=None,
                   help="Override the submission_mode from config")
    p.add_argument("--jobid", type=_sbatch_token("--jobid"), default=None,
                   help="Existing jobid for --mode=attach")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the sbatch script that would be submitted; do not submit")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the login-node capability-summary confirmation and submit")
    p.add_argument("--partition", type=_sbatch_token("--partition"),
                   default=None, help="Override Slurm partition")
    p.add_argument("--account", type=_sbatch_token("--account"),
                   default=None, help="Override Slurm account")
    # --time: minutes OR HH:MM:SS OR Hh / Mm. Codex 45#2: docs were
    # advertising `02:00:00`; the CLI now accepts both forms.
    p.add_argument("--time", "--time-minutes", dest="time_minutes",
                   type=_parse_time_to_minutes, default=None,
                   help="Time (integer minutes, HH:MM:SS, HH:MM, 2h, 90m)")
    # --cpus / --cpus-per-task alias.
    p.add_argument("--cpus", "--cpus-per-task", dest="cpus",
                   type=_positive_int("--cpus"),
                   default=None, help="cpus-per-task (positive integer)")
    # --memory-gb accepts plain int OR Slurm `16G` / `16000M`.
    p.add_argument("--memory-gb", "--mem", dest="memory_gb",
                   type=_parse_mem_to_gb, default=None,
                   help="Memory (integer GB, or Slurm-style 16G / 16000M)")
    p.add_argument("--gpus", type=_gpu_count, default=None,
                   help="GPU count (>= 0)")
    p.add_argument("--gpu-type", type=_sbatch_token("--gpu-type"),
                   default=None, help="GPU type (e.g. a100, v100)")
    # --gres alias: parses `gpu:a100:2` into --gpus + --gpu-type.
    p.add_argument("--gres", dest="gres", default=None,
                   help="Slurm-style `gpu:N` or `gpu:<type>:N` (alias for "
                        "--gpus + --gpu-type)")
    p.add_argument("--image", default=None, help="Override apptainer .sif path")
    return p.parse_args(argv)


_SBATCH_JOBID_RE = _re.compile(r"Submitted batch job\s+(\d+)")

# Network modes apptainer cannot enforce (it shares the host network namespace
# at v0.1 — no in-container isolation). THIS IS THE ONLY REFUSAL: an earlier
# comment here credited a second one in the inner `botainer start
# --in-container` path, but compose-at-submit (#52) deleted that launcher and
# `--in-container` is not a flag any more. Believing in a defence that no longer
# exists is how the surviving one gets weakened as "redundant", so: refusing at
# SUBMIT time is what stops a doomed job, and it also means the user gets the
# failure on the login node instead of after a queue wait and a wasted compute
# allocation (re-audit #9). Match the NetworkMode value strings.
_UNENFORCEABLE_APPTAINER_NETWORK = frozenset({"none", "endpoint-ip-allowlist"})


def _refuse_unenforceable_network(plan: SubmissionPlan) -> int:
    """Return non-zero (refuse) if the project requests a network mode apptainer
    cannot enforce. Fail-fast parity with the in-container refusal: never queue a
    job that the compute-node launcher will refuse on arrival."""
    if plan.network_mode in _UNENFORCEABLE_APPTAINER_NETWORK:
        sys.stderr.write(
            f"hpc-launcher: refusing — network.mode={plan.network_mode!r} cannot "
            f"be enforced under apptainer (it shares the host network namespace; "
            f"no isolation at v0.1). The compute-node launcher refuses this too, "
            f"so submitting would waste a queue wait + allocation. Either set "
            f"network.mode=internet (accept host network) or configure cluster-"
            f"side network namespaces.\n"
        )
        return 4
    return 0


def _refuse_proxy_on_hpc(plan: SubmissionPlan) -> int:
    """Refuse proxy auth (agent-*-proxy) on the HPC sbatch path (submit/here).

    Codex Priority-A HIGH: the proxy-mode credential withholding on
    the sbatch cage (commit 0f5a23c) is INCOMPLETE. Skipping the explicit
    `/shared-auth/agent-<name>` + `/home/agent/.<name>` binds does NOT hide real
    credentials that live under the RW `state/<uuid>` bind at
    `data/agent-<name>/profiles/<profile>/.credentials.json` (a prior isolated/
    shared session's creds) — so the consent's "creds stay host-side" promise is
    FALSE. AND the proxy socket isn't routed onto the outer sbatch argv (#160),
    so proxy auth is non-functional on HPC regardless.

    Until the state bind is narrowed to exclude credential profiles (tracked in
    S3), refuse rather than run a broken + leaky
    mode. Docker/direct proxy is unaffected — there the proxy plugin variant is
    enabled INSTEAD of the cred plugin, so no credential bind is contributed."""
    if any(p.startswith("agent-") and p.endswith("-proxy") for p in plan.plugins_enabled):
        sys.stderr.write(
            "hpc-launcher: refusing — proxy auth (agent-*-proxy) is NOT supported "
            "on the HPC sbatch path at v0.1. The proxy socket isn't routed onto "
            "the compute-node cage (auth would not work), and the read-write "
            "per-project state bind still exposes real credentials under "
            "state/<uuid>/data/agent-<name>/ (the 'credentials stay host-side' "
            "guarantee cannot be met yet). Use shared or isolated auth on HPC:\n"
            "  botainer auth use isolated --family <family>   # or: shared\n"
            "DO NOT LIFT THIS until the state/<uuid> bind stops exposing\n"
            "data/agent-*/profiles/*/.credentials.json — otherwise the consent\n"
            "screen would say creds stay host-side while a prior session's real\n"
            "credentials are readable in-cage. Socket routing alone is not enough.\n"
        )
        return 2
    return 0


def _print_login_node_confirmation(plan: SubmissionPlan) -> None:
    """Print the SLURM SCHEDULER resources for the login-node consent surface.

    The compute node has no TTY, so submit-time (login node) is where the human
    confirms. This shows only the scheduler knobs (partition/account/time/
    cpus/mem/gpus/image/project); the SECURITY posture (binds/env/network/auth)
    is disclosed right after this by `capability_summary.print_and_maybe_confirm`
    on the ACTUAL composed spec -- accurate by construction, replacing the old
    hand-mirrored approximation this function used to carry (and its now-stale
    git/proxy/module caveats, which the real capability summary supersedes).
    """
    sys.stderr.write(
        "\n# About to submit this sbatch job (scheduler resources):\n"
        f"#   partition:  {plan.partition or '(none -- sbatch will refuse)'}\n"
        f"#   account:    {plan.account or '(none -- Slurm uses your default account, or rejects if your cluster requires an explicit one)'}\n"
        f"#   time:       {plan.time_minutes} min\n"
        f"#   cpus:       {plan.cpus}\n"
        f"#   memory:     {plan.memory_gb} GB\n"
        f"#   gpus:       {plan.gpus}"
        + (f" (type={plan.gpu_type})" if plan.gpu_type else "")
        + "\n"
        f"#   image:      {plan.apptainer_image}\n"
        f"#   project:    {plan.project_root}\n"
        "#   (security posture -- binds / network / auth -- shown below)\n"
    )
    # Review MEDIUM-3: compose-at-submit captures the `module load` env + derives
    # software-root binds on THIS LOGIN NODE. A module that only resolves on the
    # compute node (GPU/arch/partition-gated) won't be captured here, so its
    # PATH/software won't reach the container by name. Warn when modules are in
    # play so a "command not found on the compute node" is diagnosable, not
    # mysterious. (Inherent to compose-at-submit's shared-FS/homogeneous-node
    # model; the direct `--mode=here` path inside an salloc composes on the
    # compute node and avoids it.)
    if "hpc-modules" in plan.plugins_enabled:
        sys.stderr.write(
            "#   ⚠ modules: the `module load` env + software-root binds are\n"
            "#     captured on THIS login node. A module that only resolves on\n"
            "#     the compute node (GPU/arch/partition-gated) will NOT be\n"
            "#     delivered by name — load it inside the job, or use\n"
            "#     `hpc --mode=here` from within an salloc on the target node.\n"
        )


def main() -> int:
    args = parse_args(sys.argv[1:])
    project_root = Path(os.environ.get("BOTAINER_PROJECT_ROOT", os.getcwd()))
    plan = make_plan(project_root)
    # Apply one-shot CLI overrides on top of config + cluster defaults.
    overrides: dict[str, object] = {}
    if args.mode:
        overrides["submission_mode"] = args.mode
    for name in ("partition", "account", "time_minutes", "cpus",
                 "memory_gb", "gpus", "gpu_type"):
        v = getattr(args, name, None)
        if v is not None:
            overrides[name] = v
    if args.gres is not None:
        gres_gpus, gres_type = _parse_gres_to_gpus(args.gres)
        # --gpus / --gpu-type win over --gres if explicitly set.
        overrides.setdefault("gpus", gres_gpus)
        if gres_type is not None:
            overrides.setdefault("gpu_type", gres_type)
    if args.image:
        overrides["apptainer_image"] = args.image
    if overrides:
        plan = plan.__class__(**{**plan.__dict__, **overrides})

    # #160: derive module software-root binds HERE (not in make_plan, which
    # stays side-effect-free — adversarial-review B2), now that the effective
    # mode + dry-run intent are known. fail_loud only on a REAL submit/here
    # launch; a --dry-run preview or an attach reattach must not be aborted by
    # a transient login-node module hiccup. The binds (if any) go onto the
    # frozen plan so to_apptainer_argv / the rendered sbatch script carry them.
    # Re-audit #9: fail-fast on an unenforceable network mode for ALL launch
    # modes (submit/here/attach all exec apptainer, which shares the host
    # network namespace). The compute-node launcher refuses these anyway; doing
    # it here avoids a wasted queue wait + allocation. Dry-run still previews
    # (the confirmation caveat marks it REFUSED) so config can be inspected.
    # Re-audit #13 (LOW): refuse BEFORE running the module-load hook below — that
    # hook spawns a subprocess + mkdirs a state dir, all wasted if we then refuse.
    if not args.dry_run:
        net_rc = _refuse_unenforceable_network(plan)
        if net_rc != 0:
            return net_rc
        # Codex Priority-A HIGH: proxy auth can't withhold creds from the RW
        # state bind on HPC (and its socket isn't routed) — refuse submit/here.
        proxy_rc = _refuse_proxy_on_hpc(plan)
        if proxy_rc != 0:
            return proxy_rc

    # Recon T-D: pre-submit credential-presence gate. On submit/here (a real
    # launch), if the active agent has no credential on the host in either
    # shared-auth or the per-project profile dir, the shared-auth bind is
    # SILENTLY skipped and the job fails to authenticate on the compute node
    # after a queue wait. Refuse early with an actionable message (mirrors the
    # time=0 / partition fail-fast gates). Skip for PROXY mode (agent-*-proxy
    # deliberately mounts no credential — the broker injects auth) and for
    # attach (reattaches an existing job, no fresh credential needed).
    if not args.dry_run and plan.submission_mode in ("submit", "here") and plan.agent_name:
        _is_proxy = any(
            p.startswith("agent-") and p.endswith("-proxy")
            for p in plan.plugins_enabled
        )
        if not _is_proxy:
            from _common import _has_agent_credential  # type: ignore[import-not-found]
            if not _has_agent_credential(
                plan.state_root, plan.project_uuid, plan.agent_name, plan.profile
            ):
                sys.stderr.write(
                    f"refused: no credential found for agent-{plan.agent_name!r} "
                    f"(profile {plan.profile!r}). Run `botainer auth login` first "
                    f"— without it the credential bind is silently skipped and the "
                    f"job would fail to authenticate on the compute node after a "
                    f"queue wait.\n"
                    f"  shared mode:   looked in "
                    f"{plan.state_root}/shared-auth/agent-{plan.agent_name}/\n"
                    f"  isolated mode: looked in "
                    f"{plan.state_root}/state/{plan.project_uuid}/data/"
                    f"agent-{plan.agent_name}/profiles/{plan.profile}/\n"
                )
                return 2

    # Refuse on unknown-cluster time=0 (per detect_cluster: 0 means
    # "no defaults; user must specify"). Better message than letting
    # sbatch refuse on `--time=00:00:00`.
    if plan.submission_mode == "submit" and plan.time_minutes <= 0:
        sys.stderr.write(
            "hpc-launcher: refusing to submit with time=0. Pass `--time <min>` "
            "(e.g. `--time 120`), set `plugins.hpc-launcher.time_minutes` in "
            ".botainer/config.yaml, or run `botainer hpc setup` to write a "
            "cluster profile with default_time_minutes.\n"
        )
        return 4

    # ── Compose-at-submit (DN-004) ─────────────
    # Compose the FULL apptainer session ON THIS LOGIN NODE and render the
    # adapter argv to bake into the sbatch script. The compute-node container
    # then execs the AGENT entrypoint (which IS in the .sif) — never `botainer
    # start --in-container` on a binary that isn't there (the FATAL this fixes).
    # The module software-root binds + inner-load binds + captured module env
    # now flow via the SAME Flow-1 host_pre_launch hooks the direct apptainer
    # path uses (run_host_pre_launch_hooks → ApptainerAdapter), so the old
    # standalone Flow-2 derivation is gone. Shared filesystem → login-node paths
    # are valid on the compute node.
    from botainer.core import composition  # type: ignore[import-not-found]
    from botainer.core.refusal import Refused  # type: ignore[import-not-found]
    from botainer.inspect import capability_summary  # type: ignore[import-not-found]
    try:
        spec, agent_argv = composition.compose_agent_exec_for_hpc(
            project_root, image_override=(plan.apptainer_image or None),
        )
    except Refused as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    session_dir = str(Path(spec.state_dir) / "sessions" / spec.session_id)
    plan = plan.__class__(**{
        **plan.__dict__,
        "agent_exec_argv": tuple(agent_argv),
        "session_id": spec.session_id,
        "session_dir": session_dir,
    })

    def _consent(interactive: bool) -> bool:
        """Login-node consent surface (the compute node has no TTY). Show the
        SLURM scheduler resources, then the REAL capability disclosure from the
        COMPOSED spec (binds/env/network/auth) — replacing the old hand-mirrored
        approximation, which is now accurate because we hold the actual spec.
        `interactive` → prompt; otherwise (here/attach) show-only."""
        _print_login_node_confirmation(plan)
        return capability_summary.print_and_maybe_confirm(
            spec, quiet=False, as_json=False,
            auto_yes=(args.yes or not interactive),
        )

    if plan.submission_mode == "submit":
        # HPC-IMPL #7: per-project concurrency cap. Refuse early if
        # `plugins.hpc-launcher.max_concurrent_jobs` is set and this
        # project already has that many jobs queued or running.
        cap_check_rc = _refuse_if_concurrency_cap_exceeded(plan, args.dry_run)
        if cap_check_rc != 0:
            return cap_check_rc
        if not args.dry_run:
            if not _consent(interactive=True):
                sys.stderr.write("hpc-launcher: aborted (no confirmation).\n")
                return 3
            # Pre-bind mkdir: apptainer refuses to launch on a missing bind
            # source (compose already created the state subtree/session dir;
            # this creates the HOST-ONLY SLURM --output dir + the S1 tripwire).
            plan.prepare_host_paths()
        return _do_submit(plan, spec, dry_run=args.dry_run)
    if plan.submission_mode == "attach":
        if not args.dry_run:
            _consent(interactive=False)
            plan.prepare_host_paths()
        return _do_attach(plan, args.jobid, dry_run=args.dry_run)
    if plan.submission_mode == "here":
        if not args.dry_run:
            _consent(interactive=False)
            plan.prepare_host_paths()
        return _do_here(plan, dry_run=args.dry_run)
    sys.stderr.write(f"unknown submission_mode: {plan.submission_mode}\n")
    return 1


def _refuse_if_concurrency_cap_exceeded(
    plan: SubmissionPlan, dry_run: bool
) -> int:
    """HPC-IMPL #7. Read max_concurrent_jobs from the project's
    hpc-launcher plugin config and refuse the submit if this project
    already has that many jobs queued+running on Slurm.

    The cap is per-project (matched by the job-name prefix
    `botainer-<uuid_prefix>`, set by `to_sbatch_argv`). Submit-time
    refusal is better UX than scancel-after-the-fact and prevents
    runaway agents from spinning up unbounded jobs.

    `--dry-run` skips the check (we want to show the script even
    if at-cap so the user can decide what to cancel first).
    """
    if dry_run:
        return 0
    # Re-read the project config since SubmissionPlan doesn't carry it.
    try:
        from _common import load_plugin_config as _load_plugin_config
    except ImportError:
        return 0
    cfg = _load_plugin_config(plan.project_root)
    cap_raw = cfg.get("max_concurrent_jobs")
    if cap_raw is None:
        return 0  # no cap configured; submit freely
    try:
        cap = int(cap_raw)
    except (TypeError, ValueError):
        sys.stderr.write(
            f"hpc-launcher: warning: max_concurrent_jobs={cap_raw!r} is not "
            f"an integer; ignoring the cap.\n"
        )
        return 0
    if cap <= 0:
        sys.stderr.write(
            f"hpc-launcher: max_concurrent_jobs={cap} blocks all submissions. "
            f"Set to a positive integer or remove the cap.\n"
        )
        return 4

    # Count this project's queued+running jobs. The job-name prefix is
    # `botainer-<first-8-of-uuid>` (per to_sbatch_argv).
    name_prefix = f"botainer-{plan.project_uuid[:8]}"
    if not have_slurm():
        # Can't enforce without squeue; let the submit proceed and
        # surface the failure later if it happens.
        return 0
    try:
        result = subprocess.run(
            ["squeue", "-u", os.environ.get("USER", ""),
             "--name", name_prefix, "--noheader", "--format=%i"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        sys.stderr.write(
            "hpc-launcher: warning: squeue check timed out; "
            "concurrency cap not enforced.\n"
        )
        return 0
    if result.returncode != 0:
        return 0  # squeue errored; don't block on a check failure
    current = sum(1 for line in result.stdout.strip().splitlines() if line.strip())
    if current >= cap:
        sys.stderr.write(
            f"hpc-launcher: refusing to submit — this project already has "
            f"{current} job(s) queued/running, at or above the configured "
            f"max_concurrent_jobs={cap}.\n"
            f"\n"
            f"To proceed, either:\n"
            f"  - Cancel an existing job: botainer hpc cancel <jobid>\n"
            f"  - Wait for one to finish: botainer hpc list\n"
            f"  - Raise the cap in .botainer/config.yaml "
            f"(plugins.hpc-launcher.max_concurrent_jobs).\n"
        )
        return 4
    return 0


def _do_submit(plan: SubmissionPlan, spec, *, dry_run: bool) -> int:
    if not plan.partition:
        sys.stderr.write(
            "hpc-launcher: refusing to submit without a partition.\n"
            "  Set plugins.hpc-launcher.partition in .botainer/config.yaml, pass "
            "--partition <X>, or run `botainer hpc setup` (bundled profiles set "
            "one).\n"
        )
        return 4
    # Account is OPTIONAL — do NOT force it. Many clusters give each user a
    # DEFAULT Slurm account, so requiring one was pure friction. to_sbatch_argv
    # omits --account when unset, letting Slurm apply the user's default; if the
    # cluster REQUIRES an explicit account, sbatch rejects synchronously with a
    # clear message (no queue wait, no wasted allocation). The consent surface
    # already discloses account=(none) so the user isn't surprised.
    script = plan.render_sbatch_script()
    if dry_run:
        sys.stdout.write(
            "# hpc-launcher --dry-run: the sbatch script that WOULD be submitted:\n"
        )
        sys.stdout.write(script)
        sys.stdout.write(
            "\n# Next: drop --dry-run to submit. The compute-node container\n"
            "#       execs the agent entrypoint directly (composed on this login\n"
            "#       node) — no botainer runs inside the container.\n"
        )
        return 0
    if not have_slurm():
        sys.stderr.write("hpc-launcher: `sbatch` not on PATH; not on a Slurm cluster.\n")
        return 5
    sessions_dir = plan.state_root / "state" / plan.project_uuid / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    submit_dir = sessions_dir / "_submit-scripts"
    submit_dir.mkdir(exist_ok=True)
    # Independent-read F-9: previous version used `os.times().elapsed`
    # (seconds-since-process-start), which collides if you submit two
    # jobs from one shell within a second. `time_ns()` is monotonic
    # and globally unique enough for filenames.
    script_path = submit_dir / f"submit-{time.time_ns()}.sh"
    script_path.write_text(script, encoding="utf-8")
    os.chmod(script_path, 0o700)
    # Codex 45#5: capture sbatch stdout so we can parse the jobid and
    # tell the user what to run next.
    # Scrub APPTAINERENV_*/SINGULARITYENV_* from the submission env: SLURM's
    # default --export=ALL would propagate them to the compute node, where
    # `apptainer exec --cleanenv` HONORS them (that's their purpose) — so an
    # untrusted repo's direnv `.envrc` inherited on the login node would smuggle
    # env past the cage into the job (sharp-edges MED). Intended env is
    # delivered via the script's --env/--env-file.
    _sub_env = {k: v for k, v in os.environ.items()
                if not k.startswith(("APPTAINERENV_", "SINGULARITYENV_"))}
    completed = subprocess.run(
        ["sbatch", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
        env=_sub_env,
    )
    # Surface sbatch's stdout/stderr in any case so users see the underlying
    # message if it's not the standard "Submitted batch job N".
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        return completed.returncode
    match = _SBATCH_JOBID_RE.search(completed.stdout or "")
    if match:
        jobid = match.group(1)
        # Matches render_sbatch_script's #SBATCH --output line + the
        # outputs_dir mkdir in prepare_host_paths. Security-audit
        # Finding 1: the HOST-ONLY job-output dir (not the container-bound
        # sessions/_outputs) via the shared _job_output_dir helper.
        from _common import _job_output_dir  # type: ignore[import-not-found]
        output_path = _job_output_dir(plan.state_root, plan.project_uuid) / f"slurm-{jobid}.out"
        # Compose-at-submit (design #12): the runtime adapter is NOT invoked on
        # the sbatch path, so record the jobid (+ screen session id when nudge
        # is on) into the composed session record HERE, login-side. `hpc logs`,
        # `hpc stop --all`, and `hpc status` discover the job from this. The
        # compute node writes its hostname into <session_dir>/node itself (the
        # sbatch provenance lines). Best-effort — a record write must not fail
        # an otherwise-successful submit.
        try:
            from botainer.state import session_record  # type: ignore[import-not-found]
            _rec_fields: dict[str, object] = {"slurm_jobid": jobid}
            if plan.nudge_enabled:
                _rec_fields["screen_session_id"] = f"botainer-{jobid}"
            session_record.update_runtime(Path(plan.session_dir), **_rec_fields)
        except Exception as _exc:  # noqa: BLE001 — provenance is best-effort
            sys.stderr.write(
                f"# (note: could not record jobid into the session record: {_exc})\n"
            )
        sys.stdout.write(
            f"\n# Submitted: jobid={jobid}\n"
            f"#   output:   {output_path}  (created when the job runs)\n"
            f"#   script:   {script_path}\n"
            f"#\n"
            f"# Next commands:\n"
            f"#   botainer hpc logs {jobid} -f   # watch output (waits until the job starts)\n"
            f"#   botainer hpc status            # status of your botainer jobs\n"
            f"#   botainer hpc attach --jobid {jobid}   # attach a terminal once it's running\n"
            f"#   scancel {jobid}                # cancel\n"
            f"#   (raw: squeue -u $USER -j {jobid} ; tail -f {output_path})\n"
        )
    else:
        # sbatch's output didn't match the standard pattern. We've already
        # echoed it above; nudge the user to inspect.
        sys.stderr.write(
            "\n# hpc-launcher: could not parse jobid from sbatch stdout. "
            "See the message above; check with `squeue -u $USER`.\n"
        )
    return 0


def _do_attach(plan: SubmissionPlan, jobid: str | None, *, dry_run: bool) -> int:
    jobid = jobid or plan.existing_jobid
    if not jobid:
        sys.stderr.write("hpc-launcher --mode=attach requires --jobid or SLURM_JOB_ID env.\n")
        return 4
    # Consent (posture + resources) already shown by main()'s _consent().
    if plan.nudge_enabled:
        # #56 + cluster-nudge fix: the batch agent runs inside a
        # `screen -dmS botainer-<jobid>` on the compute node (see _common.py's
        # run_section) — the SAME screen `botainer nudge` delivers into. REATTACH
        # to it so what you see IS the running agent (and what you nudge). Pure
        # argv, no shell. If the screen is gone (job ended), screen -r exits
        # cleanly. Only when nudge/screen was NOT enabled do we fall back to a
        # fresh agent (the batch agent had no TTY and already exited).
        #
        # `-r` (resume-only) is LOAD-BEARING and must NOT become `-R`/`-RR`
        # (sharp-edges): on a miss `-R` would CREATE a new, UNCAGED
        # compute-node shell in the allocation. `-r` just errors on a miss.
        argv = ["srun", f"--jobid={jobid}", "--overlap", "--pty",
                "screen", "-r", f"botainer-{jobid}"]
    else:
        argv = ["srun", f"--jobid={jobid}", "--overlap", "--pty"] + plan.to_apptainer_argv()
    if dry_run:
        sys.stdout.write("# hpc-launcher --dry-run (attach):\n")
        sys.stdout.write("  " + " ".join(shlex.quote(a) for a in argv) + "\n")
        return 0
    if not have_slurm():
        sys.stderr.write("hpc-launcher: `srun` not on PATH; not on a Slurm cluster.\n")
        return 5
    return subprocess.run(argv, check=False).returncode


def _do_here(plan: SubmissionPlan, *, dry_run: bool) -> int:
    if plan.existing_jobid is None and not dry_run:
        sys.stderr.write("hpc-launcher --mode=here: not inside a Slurm allocation.\n")
        return 4
    # Consent (posture + resources) already shown by main()'s _consent().
    argv = plan.to_apptainer_argv()
    if dry_run:
        sys.stdout.write("# hpc-launcher --dry-run (here):\n")
        sys.stdout.write("  " + " ".join(shlex.quote(a) for a in argv) + "\n")
        return 0
    return subprocess.run(argv, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
