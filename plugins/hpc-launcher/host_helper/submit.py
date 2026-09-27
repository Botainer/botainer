#!/usr/bin/env python3
"""hpc-launcher submit — write a sbatch script and submit it.

Interface:
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
from typing import NamedTuple
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


def emit_partition_warnings(plan, *, stream) -> None:
    """Write what this partition COSTS to `stream`. Never raises.

    A separate named function rather than an inline block so the emission can
    be driven directly in a test — the first version was buried inside
    `_do_submit`, where the only reachable test path was `--dry-run`, and
    `--dry-run` is precisely the mode that skips the consent prompt this is
    supposed to precede. A test that can only run the mode where the bug is
    absent is not a test of the bug.

    `plan.partition` is the EFFECTIVE partition (config.yaml > flag > cluster
    default); that is why this lives in the helper and not in `botainer hpc
    submit`, which knows only the flag.
    """
    try:
        from botainer.hpc.partition_warnings import (  # type: ignore[import-not-found]
            partition_warnings,
        )
        from botainer.state import cluster_profile as _cp  # type: ignore[import-not-found]

        for line in partition_warnings(plan.partition, _cp.active_profile()):
            stream.write(f"hpc-launcher: {line}\n")
    except Exception:                                            # noqa: BLE001
        pass                            # a warning must never block a launch


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
    # HPC PARITY (queue 118): the three one-shot overrides `botainer start` has.
    # `_sbatch_token` is the same validator the other free-text flags use, so a
    # flag-shaped or control-char value is refused here rather than reaching an
    # #SBATCH line or an apptainer argv. --agent is additionally re-checked by
    # SubmissionPlan.__post_init__ (`_reject_traversal_agent`), because it is
    # interpolated into credential paths.
    p.add_argument("--agent", type=_sbatch_token("--agent"), default=None,
                   help="Run this submission with a different agent")
    # The history sentence is written out here rather than imported: this
    # launcher runs on login nodes where `botainer` may not be importable, which
    # is the whole reason it is a standalone script. A test pins that this help
    # keeps saying it, so it cannot drift back to credentials-only while the
    # four botainer-side commands say more.
    p.add_argument("--auth-mode", type=_sbatch_token("--auth-mode"), default=None,
                   help="One-shot auth-mode override for this submission. "
                        "Selects the agent's whole config directory (credential "
                        "AND history/settings); nothing is carried into it and "
                        "config.yaml is not modified.")
    p.add_argument("--auth-profile", type=_sbatch_token("--auth-profile"),
                   default=None,
                   help="One-shot auth-profile override for this submission. "
                        "Selects the agent's whole config directory (credential "
                        "AND history/settings); nothing is carried into it and "
                        "config.yaml is not modified.")
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


def _refuse_broker_on_hpc(plan: SubmissionPlan) -> int:
    """Refuse broker auth on the sbatch path BEFORE the broker starts.

    Compose already refuses it — but at the wrong moment. The claude broker's
    `pre_session` hook contributes a `unix-socket` bind when the runtime is
    apptainer (`start_broker.py:416,450`), and `_refuse_cross_node_binds` rejects
    exactly that with `unsupported-runtime-feature`. MEASURED with the real
    function on a synthetic spec: a `unix-socket` bind is refused, the same bind
    stored `rw` is NOT — a socket recorded with the wrong mode walks straight
    past this check, which is why the guard below keys on the plugin name rather
    than on a bind the hook has not contributed yet. Codex's broker is loopback
    TCP and is caught by the same check's port rule.

    THE PROBLEM IS THE ORDER. That refusal is at `composition.py:2983`, AFTER
    `run_pre_session_hooks` at `:2974`. So following the old path cost a user:
    config rewritten → broker daemon started → host-side OAuth refresh, which can
    ROTATE the refresh token and log every other holder out → refusal, no
    session. The product's own dry-run text says that rotation is not undone by
    the teardown. `_refuse_proxy_on_hpc` has guarded the proxy mode here since
    the Codex review; broker had no equivalent, which the refuting review of the
    row-70 consent text found while checking where that paragraph renders.

    So this runs beside the proxy guard, before any hook: a mode that cannot work
    here must not first touch the credential it would break.
    """
    brokers = [p for p in plan.plugins_enabled
               if p.startswith("agent-") and p.endswith("-broker")]
    if not brokers:
        return 0
    sys.stderr.write(
        f"hpc-launcher: refusing — broker auth ({', '.join(brokers)}) cannot run "
        f"on the HPC sbatch path at v0.1. The broker talks to the agent over a "
        f"node-local channel (a unix socket, or loopback TCP for codex) and the "
        f"compute node is not the login node, so the cross-node bind check "
        f"refuses the session.\n"
        f"  REFUSED HERE, ON PURPOSE, BEFORE THE BROKER STARTS: letting compose "
        f"refuse it means the broker has already run a host-side OAuth refresh, "
        f"which can ROTATE your refresh token and log your other projects out — "
        f"for a session that then does not launch.\n"
        f"  Broker mode DOES work on a compute node: `salloc ...` then "
        f"`botainer start --runtime apptainer` inside the allocation.\n"
        f"  Or for the sbatch path: `botainer auth use shared` (or `isolated`).\n"
    )
    return 2


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
    # The three go to make_plan, NOT into the `overrides` dict below: --agent
    # has to be known before `_resolve_apptainer_image` picks the .sif, or the
    # job would run one agent's entrypoint out of another agent's image.
    plan = make_plan(
        project_root,
        agent_override=args.agent,
        auth_mode_override=args.auth_mode,
        auth_profile_override=args.auth_profile,
    )
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
        # The guards below judge `plan.plugins_enabled`, which `make_plan` got by
        # ASKING `composition.apply_plugin_overrides` — the same function compose
        # calls. There is no launcher-side derivation left to disagree with it,
        # and no re-asking here: a plan is frozen at make_plan and the answer
        # cannot have changed since, because nothing between has run.
        net_rc = _refuse_unenforceable_network(plan)
        if net_rc != 0:
            return net_rc
        # Codex Priority-A HIGH: proxy auth can't withhold creds from the RW
        # state bind on HPC (and its socket isn't routed) — refuse submit/here.
        proxy_rc = _refuse_proxy_on_hpc(plan)
        if proxy_rc != 0:
            return proxy_rc
        # Beside the proxy guard and for the same reason, except that the cost of
        # being late here is a ROTATED REFRESH TOKEN: compose refuses broker on
        # this path only after the broker hook has started and refreshed.
        broker_rc = _refuse_broker_on_hpc(plan)
        if broker_rc != 0:
            return broker_rc

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
            project_root,
            image_override=(plan.apptainer_image or None),
            agent_override=plan.agent_override,
            auth_mode_override=plan.auth_mode_override,
            auth_profile_override=plan.auth_profile_override,
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

    # #109: what this partition COSTS, said BEFORE the consent prompt.
    #
    # `preemptible` and `exclusive` have been on PartitionSpec since ~40 real
    # sites were transcribed, and until now only the job-profile GENERATOR read
    # them — so the two facts that make a partition choice expensive or
    # destructive were never surfaced where they apply.
    #
    # POSITION IS THE WHOLE POINT, and the first version of this got it wrong:
    # emitted inside `_do_submit`, the line landed AFTER `_consent` had already
    # asked "Launch this session? [y/N]" and been answered. A cost disclosed
    # after the decision is not a disclosure. It sits here, before the mode
    # dispatch, so every path that places work on a partition — submit, here,
    # and attach — shows it, on dry-run as well as for real.
    #
    # Emitted HERE rather than in `botainer hpc submit` because `plan.partition`
    # is the EFFECTIVE value: the CLI knows only the --partition flag, so a
    # partition set in .botainer/config.yaml would have been warned about
    # wrongly or not at all.
    emit_partition_warnings(plan, stream=sys.stderr)

    def _consent(interactive: bool) -> bool:
        """Login-node consent surface (the compute node has no TTY). Show the
        SLURM scheduler resources, then the REAL capability disclosure from the
        COMPOSED spec (binds/env/network/auth) — replacing the old hand-mirrored
        approximation, which is now accurate because we hold the actual spec.
        `interactive` → prompt; otherwise (here/attach) show-only."""
        _print_login_node_confirmation(plan)
        return capability_summary.print_and_maybe_confirm(
            spec, quiet=False, as_json=False,
            # THE FACT ONLY THIS CALLER HAS. The credential paragraph's
            # host-side advice differs here: broker mode is refused on the
            # sbatch path (the unix-socket bind cannot cross nodes) AFTER the
            # broker has already started and possibly rotated the token, so the
            # summary must not send a cluster user to `auth use broker`. It used
            # to infer this from `spec.runtime`, which is also apptainer for
            # `start --runtime apptainer` inside an salloc — where broker works.
            on_sbatch_path=True,
            # The two facts, kept apart. `--yes` is consent; having no
            # terminal is not. Merging them let a show-only here/attach
            # satisfy the first-launch gate on the user's behalf.
            pre_authorised=args.yes, interactive=interactive,
        )

    # Composition invokes pre_session hooks so their bind and environment
    # contributions appear in the generated sbatch script. Those hooks can
    # write state and start host processes, even for a dry run. Until a separate
    # preview-safe hook path exists, every exit without a launch must tear down
    # what it started and disclose these effects. Compose already does this on
    # its cross-node refusal path; the cleanup below covers the other exits.
    _torn_down: list[bool] = []

    def _teardown() -> None:
        # RUN ONCE. Idempotence is what lets the dispatch result be judged at a
        # SINGLE site below instead of at every early return: calling it twice
        # would run this project's post_session hooks twice, and a hook that
        # stops a daemon is not required to survive being told twice.
        if _torn_down:
            return
        _torn_down.append(True)
        try:
            composition.run_post_session_hooks(spec)
        except Exception as exc:      # teardown is best-effort, like compose's
            sys.stderr.write(
                f"hpc-launcher: post_session cleanup after a non-launching "
                f"compose reported: {exc}\n")

    outcome: Outcome
    if plan.submission_mode == "submit":
        # HPC-IMPL #7: per-project concurrency cap. Refuse early if
        # `plugins.hpc-launcher.max_concurrent_jobs` is set and this
        # project already has that many jobs queued or running.
        cap_check_rc = _refuse_if_concurrency_cap_exceeded(plan, args.dry_run)
        if cap_check_rc != 0:
            _teardown()
            return cap_check_rc
        if not args.dry_run:
            if not _consent(interactive=True):
                sys.stderr.write("hpc-launcher: aborted (no confirmation).\n")
                _teardown()
                return 3
            # Pre-bind mkdir: apptainer refuses to launch on a missing bind
            # source (compose already created the state subtree/session dir;
            # this creates the HOST-ONLY SLURM --output dir + the S1 tripwire).
            plan.prepare_host_paths()
        outcome = _do_submit(plan, spec, dry_run=args.dry_run)
    elif plan.submission_mode == "attach":
        if not args.dry_run:
            _consent(interactive=False)
            plan.prepare_host_paths()
        outcome = _do_attach(plan, args.jobid, dry_run=args.dry_run)
    elif plan.submission_mode == "here":
        if not args.dry_run:
            _consent(interactive=False)
            plan.prepare_host_paths()
        outcome = _do_here(plan, dry_run=args.dry_run)
    else:
        sys.stderr.write(f"unknown submission_mode: {plan.submission_mode}\n")
        _teardown()
        return 1
    # THE ONE PLACE THAT DECIDES. Before this, `_teardown()` was a closure in
    # this function and the three dispatch functions could not reach it, so
    # every non-launching return inside them — no partition, `sbatch` missing,
    # AND SBATCH REJECTING THE JOB, which is the common real-cluster case —
    # left whatever a pre_session hook had started running. Six exits, none of
    # them able to call the cleanup that existed a few lines above them.
    #
    # The fix is not six more calls. `Outcome` makes "did anything start?" a
    # field every return must fill in, so a new early return cannot skip the
    # question, and the answer is judged here, once.
    if not outcome.launched:
        _teardown()
    rc = outcome.rc

    if args.dry_run:
        # SAY WHAT THE DRY RUN LEFT BEHIND. It composed for real, so it wrote
        # host state; a user who reads "dry run" and finds a new session
        # directory has been told something false by omission.
        _teardown()
        sys.stderr.write(
            f"\nhpc-launcher: --dry-run COMPOSED THIS SESSION FOR REAL (that is "
            f"what makes the script above accurate), so it ran this project's "
            f"pre_session hooks and wrote host state:\n"
            f"  session dir: {plan.session_dir}\n"
            f"  Nothing was submitted, and anything a hook STARTED has been "
            f"torn down.\n"
            f"  BUT A PREVIEW IS NOT CREDENTIAL-INERT. In shared auth mode, if "
            f"this project's credential file is newer than the shared one, the "
            f"reconcile copies it OVER your shared login (the hook says so, "
            f"loudly, when it happens). In broker mode, starting the broker can "
            f"refresh and ROTATE your refresh token, which logs other holders "
            f"out. Neither is undone by the teardown above.\n")
    return rc


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


class Outcome(NamedTuple):
    """What a dispatch function DID, not merely what it returned.

    `rc` alone cannot answer "did anything start?" — 0 means both "submitted"
    and "printed a dry run", and a non-zero rc means both "sbatch rejected it"
    and "the session ran and exited non-zero". `main()` has to tell those apart
    to know whether to tear down what a pre_session hook started, so the answer
    is a FIELD every return must fill in rather than something inferred from a
    number. A new early return cannot silently skip the question.

    `launched=True` means something OUTLIVES this command and owns what the
    hooks set up — which is exactly one case: a job accepted by `sbatch`. It
    runs later on a compute node, so a host helper a pre_session hook started
    may be needed for its lifetime and must not be torn down here.

    Everything else is False, including the two SYNCHRONOUS paths: when `srun`
    or `apptainer exec` returns, the session is over and post_session is due —
    the same moment `botainer start` runs it. And every `--dry-run`, which
    prints and starts nothing.
    """
    rc: int
    launched: bool


def _do_submit(plan: SubmissionPlan, spec, *, dry_run: bool) -> Outcome:
    if not plan.partition:
        sys.stderr.write(
            "hpc-launcher: refusing to submit without a partition.\n"
            "  Set plugins.hpc-launcher.partition in .botainer/config.yaml, pass "
            "--partition <X>, or run `botainer hpc setup` (bundled profiles set "
            "one).\n"
        )
        return Outcome(4, launched=False)
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
        return Outcome(0, launched=False)
    if not have_slurm():
        sys.stderr.write("hpc-launcher: `sbatch` not on PATH; not on a Slurm cluster.\n")
        return Outcome(5, launched=False)
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
        # SBATCH REJECTED THE JOB — the common real-cluster exit, and the
        # one that leaked for longest: nothing was queued, so whatever a
        # hook started has no job to serve.
        return Outcome(completed.returncode, launched=False)
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
    # Submitted. The job runs LATER on a compute node, so a host helper a
    # hook started may be needed for its lifetime: do NOT tear down.
    return Outcome(0, launched=True)



def _attached_job_still_running(jobid: str) -> bool:
    """Is this Slurm job still alive? Used ONLY to tell a departed viewer from
    a finished session in `_do_attach`.

    FAILS TOWARD TEARDOWN, deliberately. No squeue, a timeout, a non-zero exit
    or an unparseable answer all return False, which preserves exactly today's
    behaviour. Of the two ways to be wrong, silently skipping the shared
    credential reconcile is the worse one — it produces a cross-project "login
    expired" days later with nothing pointing at the cause, and it has already
    been shipped and fixed once. Running the reconcile a little early is
    visible and recoverable.
    """
    if not have_slurm():
        return False
    try:
        result = subprocess.run(
            ["squeue", "-j", str(jobid), "--noheader", "--format=%T"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        sys.stderr.write(
            "hpc-launcher: warning: could not determine whether job "
            f"{jobid} is still running; treating the session as ended.\n")
        return False
    if result.returncode != 0:
        return False
    # squeue prints nothing for a job that has left the queue. Any live state
    # (RUNNING, COMPLETING, ...) means the session is still there.
    return bool(result.stdout.strip())


def _do_attach(plan: SubmissionPlan, jobid: str | None, *, dry_run: bool) -> Outcome:
    jobid = jobid or plan.existing_jobid
    if not jobid:
        sys.stderr.write("hpc-launcher --mode=attach requires --jobid or SLURM_JOB_ID env.\n")
        return Outcome(4, launched=False)
    # Consent (posture + resources) already shown by main()'s _consent().
    if plan.nudge_enabled:
        # #56 + cluster-nudge fix: the batch agent runs inside a
        # screen session named `botainer-<jobid>` on the compute node (see
        # _common.py's run_section, which creates it nonforking with `-D -m`
        # so the batch process IS the session) — the SAME screen `botainer
        # nudge` delivers into. REATTACH
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
        return Outcome(0, launched=False)
    if not have_slurm():
        sys.stderr.write("hpc-launcher: `srun` not on PATH; not on a Slurm cluster.\n")
        return Outcome(5, launched=False)
    # IS THE SESSION OVER, OR DID THE VIEWER JUST GO AWAY? Those are different
    # questions and this used to conflate them.
    #
    # The old comment here said "`srun` is synchronous, so by the time it
    # returns the agent has exited". TRUE on the else-branch above, where srun
    # runs the agent itself. FALSE on the nudge branch — the only branch anyone
    # attaches on — where srun is running `screen -r`, which returns the moment
    # you are DETACHED. And nobody detaches on purpose: a sleeping laptop, a
    # dropped VPN, a closed lid or an SSH timeout all do it, which is precisely
    # what screen exists to survive. So on that branch a return is the ORDINARY
    # case and says nothing about the agent.
    #
    # `launched=False` reaches the single teardown site in main(), which runs
    # this project's post_session hooks — including the shared-credential
    # reconcile — against a session that is still running. Viewer detachment
    # must therefore be distinguished from workload completion.
    #
    # THE OBVIOUS FIX IS WRONG AND WAS ALREADY TRIED. Returning `launched=True`
    # unconditionally skipped that reconcile entirely: a refresh inside the
    # container was never promoted to the shared login, silently, which is the
    # cross-project "login expired" symptom the hook exists to prevent. So this
    # cannot be "never tear down"; it has to tell the two cases apart.
    #
    # SLURM IS THE AUTHORITY, and the 79a56bc lifetime fix is what makes it
    # exact: the batch process IS the screen session now (`exec screen -D -m`),
    # so "the job is alive" and "the session is alive" are the same fact. Before
    # that fix they could diverge and this check would not have been sound.
    rc = subprocess.run(argv, check=False).returncode
    if plan.nudge_enabled and _attached_job_still_running(jobid):
        # The agent is still on the node. Leaving the viewer is not session end,
        # so post_session must NOT run; it belongs to `hpc stop` / job end.
        return Outcome(rc, launched=True)
    return Outcome(rc, launched=False)


def _do_here(plan: SubmissionPlan, *, dry_run: bool) -> Outcome:
    if plan.existing_jobid is None and not dry_run:
        sys.stderr.write("hpc-launcher --mode=here: not inside a Slurm allocation.\n")
        return Outcome(4, launched=False)
    # Consent (posture + resources) already shown by main()'s _consent().
    argv = plan.to_apptainer_argv()
    if dry_run:
        sys.stdout.write("# hpc-launcher --dry-run (here):\n")
        sys.stdout.write("  " + " ".join(shlex.quote(a) for a in argv) + "\n")
        return Outcome(0, launched=False)
    # THE SESSION IS OVER — same as attach: `apptainer exec` is synchronous, so
    # post_session runs now, matching `botainer start`. See `_do_attach`.
    return Outcome(subprocess.run(argv, check=False).returncode, launched=False)


if __name__ == "__main__":
    sys.exit(main())
