"""Parse `Main/.botainer/config.yaml`.

The config is `safe_load`-ed YAML, then validated against a closed JSON Schema.
After validation, it's a typed `ProjectConfig` model.

Per user decision: plugin settings live INLINE in
`config.yaml.plugins.<name>:`. The separate `Main/.botainer/settings/<plugin>/`
directory is collapsed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from botainer import auth_modes as _auth_modes
from botainer.core import agent_permissions
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import validate_profile_name

CONFIG_FILENAME = "config.yaml"
HOST_MANAGED_DIR = ".botainer"


class NetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "none"
    endpoints: list[str] = Field(default_factory=list)
    # #175: host_services config field removed. The
    # intended use case is now served by BindMode.UNIX_SOCKET binds.

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        # Readiness audit #12: was a plain str, so `mode: intenret` (typo) or
        # `mode: bridge` sailed through model_validate and got a false all-clear
        # from `config check`, then failed opaquely (or silently defaulted) at
        # launch. Refuse an unknown mode at parse time with the valid set.
        valid = {"none", "internet", "endpoint-ip-allowlist"}
        if v not in valid:
            raise ValueError(
                f"network.mode must be one of {sorted(valid)} (got {v!r})."
            )
        return v


class ResourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: int | None = None
    memory_mb: int | None = None
    time_minutes: int | None = None
    gpus: int = 0
    gpu_type: str | None = None
    partition: str | None = None
    account: str | None = None


# \A…\Z, NOT ^…$: Python's `$` also matches just before a trailing newline, so
# `^…$` admits e.g. "day\n" — which, rendered into `#SBATCH --partition=day\n`,
# splits the directive block and silently drops later #SBATCH lines (sharp-edges
# F2; same class as the module-name fix). Control chars rejected in
# _validate_sbatch_str too.
_SBATCH_SAFE = __import__("re").compile(r"\A[A-Za-z0-9._:+-]+\Z")
# `--constraint` additionally takes Slurm's feature operators & (AND) and
# | (OR). Bracket-count syntax (`[icelake*2]`) is deliberately NOT allowed:
# rare in practice, and it widens the grammar for no benefit.
_SBATCH_CONSTRAINT_SAFE = __import__("re").compile(r"\A[A-Za-z0-9._:+&|-]+\Z")


def is_parallel_shape(nodes: int | None, ntasks: int | None,
                      ntasks_per_node: int | None) -> bool:
    """True if a job shape launches MORE THAN ONE task — i.e. it needs the `srun`
    (and, with `mpi:`, the PMIx) launch path rather than a single `exec`.

    ONE definition, shared by the `JobProfile` validator (checks profile defaults
    at config-load) AND `dispatcher.render_child_sbatch` (checks the RESOLVED shape
    after agent overrides). They MUST agree, or `mpi:` can validate against a
    parallel default yet resolve to a single task and silently drop the MPI launch
    (sharp-edges MEDIUM). Note `>1` for nodes/ntasks (a bare `ntasks: 1`
    is NOT parallel); any `ntasks_per_node` is treated as multi-task per the
    dispatcher's long-standing behavior."""
    return ((nodes or 1) > 1
            or (int(ntasks) if ntasks else 1) > 1
            or bool(ntasks_per_node))


class JobProfile(BaseModel):
    """#54: a NAMED, capped resource shape the agent may dispatch a job into.

    Profiles are a CAP, not a default: the agent picks a profile by NAME and
    gets exactly these resources — it cannot override them from the request. The
    profile is UNTRUSTED (config is git-shareable) — the real trust boundary is
    the root-owned site policy's JobPolicy ceiling (see policy.py); these
    validators are defense-in-depth + a sane parse-time error.
    """

    model_config = ConfigDict(extra="forbid")
    description: str = ""
    partition: str = ""
    time: str = ""            # SLURM --time (HH:MM:SS or D-HH:MM:SS)
    cpus: int = 1
    memory: str = ""         # SLURM --mem, e.g. "32G"
    gpus: int = 0
    gpu_type: str | None = None
    account: str | None = None
    modules: list[str] | None = None   # None = inherit project modules; [] = none
    max_concurrent: int = 1            # cap across pending+queued+running
    array_max: int | None = None       # cap --array size (job arrays)
    # MPI / multi-node (#68). nodes>1 or ntasks>1 makes this an MPI/parallel
    # profile: the child job is launched with `srun` so SLURM spawns the tasks
    # across the allocation (PMI). NOTE: for MPI ranks to actually communicate,
    # the .sif's MPI must be built compatible with the cluster's (bind the host
    # MPI/PMI) — a .sif-build concern, not just these directives.
    nodes: int = 1
    ntasks: int | None = None          # total tasks (SLURM --ntasks)
    ntasks_per_node: int | None = None # SLURM --ntasks-per-node
    # Verbal guidance shown to BOTH the user (in config) and the agent (via
    # `botainer-job profiles`): how/when to use this profile, which to prefer,
    # where to be conservative. Multi-line OK. UNTRUSTED free text (this config
    # is git-shareable): it is NOT covered by the sbatch-directive charset guard,
    # so it is control-char-scrubbed + length-capped in
    # hpc.jobs._scrub_profile_text before it reaches the agent's terminal/context
    # and is presented as DATA, not instructions. It is NOT injected into the
    # system prompt / AGENT_HINTS instruction chain (that text is launcher-static)
    # — the agent only reads it on demand via `botainer-job profiles`.
    notes: str = ""
    # "Fixed by default, opt-in max" (jobs v2): the scalar fields above are the
    # DEFAULT the agent gets. If a max_* is set, the agent MAY request UP TO it
    # via `botainer-job submit --cpus/--mem/--gpus/--nodes/--time`; if a max_* is
    # absent, that resource is FIXED at the default.
    # SITE CEILING: the RESOLVED (post-override) cpus/memory/time/gpus/nodes are all
    # re-checked against the root-owned site JobPolicy at submit
    # (dispatcher → check_profile_against_ceiling), so the guarantee
    # `agent ≤ profile max ≤ site policy` holds for EVERY dimension (site caps:
    # max_cpus_per_job / max_mem_mb_per_job / max_time_seconds_per_job /
    # max_gpus_per_job / max_nodes_per_job; None = uncapped, SLURM/QOS is then the
    # backstop). The profile is untrusted (git-shareable); the JobPolicy is not.
    max_cpus: int | None = None
    max_memory: str | None = None      # SLURM --mem form, e.g. "128G"
    max_gpus: int | None = None
    max_nodes: int | None = None
    max_time: str | None = None        # SLURM --time form
    # Warm pool (jobs v2): if set, the dispatcher AUTO-STARTS this many persistent
    # caged workers for this profile at session start, so the agent's
    # `botainer-job submit <profile> --hot -- …` runs instantly (no per-task
    # queue wait). 0/None = no auto pool (the agent can still start one at runtime
    # with `botainer-job pool start`). Bounded by max_concurrent + the site
    # policy. A warm worker holds its Slurm allocation while up, so it self-
    # releases after warm_pool_idle_timeout seconds idle.
    warm_pool_size: int | None = None
    warm_pool_idle_timeout: int = 300
    # Per-profile image (MPI/GPU): if set, THIS profile's jobs run in this .sif
    # instead of the session/agent image — so e.g. `mpi` jobs use an MPI-enabled
    # image (built against the cluster's MPI/PMI) while the agent + cpu/gpu jobs
    # stay on the lean base. Absolute path to a .sif (or a docker ref for the
    # docker runtime). None = use the session image.
    image: str | None = None
    # Whole-node allocation (`#SBATCH --exclusive`) — common for MPI to avoid
    # noisy neighbours + get predictable performance.
    exclusive: bool = False
    # `#SBATCH --constraint=<features>` — pin to a node FEATURE set, e.g. a CPU
    # generation (`cascadelake`, `icelake`). Partitions on most clusters mix
    # hardware, so back-to-back benchmark runs otherwise land on different
    # silicon and the numbers aren't comparable. (User request.)
    #
    # OPERATOR-FIXED, never agent-requestable: there is deliberately NO
    # `max_constraint`, and `botainer-job submit` exposes no `--constraint`. The
    # agent picks a PROFILE BY NAME and gets whatever constraint the operator
    # wrote — the same trust shape as `partition` and `account`. Nothing
    # agent-controlled ever reaches this sbatch directive, so the directive
    # cannot be steered by a prompt-injected agent; the charset validator below
    # is defense-in-depth for the OPERATOR's own typo, not the trust boundary.
    #
    # Slurm feature syntax allows `&` (AND), `|` (OR) and `[]` (counts). Only
    # `&` and `|` are permitted here — `[]` is dropped from the charset because
    # it is rare in practice and widens the grammar for no benefit.
    constraint: str | None = None
    # MPI launch flavor (#68 Phase-2, multi-node). None = the child is launched as
    # a plain `srun` (independent tasks) or a single `exec`. Set to "pmix"/"pmi2"
    # to wire the CROSS-NODE MPI handshake: the child is launched as
    # `srun --mpi=<flavor> --export=NONE <PMIx bootstrap shim> <caged apptainer>`
    # so the per-task PMIX_*/PMI_*/SLURM_* env + the per-step PMIx server tmpdir
    # reach the workload THROUGH the §4 cage. The cage flags are UNCHANGED — the
    # shim only injects APPTAINERENV_*/APPTAINER_BIND, which apptainer applies
    # AFTER --cleanenv/--containall clean, so they survive. AUTHOR opt-in, never
    # agent-settable; requires the .sif to carry a PMIx-capable MPI (OpenMPI >= 3.1;
    # PMIx >= 2.1.1 is cross-version compatible, so NO rebuild against the exact
    # host PMIx is needed). See DN-001 §9 Q2 +
    # docs/CAPABILITY-SURFACE.md (the MPI env/socket grant).
    mpi: str | None = None

    @field_validator("constraint")
    @classmethod
    def _validate_constraint(cls, v: str | None) -> str | None:
        """Same intent as _validate_sbatch_str, plus Slurm's feature operators.

        `--constraint` legitimately takes `&` (AND) and `|` (OR), which the
        general sbatch charset excludes. Everything else is identical: refuse
        anything that could terminate the directive or start a shell line.
        """
        if v in (None, ""):
            return v
        if not _SBATCH_CONSTRAINT_SAFE.match(v) or any(ord(ch) < 0x20 for ch in v):
            raise ValueError(
                f"job_profiles constraint {v!r} contains characters outside "
                f"[A-Za-z0-9._:+&|-]; refused (sbatch-injection defense)."
            )
        return v

    @field_validator("partition", "account", "gpu_type", "memory", "time",
                     "max_memory", "max_time")
    @classmethod
    def _validate_sbatch_str(cls, v: str | None) -> str | None:
        # These flow into `#SBATCH` directives on the host-generated sbatch
        # script; refuse anything outside a strict charset so they can never
        # inject a directive / shell line (the dispatcher re-checks at the sink).
        if v in (None, ""):
            return v
        if not _SBATCH_SAFE.match(v) or any(ord(ch) < 0x20 for ch in v):
            raise ValueError(
                f"job_profiles value {v!r} contains characters outside "
                f"[A-Za-z0-9._:+-]; refused (sbatch-injection defense)."
            )
        return v

    @field_validator("modules")
    @classmethod
    def _validate_modules(cls, v: list[str] | None) -> list[str] | None:
        # Module names are auto-loaded via `module load <name…>` inside a caged
        # job (the module preamble is host-generated from THIS list; #68). Restrict
        # to a flat Lmod token so a (git-shareable, untrusted) profile can't inject
        # shell into the preamble. Same charset v0.0.x enforced.
        if v is None:
            return v
        import re as _re
        # \A…\Z, not ^…$ — Python's `$` also matches before a trailing newline, so
        # `^…$` would admit "x\n" and let it break out of the `module load`
        # preamble line (sharp-edges F1). Reject control chars too.
        tok = _re.compile(r"\A[A-Za-z0-9_./+-]+\Z")
        for m in v:
            if not (isinstance(m, str) and m and tok.match(m)
                    and not any(ord(ch) < 0x20 for ch in m)):
                raise ValueError(
                    f"job_profiles module {m!r} is not a valid Lmod name "
                    f"(allowed: letters, digits, and _ . / + -)."
                )
        return v

    @field_validator("image")
    @classmethod
    def _validate_profile_image(cls, v: str | None) -> str | None:
        # The profile image becomes an OPERAND to `apptainer exec … <image> …`
        # (after the fixed §4 cage flags). A leading '-' would be parsed as a
        # flag — an argv-injection / cage-bypass vector — and control chars could
        # smuggle args. The cage flags are still emitted regardless, but reject
        # these so a (git-shareable, untrusted) profile can't shape the runtime.
        if v in (None, ""):
            return v
        if v[0] == "-":
            raise ValueError(
                f"job_profiles image {v!r} must not start with '-' (it is an "
                f"apptainer/docker operand; a leading dash parses as a flag)."
            )
        if any(c in v for c in ("\x00", "\n", "\r", "\t")):
            raise ValueError(
                f"job_profiles image {v!r} must not contain control characters."
            )
        return v

    @field_validator("mpi")
    @classmethod
    def _validate_mpi(cls, v: str | None) -> str | None:
        # The value becomes the SLURM `--mpi=<flavor>` selector on the child's
        # srun line. Restrict to the two flavors botainer wires (PMIx is the
        # default modern path; pmi2 for MPICH-family stacks). `srun --mpi=list`
        # shows what the site supports.
        if v in (None, ""):
            return None
        if v not in ("pmix", "pmi2"):
            raise ValueError(
                f"job_profiles mpi {v!r} must be 'pmix' or 'pmi2' (the SLURM "
                f"--mpi flavor; check `srun --mpi=list` on the cluster)."
            )
        return v

    @field_validator("cpus", "gpus", "max_concurrent", "nodes",
                     "warm_pool_idle_timeout")
    @classmethod
    def _validate_positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"job_profiles resource count must be >= 0 (got {v}).")
        return v

    @field_validator("ntasks", "ntasks_per_node",
                     "max_cpus", "max_gpus", "max_nodes", "warm_pool_size")
    @classmethod
    def _validate_positive_optional(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"job_profiles ntasks/max_* must be >= 1 (got {v}).")
        return v

    @model_validator(mode="after")
    def _reject_unsupported_combos(self) -> "JobProfile":
        # A warm worker holds ONE long-lived allocation and resolves the SESSION
        # image for its loop — it does not (yet) honor a per-profile `image:`. Rather
        # than silently run warm tasks in the wrong .sif, refuse the combination so
        # the author picks one (cold-submit MPI jobs use the custom image; warm
        # pools use the session image). #68 follow-up: teach the worker prof.image.
        if self.image and self.warm_pool_size:
            raise ValueError(
                "job_profiles: `image:` and `warm_pool_size:` cannot be combined "
                "yet — a warm worker runs the session image, so its tasks would "
                "silently use the wrong .sif. Use a custom `image:` with cold "
                "(non-warm) submits, or drop `image:` for the warm pool."
            )
        # MPI / multi-task + warm pool: the warm worker runs a single caged exec
        # (not srun) and its allocation omits ntasks-per-node/exclusive — a hot
        # MPI task would silently run non-parallel. Refuse until the worker
        # srun-launches (#68 follow-up). Enforced again at the runtime chokepoint
        # (_start_pool_workers) for the agent `pool start` path.
        if self.warm_pool_size and (self.nodes > 1 or self.ntasks
                                    or self.ntasks_per_node):
            raise ValueError(
                "job_profiles: `warm_pool_size:` cannot be combined with MPI / "
                "multi-task (`nodes:` > 1, `ntasks:`, `ntasks_per_node:`) yet — a "
                "warm worker doesn't srun-launch across the allocation, so a hot "
                "task would silently run as one non-parallel process. Cold-submit "
                "MPI jobs (drop `warm_pool_size:` on this profile)."
            )
        # `mpi:` wires the cross-node srun+PMIx launch, which only fires on the
        # PARALLEL (srun) path. Without a parallel shape the child takes the single
        # `exec` path and `mpi:` would be a silent no-op — refuse so the misconfig
        # is loud. (This also means mpi implies the parallel shape the warm-pool
        # check above already forbids, so mpi + warm_pool is rejected there.)
        if self.mpi and not is_parallel_shape(self.nodes, self.ntasks,
                                              self.ntasks_per_node):
            raise ValueError(
                "job_profiles: `mpi:` needs a parallel shape — set `ntasks:` > 1 "
                "(and optionally `nodes:` / `ntasks_per_node:`). With a single "
                "task the job runs as one process and the MPI launch does nothing. "
                "(`ntasks: 1` is NOT parallel — the same predicate the dispatcher "
                "uses, so this can't silently drop the MPI launch.)"
            )
        return self


class MountExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    mode: str = "ro"
    reason: str = ""

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        # Task #174: was silently downgraded to RO if user typed 'rww'
        # or other typo. Now: refuse explicitly so the user FIXES the
        # typo instead of getting confusing 'why is this readonly'
        # debugging later.
        if v not in {"ro", "rw"}:
            raise ValueError(
                f"mount.extra.mode must be 'ro' or 'rw' (got {v!r}). "
                f"Common typos: 'rww', 'readonly', 'readwrite' — use 'ro'/'rw'."
            )
        return v


class MountsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra: list[MountExtra] = Field(default_factory=list)
    tmp: bool = False


class CapsKernel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: list[str] = Field(default_factory=list)


class CapsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kernel: CapsKernel = Field(default_factory=CapsKernel)


class ProjectConfig(BaseModel):
    """Parsed Main/.botainer/config.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: str = "config-v1"
    agent: str = "claude"
    profile: str = "default"
    image: str | None = None
    runtime: str = "auto"  # docker, apptainer, mock, auto
    # #53 / T0-2: the agent's in-cage permission posture.
    #   "bypass" (default) — NO in-agent permission prompts. The container
    #      (binds + network + §4 cage) is the only boundary. REQUIRED for
    #      unattended / HPC-batch runs (an sbatch step has no TTY, so an
    #      interactive approval prompt would deadlock the job). Matches v0.0.x's
    #      `permissions: skip` default and the "container is the boundary" thesis.
    #   "prompt" — the agent's stock interactive approval prompts. Only usable
    #      with a TTY (interactive `here`); do NOT use for batch.
    # A site policy may CAP this at "prompt" (see policy.AgentPolicy).
    #
    # HOW IT REACHES THE AGENT — and this comment described the wrong mechanism
    # until 2026-08-31, which matters because this is the text a user reads to
    # understand the field. It said the value is "composed into a
    # --cleanenv-carried BOTAINER_AGENT_PERMISSIONS env var that each agent's
    # entrypoint wrapper maps to its own flags". That WAS built (704c11f,
    # 2026-07-03) and was REVERTED the next day (4fb80b5) after a live cluster
    # transcript showed the agent still prompting: the wrapper lived in a `.sif`
    # built before the fix, so shipping the logic in the image meant it only
    # took effect after a rebuild nobody had done.
    #
    # What actually happens: trusted compose appends the flags to the agent argv
    # from `_AGENT_BYPASS_FLAGS` (composition.py:1010-1014) — claude
    # `--dangerously-skip-permissions`; codex `--sandbox danger-full-access
    # --ask-for-approval never`. One table, in first-party code, so a stale
    # image cannot change the posture.
    #
    # BOTAINER_AGENT_PERMISSIONS is still set in the container env, but ONLY as
    # a posture SIGNAL the agent may read. Both entrypoint wrappers say so in
    # their own comments and neither acts on it; docs/CAPABILITY-SURFACE.md §1
    # says the same. This file was the last place still describing the reverted
    # design. Full derivation: internal design note DN-010.
    agent_permissions: str = "bypass"

    @field_validator("runtime")
    @classmethod
    def _validate_runtime(cls, v: str) -> str:
        # Readiness audit #12: plain str → `runtime: dokcer` typo got a false
        # all-clear from `config check`, then silently fell back to autodetect
        # (or a confusing downstream error). Refuse unknown runtimes at parse.
        valid = {"auto", "docker", "apptainer", "mock"}
        if v not in valid:
            raise ValueError(
                f"runtime must be one of {sorted(valid)} (got {v!r})."
            )
        return v

    @field_validator("agent_permissions")
    @classmethod
    def _validate_agent_permissions(cls, v: str) -> str:
        # #53 / T0-2: fail-closed enum (mirrors runtime + network.mode). A typo
        # like `agent_permissions: bypss` must be refused at parse, not silently
        # treated as "prompt" (which would deadlock a batch job) or "bypass"
        # (which would silently disable prompts the user thought they'd kept).
        #
        # VALIDATION IS TWO-STAGE, and this is the WIDE half. The legal set is
        # per-agent (claude has `acceptEdits`, codex has `on-request`), but the
        # agent is NOT known here: `agent:` is a sibling field, and
        # `botainer start --agent codex` (#112) can override it after parse. So
        # narrowing here would be wrong for `--agent`. This stage catches a
        # typo; compose catches a cross-agent mistake, where `agent:` is
        # settled and the refusal can name what the chosen agent accepts.
        valid = agent_permissions.ALL_KNOWN_VALUES
        if v not in valid:
            raise ValueError(
                f"agent_permissions must be one of {sorted(valid)} (got {v!r}). "
                f"`bypass` and `default` mean the same thing for every agent; "
                f"the rest are the agents' own mode names and are checked "
                f"against the agent you selected when the session is composed."
            )
        return v
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    mounts: MountsConfig = Field(default_factory=MountsConfig)
    caps: CapsConfig = Field(default_factory=CapsConfig)
    env: dict[str, str] = Field(default_factory=dict)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plugins_enabled: list[str] = Field(default_factory=list)
    #: Agent families whose CREDENTIALS are bound without their entrypoint
    #: running. (#230) `agent:` decides WHICH AGENT STARTS; this decides WHOSE
    #: CREDENTIALS ARE PRESENT. They were one decision because one plugin
    #: carried both, which is why `--agent` had to do plugin surgery and why a
    #: message about choosing an agent talked to the user about credentials.
    #:
    #: Values are the SHORT family name (`codex`, `claude`). Naming the running
    #: agent here is a no-op, not an error: its credentials are bound anyway.
    #:
    #: The injected agent's CLI is NOT in the running agent's image — the images
    #: are single-agent (agent-claude installs @anthropic-ai/claude-code only).
    #: `npm install -g <cli>` inside the session puts it in /packages and it
    #: persists; see NPM_CONFIG_PREFIX in the Dockerfiles.
    inject_credentials: list[str] = Field(default_factory=list)
    # #54: named, capped resource shapes the agent may dispatch jobs into.
    job_profiles: dict[str, JobProfile] = Field(default_factory=dict)

    # Optional one-line notes saying what each auth profile is FOR.
    #
    # WHY THIS EXISTS. `docs/CAPABILITY-SURFACE.md` §4cm makes a profile an
    # ACCOUNT boundary, and until now the only thing distinguishing `personal`
    # from `work` was the string itself. A user deciding which account a session
    # is about to spend had a name and nothing else.
    #
    # WHY IT LIVES HERE and not beside the credential. The profile DIRECTORY is
    # bound into the container at /home/agent/.claude and the agent writes
    # there, so a note stored in it would be a label the agent could forge —
    # and this label's whole job is to inform an account decision. Project
    # config is host-side and read-only to the container.
    #
    # It is a LABEL, not a grant: nothing reads it to decide anything, so a
    # wrong or hostile note misinforms and cannot authorise. Deliberately flat
    # (name -> text) so adding one is a single line and creating a profile stays
    # "type a name".
    #
    # Named `profile_notes` rather than `profiles` on purpose: `--profile`
    # already means two unrelated things (#160) and a `profiles:` key sitting
    # next to `profile:` would be a third way to be confused.
    profile_notes: dict[str, str] = Field(default_factory=dict)

    @field_validator("profile_notes")
    @classmethod
    def _profile_notes_are_short_single_line_text(
        cls, v: dict[str, str]
    ) -> dict[str, str]:
        """A note is printed into a terminal listing, so bound its shape.

        Not a security control — the value is host-side and grants nothing —
        but a multi-line or control-character note would wreck the table it
        appears in, and a config that renders badly is a config that gets
        ignored.
        """
        for name, text in v.items():
            if not isinstance(text, str):
                raise ValueError(
                    f"profile_notes[{name!r}] must be text, got "
                    f"{type(text).__name__}."
                )
            if len(text) > 200:
                raise ValueError(
                    f"profile_notes[{name!r}] is {len(text)} characters; keep "
                    f"it under 200 — it is a one-line label, not documentation."
                )
            if any(ch in text for ch in "\n\r\t") or any(
                ord(ch) < 32 for ch in text
            ):
                raise ValueError(
                    f"profile_notes[{name!r}] contains a newline or control "
                    f"character; it is printed as one line in a listing."
                )
        return v

    @model_validator(mode="after")
    def _reject_misplaced_top_level_key_under_plugins(self) -> "ProjectConfig":
        # `plugins:` is a free-form dict (arbitrary plugin names → arbitrary
        # config), so `extra="forbid"` — which guards the TOP level — cannot catch
        # a top-level key mistakenly INDENTED under `plugins:`. That is exactly how
        # a misplaced `job_profiles:` gets silently swallowed (it becomes "config
        # for a plugin named job_profiles" that nothing enables). Reject it loudly:
        # a plugin is never legitimately named after a top-level config field.
        field_names = set(type(self).model_fields) - {"plugins"}
        misplaced = sorted(k for k in self.plugins if k in field_names)
        if misplaced:
            raise ValueError(
                f"{misplaced} is/are nested under `plugins:` but is/are TOP-LEVEL "
                f"config key(s) — move to column 0 (un-indent, out of `plugins:`). "
                f"A misplaced key under `plugins:` is otherwise silently ignored; "
                f"e.g. `job_profiles` must NOT live under `plugins:`."
            )
        return self

    @field_validator("job_profiles")
    @classmethod
    def _validate_job_profile_names(
        cls, v: dict[str, JobProfile]
    ) -> dict[str, JobProfile]:
        # The agent picks a profile BY NAME (`botainer-job submit <name> …`) and
        # the name flows into the mailbox request + is matched against this dict.
        # Constrain it to a flat token so it can't traverse / inject.
        import re as _re
        for name in v:
            if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
                raise ValueError(
                    f"job_profiles name {name!r} must match "
                    f"[A-Za-z0-9][A-Za-z0-9._-]{{0,63}} (a flat token)."
                )
        return v

    @field_validator("agent")
    @classmethod
    def _validate_agent(cls, v: str) -> str:
        # AC7 validator-parity audit: the CLI `--agent` flag is
        # checked against known plugins by cli/init._validate_agent_name, but
        # the config-sourced `agent:` field had NO validator — so
        # `agent: "../../etc/foo"` flowed through `_agent_plugin_name`
        # ("agent-../../etc/foo") into `_resolve_apptainer_sif_path`, which
        # builds `<state>/images/botainer-{agent_name}.sif` — a path the '..'
        # traverses out of (a read-only `.exists()` probe, LOW, but the same
        # config-vs-CLI asymmetry class as the image/uuid sinks). Reject the
        # traversal/flag charset at the type level. Empty is allowed (a
        # "no agent" project; `_agent_plugin_name` returns None for it).
        if v:
            if "/" in v or ".." in v:
                raise ValueError(
                    f"agent {v!r} must not contain '/' or '..' — it becomes a "
                    f"plugin name (agent-<v>) used in filesystem paths; a "
                    f"traversal would escape the images/plugin dir."
                )
            if v[0] == "-":
                raise ValueError(
                    f"agent {v!r} must not start with '-' (would be parsed as "
                    f"a flag where the resolved name reaches argv)."
                )
            if any(c in v for c in ("\x00", "\n", "\r", "\t", " ")):
                raise ValueError(
                    f"agent {v!r} must not contain whitespace/control chars."
                )
            # CANONICAL SHORT FORM, because two readers disagreed about the
            # prefixed one. `agent: agent-claude` is accepted everywhere inside
            # botainer (`_agent_plugin_name` and `_agent_variant_for_mode` both
            # tolerate it) and the standalone hpc-launcher helper does not: it
            # builds `agent-{value}` unconditionally, so `hpc submit` resolved
            # `botainer-agent-agent-claude.sif` and refused naming a file the
            # user never typed, while `start` launched the real one. Measured by
            # a refuting review. `botainer init --agent agent-claude` can write
            # this state itself, so refusing it outright would refuse a config
            # botainer produced.
            #
            # So: strip ONE `agent-` prefix and store the short form. A SECOND
            # one is not a spelling confusion and is refused by name — silently
            # accepting `agent-agent-claude` would be the same class of
            # tolerance that caused this.
            if v.startswith("agent-"):
                v = v[len("agent-"):]
                if v.startswith("agent-"):
                    raise ValueError(
                        f"agent {v!r} still starts with 'agent-' after one "
                        f"prefix was stripped. Write the SHORT name — "
                        f"`agent: claude`, not `agent: agent-agent-claude`; "
                        f"botainer adds the `agent-` prefix itself."
                    )
                if not v:
                    raise ValueError(
                        "agent 'agent-' names no agent. Write the short name, "
                        "e.g. `agent: claude`."
                    )
        return v

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, v: str) -> str:
        # AUDIT (C1 sibling): the config-sourced `profile:` field
        # had NO validator, yet profile flows into SessionSpec.profile and
        # (once wired into the plugin subprocess env — see start.py's
        # BOTAINER_PROFILE TODO) into the per-agent `profiles/<profile>`
        # apptainer --bind source + on-host mkdir/chmod. A '..'/'/' value
        # would traverse to an arbitrary host dir bound read-write into the
        # container — the same class as the `agent` field above. Constrain to
        # a flat token at the type level (mirror of cli/start.py _PROFILE_RE
        # and the standalone host_helper check).
        return validate_profile_name(v)


def _explain_config_error(raw: dict, exc: Exception) -> str:
    """Turn a pydantic dump into something a human can act on.

    Unknown keys should identify the misspelled name and suggest valid keys.
    For example, `job-profiles` is a near miss for `job_profiles`; the raw
    extra-field error alone does not tell the user how to correct it.

    Three things are added, cheapest first:
      1. NEAR-MISS suggestion (difflib). A hyphen-for-underscore typo is the
         single most likely config mistake and is mechanically detectable.
      2. The nesting footgun, called out by name: `job_profiles` under
         `plugins:` is silently ignored (see the model validator), which is a
         different failure needing a different fix.
      3. WHERE TO LOOK: the schema command and the shipped example that
         demonstrates the key in question.
    """
    import difflib

    known = sorted(ProjectConfig.model_fields)
    text = str(exc)
    lines: list[str] = [f"config schema mismatch: {exc}"]

    # Unknown top-level keys -> did you mean ...?
    unknown = [k for k in raw if isinstance(k, str) and k not in known]
    if unknown:
        lines.append("")
        for key in unknown:
            # difflib alone covers the realistic typos, INCLUDING the
            # hyphen/underscore swap that prompted this. An explicit swap check
            # was written first and then deleted: mutation-testing showed it
            # changed no outcome (job-profiles, plugins-enabled,
            # agent-permissions and "job profiles" all resolve without it), and
            # a rule that backs up no demonstrated gap is dead code.
            near = difflib.get_close_matches(key, known, n=2, cutoff=0.6)
            if near:
                lines.append(
                    f"  `{key}` is not a config key — did you mean "
                    f"`{near[0]}`?" + (f" (or `{near[1]}`)" if len(near) > 1 else "")
                )
            else:
                lines.append(f"  `{key}` is not a config key.")
        lines.append(f"  Valid top-level keys: {', '.join(known)}")

    # The nesting footgun: right key, wrong level. Silently ignored otherwise.
    plugins_block = raw.get("plugins")
    if isinstance(plugins_block, dict):
        misplaced = sorted(k for k in plugins_block if k in known and k != "plugins")
        if misplaced:
            lines.append("")
            lines.append(
                f"  {misplaced} appear under `plugins:` but are TOP-LEVEL keys. "
                f"Un-indent them to column 0 — nested there they are ignored.")

    lines.append("")
    lines.append("  Where to look:")
    lines.append("    botainer schema config      # the full schema, authoritative")
    lines.append("    examples/                   # working configs you can copy")
    if any("job" in str(k) for k in unknown) or "job_profiles" in text:
        lines.append("    examples/hpc-job-profiles.yaml   # job_profiles, worked")
    return "\n".join(lines)


def load_config(project_root: Path) -> ProjectConfig:
    path = project_root / HOST_MANAGED_DIR / CONFIG_FILENAME
    if not path.exists():
        raise Refused(
            RefusalCategory.CONFIG_MISSING,
            f"no {HOST_MANAGED_DIR}/{CONFIG_FILENAME} in {project_root}",
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise Refused(RefusalCategory.CONFIG_INVALID, f"yaml parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise Refused(RefusalCategory.CONFIG_INVALID, "config.yaml top-level must be a mapping")
    # Migrate api-only → endpoint-ip-allowlist quietly (codex HIGH 5 rename).
    net = raw.get("network", {})
    if isinstance(net, dict) and net.get("mode") == "api-only":
        net["mode"] = "endpoint-ip-allowlist"
    try:
        return ProjectConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError lacks a stable type across versions
        raise Refused(
            RefusalCategory.CONFIG_SCHEMA_MISMATCH,
            _explain_config_error(raw, exc),
        ) from exc


def _resolve_default_auth_mode() -> str:
    """Read policy.yaml's default_auth_mode (host-wide). Empty if unset.

    Per internal design note DN-040 §9 + codex 45#6 follow-up: `botainer init`
    honors this so a user who set `botainer policy set default_auth_mode
    shared` (or `botainer auth use shared --global`) gets shared-mode
    projects by default."""
    try:
        from botainer.core import policy as policy_module
        return policy_module.load_user_policy().default_auth_mode or ""
    except Exception:
        return ""


def _agent_variant_for_mode(agent: str, mode: str) -> str:
    """Return the plugin name for an (agent_short, mode) pair.

    `agent` is the SHORT form (e.g. "claude"); we return the full plugin
    name (e.g. "agent-claude-shared"). Mode-suffix convention is
    documented in internal design note DN-040 §1.
    """
    base = agent if agent.startswith("agent-") else f"agent-{agent}"
    if mode == "isolated" or not mode:
        return base
    return f"{base}-{mode}"


def write_initial_config(
    project_root: Path,
    *,
    agent: str,
    force: bool,
    runtime: str = "auto",
) -> Path | None:
    """Write the project's starting config. Returns the backup path, or None.

    `force` overwrites an existing config.yaml — that is the whole point of the
    flag — so THE FILE IT REPLACES IS KEPT FIRST, here rather than in the
    caller. One chokepoint: nothing can reach the overwrite without passing the
    backup, which is the difference between a property and a rule someone has
    to remember. (`botainer init --force` refused on every existing project
    until 2026-09-17, so this overwrite had never once run against a file a
    user had edited; making the flag work is what created the hazard.)

    `shutil.copy2` rather than a read-and-write, so the copy inherits the
    original's mode: a backup must never be more readable than what it copies.

    ONE `.bak`, overwritten, not a timestamped series — a per-run file is an
    unbounded write into the user's project, and the realistic use of `--force`
    is once. The trade is stated in the `--force` help rather than left for
    someone to discover.

    The backup is NOT reachable by the agent: `/workspace/.botainer` is
    null-bound in the mount plan and exactly one file is re-mounted inside it
    (AGENT_ACCESS.txt, ro).
    """
    path = project_root / HOST_MANAGED_DIR / CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return None
    backup: Path | None = None
    if path.exists():
        import shutil as _shutil
        backup = path.with_suffix(path.suffix + ".bak")
        _shutil.copy2(path, backup)
    # Resolve which variant of the agent plugin to enable based on
    # the host-wide default_auth_mode policy.
    default_mode = _resolve_default_auth_mode()
    agent_plugin = _agent_variant_for_mode(agent, default_mode)
    # Fall back to the isolated variant if the chosen-mode plugin isn't
    # installed on this host (e.g. policy says shared but only -isolated
    # is bundled). list_installed is the source of truth.
    try:
        from botainer.plugins.lifecycle import list_installed
        installed = {p.name for p in list_installed()}
        if agent_plugin not in installed and default_mode:
            fallback = _agent_variant_for_mode(agent, "isolated")
            import sys as _sys
            _sys.stderr.write(
                f"[botainer init] policy default_auth_mode={default_mode!r} "
                f"requested {agent_plugin!r} but it isn't installed; "
                f"falling back to {fallback!r}. Run `botainer setup` to "
                f"install all bundled variants.\n"
            )
            agent_plugin = fallback
            # The note below is written into the USER'S OWN config.yaml and
            # outlives every session. It used to interpolate the REQUESTED
            # default_mode, so after this fallback it told them "this project
            # stays on 'shared'" while recording the isolated plugin one line
            # above. Correct it to what was actually written.
            default_mode = "isolated"
    except Exception:
        pass
    inherited_note = (
        f"   # captured from policy.default_auth_mode at init time;\n"
        f"                            # this project stays on {default_mode!r} even if you\n"
        f"                            # change the policy default later. Switch with\n"
        f"                            # `botainer auth use "
        f"{_auth_modes.mode_list_hint()}`.\n"
        if default_mode
        else "\n"
    )
    # Task #127: HPC-aware init. When runtime=apptainer, the user almost
    # certainly wants hpc-launcher + hpc-modules in plugins_enabled and
    # populated plugins.hpc-launcher defaults. Docker-shaped template
    # left HPC users to hand-edit ~6 spots.
    _is_hpc = (runtime == "apptainer")
    if _is_hpc:
        # hpc-launcher is deliberately ABSENT here. It is `kind: service-host`
        # with no hooks and no container contributions, so listing it in
        # plugins_enabled changes nothing — `botainer hpc submit` works off the
        # INSTALLED plugin, and the in-container `botainer-job` is wired by
        # `job_profiles`, not by enabling. Telling users to enable it produced a
        # real confusion (2026-07-28: jobs ran fine unenabled and the user could
        # not tell whether something was wrong). hpc-modules DOES have a
        # host_pre_launch hook, so it genuinely must be enabled.
        hpc_plugins_enabled = (
            "  - hpc-modules               # capture `module load` env from cluster Lmod\n"
        )
        hpc_plugins_block = (
            "  hpc-launcher:\n"
            "    # Required on most clusters — fill in from your `botainer hpc info`.\n"
            "    partition: \"\"             # e.g. day / gpu / scavenge\n"
            "    account: \"\"               # e.g. pi_yourgroup\n"
            "    time_minutes: 240          # 4h default; raise for longer jobs\n"
            "    cpus: 4                    # cores per task\n"
            "    memory_gb: 16              # RAM (GB)\n"
            "    gpus: 0                    # 0 = no GPU\n"
            "    # gpu_type: a100           # uncomment + set for specific GPU type\n"
            "    max_concurrent_jobs: 3     # refuse `bot hpc submit` past this count\n"
            "  hpc-modules:\n"
            "    modules: []                # e.g. [python/3.11, gcc/13, cuda/12.3]\n"
            "    purge_first: true          # `module purge` before loading\n"
            "    fail_on_missing: true      # refuse session if a module isn't found\n"
        )
        # Network default is 'internet' for BOTH runtimes (frictionless
        # onboarding; matches the user-policy ceiling default — re-audit #2). On
        # HPC compute nodes outbound is often blocked; the _net_comment below
        # tells the user to set 'none' + pre-install if so. (Re-audit round 4 #4:
        # the old comment said "Default to 'none'", contradicting the code.)
        _net_mode_default = "internet"
        _net_comment = (
            "                               # HPC NOTE: compute nodes are often\n"
            "                               # network-isolated. If your cluster blocks\n"
            "                               # outbound from compute, set mode: none\n"
            "                               # and pre-install everything in the .sif.\n"
        )
        _runtime_note = " # (HPC-aware init: hpc-launcher + hpc-modules pre-enabled)"
    else:
        hpc_plugins_enabled = ""
        hpc_plugins_block = ""
        _net_mode_default = "internet"
        _net_comment = ""
        _runtime_note = ""

    # The permission words THIS agent understands, listed by name. Writing a
    # generic list would reproduce the defect this block exists to fix: a
    # config comment that names values the selected agent does not accept.
    # `bypass` and `default` are handled above (they mean the same for every
    # agent); this is the agent's OWN vocabulary.
    _perm_short = agent[len("agent-"):] if agent.startswith("agent-") else agent
    _perm_family = ("anthropic" if _perm_short.startswith("claude")
                    else "openai" if _perm_short.startswith("codex") else None)
    _perm_own = sorted(
        m for m in agent_permissions.modes_for(_perm_family)
        if not agent_permissions.is_botainer_word(m)
    )
    _perm_own_words = " | ".join(_perm_own) if _perm_own else "(this agent has no others)"
    # `default` does not mean "append nothing" for every agent, and saying so
    # where it is false is the defect this rewrite exists to remove. codex's own
    # default sandbox cannot START in a container, so botainer pins that ONE
    # axis and leaves the asking to codex. Stated here rather than hidden.
    _perm_pin_note = ""
    if agent_permissions.argv_for(_perm_family, "default"):
        _perm_pin_note = (
            f"                               #   NOTE for {_perm_short}: botainer still pins\n"
            f"                               #   `--sandbox danger-full-access`, because\n"
            f"                               #   {_perm_short}'s own sandbox cannot start inside a\n"
            f"                               #   container. The container is the boundary.\n"
        )
    if _perm_own:
        _lines = [
            f"                               # {_perm_short}'s own modes, passed straight"
            f" through:\n"
        ]
        for _m in _perm_own:
            _gloss = agent_permissions.gloss_for(_perm_family, _m) or ""
            _lines.append(
                f"                               #   {_m}: {_gloss}\n")
        for _m, _why in sorted(
                agent_permissions.REFUSED.get(_perm_family or "", {}).items()):
            _lines.append(
                f"                               # {_m} is REFUSED — {_why.split('.')[0]}.\n")
        _perm_own_words_block = "".join(_lines)
    else:
        _perm_own_words_block = ""

    content = (
        f"# {HOST_MANAGED_DIR}/{CONFIG_FILENAME} — project config (git-shareable).\n"
        f"# Describes the capabilities your agent gets in this project. Host-only\n"
        f"# state (UUID path history, credentials) lives elsewhere.\n"
        f"#\n"
        f"# Values here are frozen at `botainer init` time from your policy\n"
        f"# defaults. Changing policy later does NOT propagate to existing\n"
        f"# projects; edit this file or use `botainer auth use` / similar.\n"
        f"\n"
        f"version: config-v1\n"
        # The SHORT form, always — see `_validate_agent`. `init --agent
        # agent-claude` is a thing people do, and writing it back verbatim is
        # what let the two resolvers disagree.
        f"agent: {agent[len('agent-'):] if agent.startswith('agent-') else agent}\n"
        f"                               # The OTHER agent is `codex`. Both are\n"
        f"                               # supported; this line picks the default.\n"
        f"                               #   one session only:  botainer start --agent codex\n"
        f"                               #   preview it first:   botainer inspect --agent codex\n"
        f"                               #   permanently:        botainer config set agent codex\n"
        f"                               # Each agent has its OWN image, login and\n"
        f"                               # history; /workspace, /packages and /scratch\n"
        f"                               # are shared between them.\n"
        f"profile: default\n"
        f"agent_permissions: bypass      # bypass | default | {_perm_own_words}\n"
        f"                               # bypass (the default): botainer turns the\n"
        f"                               #   agent's OWN permission system off, so nothing\n"
        f"                               #   asks. REQUIRED for unattended / HPC-batch\n"
        f"                               #   runs — there is no TTY to answer a prompt on.\n"
        f"                               # default: botainer does not choose whether you\n"
        f"                               #   are asked. That becomes the agent's own\n"
        f"                               #   decision — NOT a promise botainer can make\n"
        f"                               #   on its behalf.\n"
        f"{_perm_pin_note}"
        f"{_perm_own_words_block}"
        f"                               # A site policy may cap this; see\n"
        f"                               #   `botainer policy show`.\n"
        f"runtime: {runtime}                  # docker (laptop) | apptainer (HPC) | mock | auto"
        f"{_runtime_note}\n"
        f"\n"
        f"network:\n"
        f"  mode: {_net_mode_default}               # none | internet | endpoint-ip-allowlist\n"
        f"                               # `internet` lets pip/npm/git clone work without\n"
        f"                               # any SSH forwarding setup. Set to `none` for\n"
        f"                               # sensitive work; agent can't exfil over HTTP.\n"
        f"{_net_comment}"
        f"  endpoints: []                # endpoint groups when mode=endpoint-ip-allowlist\n"
        f"\n"
        f"# CPU + memory limits. DOCKER ONLY — `--cpus <N> --memory <M>m`.\n"
        f"# Apptainer has no exec-time cgroup support, so setting either of\n"
        f"# these REFUSES an apptainer launch rather than silently ignoring it.\n"
        f"# For a cluster job, size it where the sbatch request is actually\n"
        f"# built: plugins.hpc-launcher.cpus / .memory_gb (see below).\n"
        f"# Null = no limit (subject to policy).\n"
        f"resources:\n"
        f"  cpu: null                    # integer cores   (docker only)\n"
        f"  memory_mb: null              # integer MB      (docker only)\n"
        f"\n"
        f"mounts:\n"
        f"  # Extra bind mounts. `extra: []` is the empty form; a real entry needs\n"
        f"  # source + target, and takes an optional mode and reason. To add one,\n"
        f"  # delete the `[]` and uncomment (keeping the indentation):\n"
        f"  #\n"
        f"  #   extra:\n"
        f"  #     - source: /absolute/path/on/the/host\n"
        f"  #       target: /data/inputs      # where it appears INSIDE the container\n"
        f"  #       mode: ro                  # ro (default) | rw — any other value is refused\n"
        f"  #       reason: raw inputs        # optional free text, for your future self\n"
        f"  #\n"
        f"  # Both ends are policy-checked, and the TARGET is the one that usually\n"
        f"  # bites: it must sit under an allowed prefix. To see the prefixes your\n"
        f"  # policy allows, run this on the HOST (not inside a session):\n"
        f"  #     botainer policy show      # the mounts.extra_targets line\n"
        f"  extra: []\n"
        f"  tmp: false                   # RESERVED / no effect. On docker, /tmp is\n"
        f"                               # ALWAYS a RAM tmpfs now (ephemeral, off the\n"
        f"                               # VM disk); this field is a v0.1 vestige (a\n"
        f"                               # disk-backed-/tmp opt-out may land in v0.2).\n"
        f"\n"
        f"caps:\n"
        f"  kernel:\n"
        f"    keep: []                   # kernel caps to keep; empty by default\n"
        f"\n"
        f"env: {{}}                       # env vars to pass through (denylisted vars rejected)\n"
        f"\n"
        f"plugins_enabled:\n"
        f"  - {agent_plugin}"
        + inherited_note
        + "  - git                       # guarded mode by default (see plugins.git below)\n"
        + hpc_plugins_enabled
        + "\n"
        "  # Opt-in plugins (uncomment to enable). `botainer plugin info\n"
        "  # <name>` for details about each.\n"
        "  #\n"
        "  # - nudge                   # `botainer nudge \"continue\"` from another shell\n"
        "  #                           # injects text into the agent's prompt (e.g. after\n"
        "  #                           # a rate-limit clears). Wraps the agent in a host-\n"
        "  #                           # side `screen` session — changes copy/paste\n"
        "  #                           # behavior. Requires `screen` on host PATH.\n"
        "  # - web-ports               # Forward TCP ports the agent serves (Jupyter /\n"
        "  #                           # Streamlit / Gradio inside the container) so you\n"
        "  #                           # can reach them at localhost:<port> on the host.\n"
        "  # - agent-claude-broker     # The real token never enters the\n"
        "  #                           # container: it stays host-side and the\n"
        "  #                           # container sees only an ephemeral\n"
        "  #                           # session token. Mutually exclusive with\n"
        "  #                           # the default credential mount; pick one.\n"
        # hpc-launcher is NOT offered here: it contributes nothing at compose,
        # so enabling it does nothing (PluginManifest.enabling_is_inert).
        # `botainer hpc submit` uses the INSTALLED plugin; jobs are wired by
        # `job_profiles`. Offering the toggle is what confused a user in
        #. hpc-modules has a real hook and IS offered.
        "  # - hpc-modules             # `module load` env capture (HPC only).\n"
        "\n"
        "# Per-plugin settings live inline below. The structure mirrors\n"
        "# `plugins_enabled` above; each enabled plugin can have an entry here.\n"
        "plugins:\n"
        "  git:\n"
        "    mode: guarded              # guarded | off\n"
        "                               # `guarded` (default): overlay .git/hooks/ as\n"
        "                               # read-only inside the container so the agent\n"
        "                               # can't install host-executed git hooks.\n"
        "                               # `off`: no overlay; only safe if you trust\n"
        "                               # the agent + the repo's hook history.\n"
        "  #\n"
        "  # web-ports:                # Container → host port forwarding (incoming on host).\n"
        "  #   ports:\n"
        "  #     - 8888                # container:8888 → host:127.0.0.1:8888\n"
        "  #     - {container: 7860, host: 7860, label: \"gradio\"}\n"
        "  # hpc-modules:\n"
        "  #   modules: [python/3.11, gcc/13, cuda/12.3]\n"
        "  # hpc-launcher:\n"
        "  #   partition: day            # cluster-specific; see `botainer hpc setup --cluster=`\n"
        "  #   account: YOUR_GROUP       # required on most clusters\n"
        "  #   time_minutes: 240\n"
        "  #   max_concurrent_jobs: 3    # refuse `botainer hpc submit` past this count\n"
        + hpc_plugins_block
    )
    path.write_text(content, encoding="utf-8")
    return backup
