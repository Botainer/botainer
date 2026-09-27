"""HPC job-dispatcher mailbox (task #54, P1 — the security core).

The directional mailbox is INV-1 of the dispatcher: it lets the caged agent
REQUEST batch/pool jobs and READ their results without ever getting a write path
that an UNCAGED host process (the dispatcher, slurmstepd) later reads or executes
through — the v0.0.x shared-rw `/jobs` let a prompt-injected agent plant a symlink
that slurmstepd wrote through, out of the sandbox (DN-003 class;
memory project_hpc_jobs_rw_folder_escape). The split is:

    in/   agent writes  → bound RW  at /jobs/in    host reads O_NOFOLLOW, skips
                                                    symlinks / non-regular / non-owned
    out/  host writes    → bound RO  at /jobs/out   agent can't create files here,
                                                    so it can't pre-plant a symlink
    run/  host-private   → NOT bound anywhere       generated .sbatch + SLURM
                                                    --output logs live here

`run/` and `out/` are written by uncaged processes, so the whole mailbox lives
OUTSIDE the container-bound `state/<uuid>/` subtree (StatePaths.hpc_jobs_dir),
and only `in/` (RW) + `out/` (RO) are bound — never `run/`, never the parent.
That separation IS the invariant: consolidating these dirs, binding `run/`,
or granting `/jobs/out` RW re-creates the v0.0.x shared-rw `/jobs` escape,
where a caged agent symlinks over a host write and lands it outside the cage.

This module is the mailbox model + its binds only; the host-side validation +
sbatch (the dispatcher daemon) and the in-container `botainer-job` CLI are
separate pieces (P2/P3).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import (
    AgentRendering,
    Bind,
    BindMode,
    Provenance,
    validate_image_reference,
)
from botainer.mount_plan import validation as _mp_validation
from botainer.state.dir import StatePaths

# In-container mount points for the mailbox. `run/` intentionally has no target.
JOBS_IN_TARGET = "/jobs/in"
JOBS_OUT_TARGET = "/jobs/out"

# The §4 SOLID cage — MUST match ApptainerAdapter (adapters/apptainer.py). A
# child job runs UNDER this cage exactly like the agent session; a drift-test
# pins the two (test_hpc_jobs_child.py). INV-2: jobs run ONLY in a caged
# container, never bare on the host.
_CHILD_CAGE_FLAGS: tuple[str, ...] = (
    "--containall", "--cleanenv", "--no-privs", "--drop-caps", "all",
)

# A child workload's entrypoint (command[0] basename) may NOT be a container
# runtime or the agent CLI: the child must not launch a sibling container
# (#148 forbidden-runtime class) nor re-enter the agent (never-botainer), which
# would escape the pinned cage / re-acquire credentials.
_FORBIDDEN_CHILD_ENTRYPOINTS: frozenset[str] = frozenset({
    "apptainer", "singularity", "singularity-ce",
    "docker", "podman", "nerdctl", "ctr", "runc", "crun",
    "botainer", "botainer-hpc", "claude", "codex",
})

# In-container targets a child job may NEVER receive — INV-2 "no credentials":
# the child gets /workspace /packages /scratch /apps(ro), never home / creds /
# ssh. Refused as a target prefix (exact or child).
_FORBIDDEN_CHILD_BIND_TARGETS: tuple[str, ...] = (
    "/home", "/root", "/home/agent/.claude", "/home/agent/.codex",
    "/home/agent/.ssh", "/home/agent/.config",
)


@dataclass(frozen=True)
class JobMailbox:
    """The three host-side mailbox directories for one project (by uuid)."""

    root: Path  # <state_root>/hpc-jobs/<uuid>
    in_dir: Path  # agent writes; host reads O_NOFOLLOW
    out_dir: Path  # host writes; agent reads RO
    run_dir: Path  # host-private; NEVER bound into any container


def mailbox_for(paths: StatePaths, uuid: str) -> JobMailbox:
    """Compute the mailbox paths for a project WITHOUT creating them."""
    root = paths.hpc_jobs_dir(uuid)
    return JobMailbox(
        root=root,
        in_dir=root / "in",
        out_dir=root / "out",
        run_dir=root / "run",
    )


def ensure_mailbox(paths: StatePaths, uuid: str) -> JobMailbox:
    """Create the mailbox dirs 0700 and return the JobMailbox.

    0700 on every dir: only the owning user can traverse them on the host. This
    is host-side hygiene (the agent reaches in/ + out/ only through the binds,
    with the modes below).
    """
    mb = mailbox_for(paths, uuid)
    for d in (mb.root, mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return mb


_MAX_PROFILE_TEXT = 1000


def _scrub_profile_text(text) -> str:
    """Control-char-scrub + length-cap a free-text profile field (`notes` /
    `description`) before it reaches the caged agent via out/profiles.json.

    These come from the UNTRUSTED, git-shareable `.botainer/config.yaml` and are
    NOT covered by the pydantic `_validate_sbatch_str` charset guard (which only
    gates the sbatch-directive fields). `botainer-job profiles` prints them and
    AGENT_HINTS tells the agent to heed them, so a hostile config could otherwise
    smuggle terminal ANSI escapes or `ignore previous instructions`-style prompt-
    injection through a channel framed as trusted operator guidance. Collapse
    every non-printable char to a space (kills ESC/ANSI/NUL; keeps line
    structure) and cap the length. The caller fences the result as DATA. (This is
    the same discipline agent_hints._sanitize_hints_section applies to plugin
    hint text; kept local to avoid an inspect→hpc import layering dependency.)"""
    if not isinstance(text, str):
        return ""
    text = text[:_MAX_PROFILE_TEXT]
    return "\n".join(
        "".join(ch if ch.isprintable() else " " for ch in line)
        for line in text.splitlines()
    )


def write_profiles_manifest(mb: JobMailbox, job_profiles: dict) -> None:
    """Host-write the available profiles into out/profiles.json so the caged
    `botainer-job profiles` can list them — the agent can't read
    `.botainer/config.yaml` (null-bind masked). Only display-safe fields are
    exposed (no host-specific account, etc. — resources come from the profile
    server-side). The free-text `notes`/`description` are control-char-scrubbed
    (`_scrub_profile_text`) since they originate in untrusted config and reach
    the agent's terminal + context."""
    import json

    profiles: dict[str, dict] = {}
    for name, prof in job_profiles.items():
        # `prof` may be a pydantic JobProfile or a plain dict (defensive).
        get = (lambda k, d=None: getattr(prof, k, d)) if not isinstance(prof, dict) \
            else (lambda k, d=None: prof.get(k, d))
        profiles[name] = {
            "description": _scrub_profile_text(get("description", "")),
            "partition": get("partition", ""),
            "time": get("time", ""),
            "cpus": get("cpus", 1),
            "memory": get("memory", ""),
            "gpus": get("gpus", 0),
            "max_concurrent": get("max_concurrent", 1),
            # #68 MPI: surfaced so the agent sees multi-node/parallel profiles.
            "nodes": get("nodes", 1),
            "ntasks": get("ntasks", None),
            "ntasks_per_node": get("ntasks_per_node", None),
            # jobs v2: the opt-in maxes the agent may request up to, + the
            # verbal usage notes (which/when to prefer, where to be conservative).
            "max_cpus": get("max_cpus", None),
            "max_memory": get("max_memory", None),
            "max_gpus": get("max_gpus", None),
            "max_nodes": get("max_nodes", None),
            "max_time": get("max_time", None),
            "notes": _scrub_profile_text(get("notes", "")),
        }
    manifest = {"version": "botainer-job-profiles-v1", "profiles": profiles}
    mb.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = mb.out_dir / ".profiles.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    import os as _os
    _os.replace(tmp, mb.out_dir / "profiles.json")


def mailbox_binds(mb: JobMailbox) -> list[Bind]:
    """The two binds that expose the mailbox to the agent container.

    INV-1 directionality is enforced by the MODES: `in/` is RW (agent writes
    requests), `out/` is RO (agent reads results but cannot create files, so it
    cannot pre-plant a symlink where the host writes). `run/` is returned by
    NEITHER bind — it is host-private and never mounted, so the generated
    `.sbatch` and SLURM `--output` logs the host writes there are unreachable
    from the cage. The parent `root/` is never bound either (only the two leaves),
    so binding does not transitively expose `run/`.
    """
    return [
        Bind(
            source=str(mb.in_dir),
            target=JOBS_IN_TARGET,
            mode=BindMode.RW,
            provenance=Provenance.CORE,
            provenance_detail="job-dispatcher inbox; agent writes requests (INV-1)",
            agent_rendering=AgentRendering.SHOWN,
            self_test="SELFTEST_JOBS_IN_RW",
        ),
        Bind(
            source=str(mb.out_dir),
            target=JOBS_OUT_TARGET,
            mode=BindMode.RO,
            provenance=Provenance.CORE,
            provenance_detail="job-dispatcher outbox; agent reads results RO (INV-1)",
            agent_rendering=AgentRendering.SHOWN,
            self_test="SELFTEST_JOBS_OUT_RO",
        ),
    ]


# ───────────────────────── INV-2: caged child job ─────────────────────────


def _validate_child_command(command: tuple[str, ...]) -> None:
    """A child workload is an ARGV list (never a shell string) whose entrypoint
    is not a container runtime / the agent CLI."""
    if not command or not all(isinstance(c, str) for c in command):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            "child job command must be a non-empty argv list of strings "
            "(INV-2: argv, never shell text).",
        )
    for tok in command:
        if any(ord(ch) < 0x20 for ch in tok):
            raise Refused(
                RefusalCategory.CAPABILITY_VALUE_INVALID,
                f"child job command token {tok!r} contains a control character.",
            )
    import os.path as _osp
    base = _osp.basename(command[0])
    if base in _FORBIDDEN_CHILD_ENTRYPOINTS:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"child job entrypoint {base!r} is a container runtime or the agent "
            f"CLI; a child job may not launch a sibling container or re-enter "
            f"the agent (INV-2 / #148 forbidden-runtime class).",
        )


def _child_bind_flags(binds: tuple[Bind, ...]) -> list[str]:
    """Validate + render the child's binds. Reuses the mount-plan path safety +
    the (ancestor-aware) source denylist so a child bind can't smuggle /etc or
    credentials via its source, and refuses credential/home TARGETS (INV-2: the
    child gets /workspace /packages /scratch /apps(ro), never creds/home/ssh)."""
    flags: list[str] = []
    for b in binds:
        src = _mp_validation._normalize_path(b.source, label="child bind source")
        tgt = _mp_validation._normalize_path(b.target, label="child bind target")
        if src != b.source or tgt != b.target:
            raise Refused(
                RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
                f"child bind not in canonical form: {b.source!r}/{b.target!r}.",
            )
        if _mp_validation._source_is_denied(src, trusted_roots=()):
            raise Refused(
                RefusalCategory.MOUNT_SOURCE_DENIED,
                f"child job bind source {src!r} is sensitive/denied.",
            )
        for forb in _FORBIDDEN_CHILD_BIND_TARGETS:
            f = forb.rstrip("/")
            if tgt == f or tgt.startswith(f + "/"):
                raise Refused(
                    RefusalCategory.MOUNT_TARGET_DENIED,
                    f"child job may not receive bind target {tgt!r} — a child "
                    f"job gets NO credentials/home/ssh (INV-2).",
                )
        # Also refuse the shared system-target denylist (/etc, /proc, /sys, /dev,
        # docker sockets, …) — defense-in-depth so no current/future child bind
        # can mount over the container's system dirs (sharp-edges F2).
        if _mp_validation._target_is_denied(tgt):
            raise Refused(
                RefusalCategory.MOUNT_TARGET_DENIED,
                f"child job bind target {tgt!r} is a denied system target.",
            )
        ro = b.mode != BindMode.RW  # RO / unix-socket / anything non-RW → ro
        flags += ["--bind", f"{src}:{tgt}" + (":ro" if ro else "")]
    return flags


import re as _re

# Env var names botainer injects into a child job (MODULEPATH etc.) — a strict
# shell-safe identifier; values may not carry control chars (they become
# `--env KEY=VAL` operands, never shell). This is host/config-sourced env
# (module machinery), NOT agent-controlled.
_ENV_KEY_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _child_env_flags(env: dict[str, str] | None) -> list[str]:
    """Validate + render `--env KEY=VAL` flags for a child job. Keys are strict
    identifiers; values reject control chars (incl. newline) so a value can't
    smuggle another flag. Emitted as apptainer operands, never shell."""
    if not env:
        return []
    flags: list[str] = []
    for k, v in env.items():
        if not (isinstance(k, str) and _ENV_KEY_RE.match(k)):
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                          f"child job env key {k!r} is not a valid identifier.")
        if not isinstance(v, str) or any(ord(ch) < 0x20 for ch in v):
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                          f"child job env value for {k!r} has a control char.")
        flags += ["--env", f"{k}={v}"]
    return flags


# \A…\Z (NOT ^…$): Python's `$` also matches just before a trailing newline, so
# `^…$`.match("x\n") succeeds — a module name ending in \n would then break out
# onto a new line of the `bash -c` preamble (sharp-edges F1). \A…\Z
# anchors the WHOLE string; control chars are rejected explicitly below too.
_MODULE_NAME_RE = _re.compile(r"\A[A-Za-z0-9_./+-]+\Z")


def _module_preamble_workload(
    command: tuple[str, ...], modules: tuple[str, ...], env: dict | None,
) -> tuple[str, ...]:
    """Wrap `command` so `module load <modules>` runs INSIDE the caged job before
    it, WITHOUT turning the agent's command into shell text: the preamble is a
    fixed script whose only interpolation is the CONFIG module list (charset-
    validated); the agent's argv is passed as positional params and re-exec'd
    verbatim via `exec "$@"` (no reparse). `module` is defined by BASH_ENV
    (set in `env` by the cluster contribution) — so fail closed if it's absent."""
    if not modules:
        return command
    # F4 hardening (sharp-edges): re-validate the ORIGINAL entrypoint HERE, so the
    # forbidden-runtime guard is local to the wrapper and survives even if a
    # future caller reaches this without the pre-wrap check (after wrapping,
    # workload[0] is `bash` and assert_caged_child_job can't see the real one).
    _validate_child_command(command)
    if not (env and env.get("BASH_ENV")):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            "job profile lists modules but the cluster module system is not "
            "exposed to jobs (site policy mounts.cluster_lmod_root is unset). "
            "Set the cluster module roots in /etc/botainer/policy.yaml, or drop "
            "`modules:` from this profile.",
        )
    for m in modules:
        if not (isinstance(m, str) and m and _MODULE_NAME_RE.match(m)
                and not any(ord(ch) < 0x20 for ch in m)):
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                          f"module name {m!r} is not a valid Lmod token.")
    names = " ".join(modules)  # charset-validated: flat tokens, no shell metachars
    # `&&` (not `;`): if `module load` fails (missing/incompatible module) the
    # workload must NOT run with the wrong/absent toolchain — fail closed
    # (sharp-edges F2). $0='bash', $1..=the original argv → `exec "$@"` verbatim.
    return ("bash", "-c", f"module load {names} && exec \"$@\"", "bash", *command)


def compose_child_job_argv(
    image: str, command: tuple[str, ...], binds: tuple[Bind, ...] = (),
    env: dict[str, str] | None = None, preload_modules: tuple[str, ...] = (),
    gpus: int = 0,
) -> list[str]:
    """Compose the §4-caged `apptainer exec` argv for a NON-agent child job.

    INV-2: the workload runs inside the SAME §4 cage as the agent, the agent's
    requested command is an argv OPERAND after the pinned image (never shell,
    never a container runtime), no credentials are bound, and the frozen-plan
    chokepoint (`assert_caged_child_job`) re-verifies all of it before the caller
    hands the argv to sbatch. The agent cannot choose the image or the cage flags.

    `env` (host/config-sourced, e.g. MODULEPATH for the module system) is emitted
    as `--env KEY=VAL` BEFORE the image, so it survives `--cleanenv` without
    touching the workload argv. `preload_modules` (per-profile config) auto-runs
    `module load <…>` inside before the workload — the ORIGINAL command's
    entrypoint is still validated (below) BEFORE the module wrapper is applied.
    """
    img = validate_image_reference(image)
    _validate_child_command(command)  # validate the ORIGINAL entrypoint first
    workload = _module_preamble_workload(command, tuple(preload_modules), env)
    # Structural mask invariant (sharp-edges): a RW /workspace bind
    # MUST carry the /workspace/.botainer mask (config.yaml/project-id), ordered
    # after it. Enforced here so NO child-bind assembler can silently omit it —
    # the class of the config-exposure bug. Same check the session validator runs.
    _mp_validation.assert_mask_invariants(list(binds))
    bind_flags = _child_bind_flags(binds)
    env_flags = _child_env_flags(env)
    # GPU exposure (#68): Slurm ALLOCATES the GPU (`#SBATCH --gres=gpu:N`) but the
    # cage's `--containall` gives a minimal /dev that HIDES /dev/nvidia*, so without
    # `--nv` the workload can't see the GPU (no driver libs, no device nodes) — the
    # GPU-jobs claim would be dead on the compute node. `--nv` binds the host NVIDIA
    # driver + devices in; it needs no privilege, so it composes with the cage. Only
    # for gpus>0 (never on CPU jobs). AMD (`--rocm`) is a gpu_type-gated follow-up.
    gpu_flags = ["--nv"] if gpus and int(gpus) > 0 else []
    argv = ["apptainer", "exec", *_CHILD_CAGE_FLAGS, *gpu_flags, *bind_flags,
            *env_flags, img, *workload]
    assert_caged_child_job(argv, img)
    return argv


def assert_caged_child_job(argv: list[str], image: str) -> None:
    """The never-bare-job chokepoint (mirrors SubmissionPlan.__post_init__).

    Re-verifies, from the FINAL argv, that a child job is a §4-caged
    `apptainer exec` of the pinned image with an argv (non-runtime) workload —
    so no construction path can hand `sbatch` a bare/uncaged/cred-bearing job.
    """
    if argv[:2] != ["apptainer", "exec"]:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"child job argv must begin with `apptainer exec`; got {argv[:2]!r} "
            f"(INV-2: jobs run ONLY in a caged container, never bare on the host).",
        )
    for flag in ("--containall", "--cleanenv", "--no-privs"):
        if flag not in argv:
            raise Refused(
                RefusalCategory.CAPABILITY_VALUE_INVALID,
                f"child job cage missing §4 flag {flag!r}.",
            )
    # --drop-caps must be immediately followed by `all`.
    if not any(
        argv[i] == "--drop-caps" and i + 1 < len(argv) and argv[i + 1] == "all"
        for i in range(len(argv))
    ):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            "child job cage missing `--drop-caps all`.",
        )
    if image not in argv:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"child job argv does not contain the pinned image {image!r}.",
        )
    # Everything AFTER the image is the workload argv. Its ENTRYPOINT
    # (workload[0]) may not be a container runtime / agent CLI. Later tokens are
    # ARGS to that entrypoint (a filename, a grep pattern, …) and are NOT checked
    # — they can't launch a runtime that isn't in the cage, and the cage is the
    # real boundary regardless.
    import os.path as _osp
    img_i = argv.index(image)
    workload = argv[img_i + 1:]
    if not workload:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            "child job has no workload command after the image.",
        )
    if _osp.basename(workload[0]) in _FORBIDDEN_CHILD_ENTRYPOINTS:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"child job entrypoint {workload[0]!r} is a container runtime or the "
            f"agent CLI (INV-2 / never-bare-job).",
        )


# ─────────────────── multi-node MPI (#68 Phase-2) ───────────────────
# The established apptainer+SLURM+PMIx recipe (CHTC / PSU Roar / Plymouth Lovelace
# guides; DN-001 §9 Q2). A multi-node MPI job is launched as
# ONE caged `apptainer exec` per rank under `srun` — so the ranks must complete
# the PMIx handshake ACROSS the §4 cage. `--cleanenv` strips the PMIX_*/PMI_*/
# SLURM_* env slurmstepd sets per rank, and `--containall` hides the per-step PMIx
# rendezvous socket, so without help every rank boots as an isolated rank-0
# instead of joining the same namespace. Both the env AND socket path are known only
# PER-TASK (post-fork), so they cannot be static `--env`/`--bind` at compose time.

_MPI_FLAVORS: tuple[str, ...] = ("pmix", "pmi2")

# The per-task PMIx bootstrap shim. Runs as the srun TASK (one per rank), OUTSIDE
# the container, so it can read the per-rank env slurmstepd set at fork. It:
#   • forwards PMIX_*/PMI_*/SLURM_* across the cage via APPTAINERENV_* — apptainer
#     INJECTS these AFTER --cleanenv/--containall clean, so they survive the cage
#     UNCHANGED (no cage flag is touched);
#   • forces PMIx unix-socket uid auth (psec=native) so the container needs no
#     munge socket;
#   • binds ONLY the per-step PMIx server tmpdir (the rendezvous socket + dstore
#     mmap files) at its host path — never host /tmp;
#   • forces a CAGE-SAFE same-node shared-memory path (OMPI_MCA_pml=ob1 +
#     btl_vader_single_copy_mechanism=none). WHY: the cage drops CAP_SYS_PTRACE
#     (--drop-caps all) and gives each rank its own PID namespace (--containall),
#     so OpenMPI's default single-copy shared-memory (CMA / process_vm_readv)
#     doesn't just fail — it SEGFAULTS. ob1 avoids UCX's CMA path and vader's
#     single-copy=none uses a safe 2-copy shared-memory segment instead. Both are
#     harmless if the image's MPI isn't OpenMPI/UCX. (InfiniBand acceleration via
#     UCX is a separate deferred item — when /dev/infiniband is bound, revisit
#     whether to drop the ob1 pin; today inter-node is TCP regardless, so ob1
#     costs nothing.) These are STATIC MPI-correctness env, not PMIx bootstrap.
#   • execs the pre-validated caged argv VERBATIM (`exec "$@"` — argv-not-shell,
#     no reparse of the workload).
#
# SECURITY: this is the ONE place env crosses INTO the cage outside the vetted
# `_child_env_flags` path. It is safe ONLY paired with `srun --export=NONE`
# (emitted together in mpi_srun_launch_lines), which strips the SUBMITTING env so
# the prefix sweep and $PMIX_SERVER_TMPDIR can only pick up slurmstepd-SET vars,
# never host login env or agent-controlled values. The shim is a COMPILE-TIME
# CONSTANT — no agent- or config-sourced text is ever interpolated into it. Gated
# on the AUTHOR-set `mpi:` profile field (never agent request data). Blast radius:
# the PMIx server is per-job-step, 0700 user-owned; a caged rank can only talk to
# its OWN step's namespace — not the scheduler, not other users/jobs.
_PMIX_BOOTSTRAP_SHIM: str = (
    'for v in "${!PMIX_@}" "${!PMI_@}" "${!SLURM_@}"; do '
    'export "APPTAINERENV_${v}=${!v}"; done\n'
    'export APPTAINERENV_PMIX_MCA_psec=native\n'
    # Cage-safe same-node shared memory: no CMA (segfaults without CAP_SYS_PTRACE
    # + a shared PID namespace, both dropped by the cage). See the comment above.
    'export APPTAINERENV_OMPI_MCA_pml=ob1\n'
    'export APPTAINERENV_OMPI_MCA_btl_vader_single_copy_mechanism=none\n'
    'if [ -n "${PMIX_SERVER_TMPDIR:-}" ]; then '
    'export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${PMIX_SERVER_TMPDIR}"; '
    'fi\n'
    'exec "$@"'
)


def mpi_srun_launch_lines(flavor: str, caged_argv: list[str]) -> list[str]:
    """Render the sbatch body lines that launch a multi-node MPI child (#68):
    `srun --mpi=<flavor> --export=NONE <PMIx shim> <ABS apptainer> <caged argv>`.

    The already-§4-caged argv is passed to the shim as positional params and
    re-exec'd verbatim (`exec "$@"`) — only its argv[0] ('apptainer') is resolved
    to an ABSOLUTE path in the batch env (which still has PATH), because
    `--export=NONE` clears PATH from the task env. The cage argv is NOT rebuilt,
    so the compose-time `assert_caged_child_job` guarantee still holds for exactly
    what runs; we re-check the structural cage invariant here as defence in depth."""
    import shlex
    if flavor not in _MPI_FLAVORS:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"unknown MPI flavor {flavor!r} (expected one of {_MPI_FLAVORS}).",
        )
    # Defence in depth: we may only wrap a §4-caged `apptainer exec` argv (so the
    # shim can never be pointed at a bare/uncaged command). Structural re-check
    # (image-independent) — the full check already ran in compose_child_job_argv.
    if caged_argv[:2] != ["apptainer", "exec"]:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            "MPI launch expects a §4-caged `apptainer exec` argv "
            f"(got {caged_argv[:2]!r}).",
        )
    for f in ("--containall", "--cleanenv", "--no-privs"):
        if f not in caged_argv:
            raise Refused(
                RefusalCategory.CAPABILITY_VALUE_INVALID,
                f"MPI launch: caged argv missing §4 cage flag {f!r}.",
            )
    # `--drop-caps all` must be present as an adjacent pair (mirror the full
    # assert_caged_child_job check) — a bare `--drop-caps` without `all` would drop
    # nothing (sharp-edges/security review).
    if not any(caged_argv[i] == "--drop-caps" and i + 1 < len(caged_argv)
               and caged_argv[i + 1] == "all" for i in range(len(caged_argv))):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            "MPI launch: caged argv missing `--drop-caps all`.",
        )
    q = shlex.quote
    rest = " ".join(q(a) for a in caged_argv[1:])  # everything AFTER argv[0]
    return [
        "# --- multi-node MPI (#68): resolve apptainer in the batch env (PATH is",
        "# cleared in the task by --export=NONE), then srun one caged rank per task",
        "# with the PMIx bootstrap shim (jobs._PMIX_BOOTSTRAP_SHIM).",
        'BOT_APPTAINER_BIN="$(command -v apptainer || true)"',
        ': "${BOT_APPTAINER_BIN:?apptainer not found on PATH in the job environment}"',
        f"exec srun --mpi={flavor} --export=NONE /bin/bash -c "
        f'{q(_PMIX_BOOTSTRAP_SHIM)} bash "$BOT_APPTAINER_BIN" {rest}',
    ]


def _child_git_binds(project_root: Path) -> tuple[Bind, ...]:
    """Freeze the whole `/workspace/.git` RO for a child job so it can't plant a
    host-executed git hook or poison repo-local git config (`/workspace` is RW, so
    otherwise a caged job could write `.git/hooks/pre-commit` or `.git/config`
    `core.hooksPath`/`core.sshCommand`, which run as the USER on the host the next
    time they `git` in the project — a full cage escape). Whole-dir RO (vs the
    session's per-file overlay) is right for batch jobs: they don't need git
    write, and it also stops CREATION of an absent hooks/config (sharp-edges F3).

    Fails CLOSED like the git plugin: refuses if `.git` is a gitdir POINTER
    (worktree/submodule — the real hooks/config live elsewhere, an RO overlay
    wouldn't cover them, F2), and refuses if `.git/config` ALREADY carries a
    host-code-execution key (a pre-existing one an RO overlay can't neutralize),
    using the SHARED section-aware scanner (F1 — no divergent copy)."""
    git = project_root / ".git"
    if not git.exists():
        return ()  # not a git repo — nothing to freeze
    if not git.is_dir():
        raise Refused(
            RefusalCategory.MOUNT_SOURCE_DENIED,
            f"{git} is a gitdir pointer (worktree/submodule); the real hooks/"
            f"config live elsewhere and can't be frozen — refusing to dispatch a "
            f"job into it (mirror the git plugin's fail-closed).",
        )
    cfg = git / "config"
    if cfg.is_file():
        from botainer.core import gitconfig_scan
        bad = gitconfig_scan.dangerous_git_config_keys(cfg)
        if bad:
            raise Refused(
                RefusalCategory.MOUNT_SOURCE_DENIED,
                f"project .git/config carries host-code-execution setting(s) "
                f"{bad}; refusing to dispatch a job into this repo until removed "
                f"(core.hooksPath/sshCommand/credential.helper/[include]/…).",
            )
    return (Bind(
        source=str(git.resolve()), target="/workspace/.git", mode=BindMode.RO,
        provenance=Provenance.CORE,
        provenance_detail="child job: whole .git frozen ro (no hook/config plant)",
        nested_under="/workspace"),)


def child_core_binds(
    project_root: Path, proj_paths, state_dir: Path,
) -> tuple[Bind, ...]:
    """The project binds EVERY dispatched child job gets so it can see the code
    and data it operates on: `/workspace` (rw), `/packages` (rw — a job may
    compile/install packages, e.g. an MPI/GPU build, into the shared tree), and
    `/scratch` (rw), plus the security masks the RW `/workspace` requires.

    These are the project's OWN dirs (already bound to the agent session), never
    credentials/home/ssh — `_child_bind_flags` refuses those targets + a
    sensitive SOURCE regardless. Symlink-escape guarded: packages/scratch must
    resolve UNDER the user state dir (task #239 defense, mirrored from
    `compose_session`). The RW `/workspace` REQUIRES the `.botainer` mask
    (structurally enforced by `assert_mask_invariants`) and the `.git` host-exec
    freeze (`_child_git_binds`).
    """
    state_root = state_dir.resolve()
    pkg = proj_paths.packages_dir.resolve()
    scr = proj_paths.scratch_dir.resolve()
    # SCRATCH HAS ITS OWN ROOT — the same correction compose_session took.
    #
    # This mirrored compose_session's containment check, and kept mirroring the
    # OLD version of it: both sources had to resolve under the state root. Once
    # `/scratch` could be placed on a cluster's scratch filesystem, a user who
    # configured that got a session that launched fine and a DISPATCHER THAT
    # REFUSED EVERY CYCLE:
    #
    #   dispatcher: cycle error (Refused): [mount-source-denied] child job
    #   scratch bind source /…/scratch/…/botainer/<uuid> resolves outside the
    #   user state dir /…/.botainer/state; symlink-escape defense.
    #
    # Observed on a real cluster. The defence is unchanged in kind — each source
    # must still resolve inside a root the LAUNCHER chose, never anywhere the
    # agent can point it — but scratch is checked against its own declared root.
    # This is the sibling-drift class: a check copied rather than shared, so the
    # copy did not move when the original did.
    #
    #: `packages` became relocatable too (the INODE half — a cluster
    # $HOME with a 500,000-file cap, exhausted by conda/pip trees). It is
    # listed here with ITS root for the same reason scratch is; hardcoding
    # state_root for it would reproduce the refusal above the moment anyone
    # sets `packages.template`.
    def _root_for(attr):
        r = getattr(proj_paths, attr, None)
        return r.resolve() if r is not None else state_root

    for label, resolved, root in (("packages", pkg, _root_for("packages_root")),
                                  ("scratch", scr, _root_for("scratch_root"))):
        try:
            resolved.relative_to(root)
        except ValueError:
            raise Refused(
                RefusalCategory.MOUNT_SOURCE_DENIED,
                f"child job {label} bind source {resolved!s} resolves outside "
                f"{root!s}; symlink-escape defense.",
            ) from None
    ws = project_root.resolve()
    # SECURITY (sharp-edges/09): /workspace is RW, so WITHOUT this mask
    # a caged job could read AND write the real /workspace/.botainer/config.yaml —
    # the security config the SESSION deliberately masks (it defines job_profiles;
    # the next dispatcher cycle re-reads the host file /workspace points at). Use a
    # DEDICATED, always-empty child anchor (mounted :ro, so nothing writes it — no
    # cross-session residue, F5) nested over /workspace. `assert_mask_invariants`
    # REFUSES any child argv that binds /workspace RW without this mask.
    anchor = (proj_paths.data_dir / "child-null-bind-anchor").resolve()
    anchor.mkdir(parents=True, exist_ok=True, mode=0o700)
    return (
        Bind(source=str(ws), target="/workspace", mode=BindMode.RW,
             provenance=Provenance.CORE,
             provenance_detail="child job: project workspace (rw)"),
        Bind(source=str(anchor), target="/workspace/.botainer",
             mode=BindMode.NULL_BIND, provenance=Provenance.CORE,
             provenance_detail="child job: mask .botainer/config (empty null-bind)",
             nested_under="/workspace"),
        *_child_git_binds(project_root),
        Bind(source=str(pkg), target="/packages", mode=BindMode.RW,
             provenance=Provenance.CORE,
             provenance_detail="child job: shared package installs (rw; may build)"),
        Bind(source=str(scr), target="/scratch", mode=BindMode.RW,
             provenance=Provenance.CORE,
             provenance_detail="child job: ephemeral scratch (rw)"),
    )


def child_cluster_contribution(effective_policy) -> tuple[tuple[Bind, ...], dict]:
    """Cluster software + module-system exposure for child jobs — ON wherever the
    SITE configures it, a safe OFF (empty) default otherwise.

    Returns RO IDENTITY binds (source==target, so a module's absolute paths like
    `/apps/software/CUDA/bin` resolve at the same location inside the caged job)
    for: the cluster software root(s), the Lmod install, and the modulefiles
    root(s) — plus the LMOD_*/MODULEPATH/BASH_ENV env so `module` works inside.

    Same root-owned SitePolicy anchors the SESSION uses
    (`mounts.cluster_software_roots` / `cluster_lmod_root` /
    `cluster_modulepath_roots`) — user/project policy CANNOT widen them
    (self-grant defense). Empty ⇒ `((), {})` (feature OFF). Every bind still flows
    through `_child_bind_flags` at compose time, so an /etc-class / `/` / denylisted
    root is refused fail-closed; RO + `--no-privs --drop-caps all` means exposing
    the (public) software tree grants the caged job nothing writable.

    Admin responsibility (NOT enforced here): point these at genuine
    admin-owned software/module trees (`/apps`, `/opt/<pkg>`, the cluster Lmod).
    A non-denylisted but shallow root (`/usr`) is NOT refused, and a
    `cluster_modulepath_roots` value overlapping an agent-writable bind (e.g.
    under `/scratch`) would let the agent pre-plant a modulefile that `module
    load` then runs — so keep MODULEPATH roots on non-agent-writable paths.
    """
    m = effective_policy.mounts

    def _norm(p: str) -> str:
        return p.rstrip("/") if p not in ("", "/") else p

    software = [_norm(p) for p in getattr(m, "cluster_software_roots", []) if _norm(p)]
    lmod = _norm(getattr(m, "cluster_lmod_root", "") or "")
    modpaths = [_norm(p) for p in getattr(m, "cluster_modulepath_roots", []) if _norm(p)]
    roots: list[str] = []
    seen: set[str] = set()
    for p in (*software, *([lmod] if lmod else []), *modpaths):
        if p and p not in seen:
            seen.add(p)
            roots.append(p)
    binds = tuple(
        Bind(source=p, target=p, mode=BindMode.RO, provenance=Provenance.SITE_POLICY,
             provenance_detail="child job: cluster software / module system (ro, identity)")
        for p in roots
    )
    env: dict[str, str] = {}
    if lmod:
        env.update({
            "LMOD_PKG": lmod,
            "LMOD_DIR": f"{lmod}/libexec",
            "LMOD_CMD": f"{lmod}/libexec/lmod",
            # BASH_ENV sources init/bash for each `bash -c` subshell, so a job
            # whose command invokes a shell gets `module` defined (mirrors the
            # session's inner-load env).
            "BASH_ENV": f"{lmod}/init/bash",
        })
    if modpaths:
        env["MODULEPATH"] = ":".join(modpaths)
    return binds, env
