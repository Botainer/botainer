"""SessionSpec — the typed, immutable description of what the launcher will run.

The SessionSpec is the single source of truth between the launcher's composition
phase (config + policy + plugins → SessionSpec) and the adapter (SessionSpec →
runtime argv). Adapters must never look at config.yaml or the user environment;
they only see the SessionSpec.

The MountPlan inside the SessionSpec is also typed and validated: every bind has
provenance, a self-test, and a mode. Adapters render the MountPlan into a runtime
argv via `--mount type=bind,...` (Docker) or `--bind` (Apptainer) — never shell
string concatenation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# AC7 hole-hunt: the image is appended as a POSITIONAL to
# `docker run ... <image>` / `apptainer exec ... <image>` with no `--`
# end-of-options separator (docker run has no such convention for the
# image operand). A value beginning with '-' is therefore parsed by
# docker as a FLAG: `image: "--privileged"` injected a privileged
# container that defeated `--cap-drop ALL` + `--security-opt
# no-new-privileges`. SessionSpec.image was a bare `str` with no
# validator, so `SessionSpec(image="--privileged")` was ACCEPTED. This
# validator is the type-level chokepoint that closes that hole for every
# construction path (compose, plugin, test, fuzz).
_IMAGE_FORBIDDEN_CHARS = ("\x00", "\n", "\r", "\t", " ")


def validate_image_reference(value: str) -> str:
    """Reject argv/shell-hostile image references. Returns the value if OK.

    Accepts an OCI reference (`[registry/]repo[:tag][@sha256:...]`,
    local-build tag) or an absolute `.sif` path. Rejects: empty, a
    leading '-' (flag injection), and any NUL / newline / CR / tab /
    space (an image ref or absolute path contains none of these). The
    charset beyond that is left generous on purpose — the security
    property is "cannot be parsed as a flag and cannot break argv
    tokenization", not a full OCI-grammar parser that might reject a
    legitimate local tag.
    """
    if not value:
        raise ValueError("image must be non-empty")
    if value[0] == "-":
        raise ValueError(
            f"image {value!r} starts with '-'; it would be parsed as a "
            f"docker/apptainer flag (argv injection — e.g. '--privileged' "
            f"would defeat --cap-drop). Image references and absolute .sif "
            f"paths never start with '-'."
        )
    bad = [c for c in _IMAGE_FORBIDDEN_CHARS if c in value]
    if bad:
        raise ValueError(
            f"image {value!r} contains forbidden character(s) {bad!r} "
            f"(NUL/newline/CR/tab/space); a valid image reference or "
            f"absolute .sif path has none."
        )
    return value


_UUID_FORBIDDEN_CHARS = ("\x00", "\n", "\r", "\t")


def validate_project_uuid(value: str) -> str:
    """Reject injection chars in a project UUID. Returns the value if OK.

    project_uuid is interpolated into filesystem paths and (on the HPC
    submit path) into the generated sbatch script as literal text. A
    newline/CR/NUL/tab would inject `#SBATCH` directives or shell lines
    (confirmed HIGH via a tampered, git-shareable `.botainer/project-id`).
    This rejects exactly that injection charset. It deliberately does NOT
    enforce the canonical-UUID grammar — placeholder ids appear in tests
    and some compose paths; the canonical contract is enforced at the
    real sources (identity._validate_uuid) and in SubmissionPlan.
    """
    bad = [c for c in _UUID_FORBIDDEN_CHARS if c in value]
    if bad:
        raise ValueError(
            f"project_uuid {value!r} contains forbidden character(s) {bad!r} "
            f"(NUL/newline/CR/tab); a project id never contains these. They "
            f"would inject sbatch directives / shell lines into the generated "
            f"job script (argv/script injection)."
        )
    return value


# Canonical profile-name charset. Keep in sync with the standalone mirror in
# plugins/hpc-launcher/host_helper/_common.py and the CLI check in
# botainer/cli/start.py (_PROFILE_RE).
PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def validate_profile_name(value: str) -> str:
    """Reject path-hostile profile names. Returns the value if OK.

    AUDIT (C1 sibling): `profile` is interpolated into apptainer
    `--bind` SOURCE/TARGET paths and an on-host `mkdir(parents=True)` +
    `chmod 0o700` (the per-agent `profiles/<profile>` dir), structurally the
    same sink as `agent`. A `..`/`/` value traverses to an arbitrary host dir
    bound read-write into the container. Constrain to a flat token
    (`^[a-z][a-z0-9_-]{0,31}$`) — no '/', no '.', so traversal is impossible.
    This is the type-level backstop mirrored at the source (CLI `--profile`),
    in the standalone HPC SubmissionPlan, and on the laptop ProjectConfig.
    """
    if not PROFILE_NAME_RE.fullmatch(value):
        raise ValueError(
            f"profile {value!r} must match ^[a-z][a-z0-9_-]{{0,31}}$ — it "
            f"becomes a filesystem path component (profiles/<profile>) used as "
            f"an apptainer --bind source and an on-host mkdir; '/' or '.' would "
            f"traverse out into an arbitrary host directory."
        )
    return value


class BindMode(str, Enum):
    RO = "ro"
    RW = "rw"
    # UNIX_SOCKET + FIFO have branching code in mount_plan/render.py
    # (docker 26-34, apptainer 52-54) + inspect/preflight.py. As of T1-1
    # a plugin pre_session `"mode":"unix-socket"` contribution
    # IS constructed with this mode (composition.run_pre_session_hooks) — it
    # was previously collapsed to RW, which rendered identically (a plain rw
    # bind, no `:ro`) but made composition._refuse_cross_node_binds (which keys
    # on UNIX_SOCKET/FIFO) DEAD for plugin sockets. Keeping the mode lets that
    # HPC cross-node refusal fire while preserving the docker/direct render.
    UNIX_SOCKET = "unix-socket"
    FIFO = "fifo"
    NULL_BIND = "null-bind"


class AgentRendering(str, Enum):
    """How the agent sees this bind in its AGENT_ACCESS.txt view.

    Renamed from `Visibility.USER_ONLY` per codex MEDIUM 5 — the old name
    suggested secrecy from the agent, but `/proc/self/mounts` is always
    readable. This is rendering, not protection.
    """

    SHOWN = "shown"
    SUMMARIZED = "summarized"


class Provenance(str, Enum):
    CORE = "core"
    PROJECT = "project"
    PLUGIN = "plugin"
    USER = "user"
    SITE_POLICY = "site-policy"


class Bind(BaseModel):
    """One bind mount.

    `source` is a host path; `target` is the container path. `mode` is the
    intended access mode; `agent_rendering` is how the agent sees it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    mode: BindMode
    provenance: Provenance
    provenance_detail: str = ""
    agent_rendering: AgentRendering = AgentRendering.SHOWN
    self_test: str = ""  # Name of the self-test that verifies this bind at preflight.
    nested_under: str | None = None  # If this bind sits "on top of" another bind.

    def is_socket(self) -> bool:
        return self.mode in (BindMode.UNIX_SOCKET, BindMode.FIFO)


class MountPlan(BaseModel):
    """Closed list of binds. Validated by mount_plan.validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    binds: tuple[Bind, ...] = Field(default_factory=tuple)

    def with_many(self, binds: list[Bind]) -> MountPlan:
        return MountPlan(binds=(*self.binds, *binds))


class NetworkMode(str, Enum):
    NONE = "none"
    INTERNET = "internet"
    # Renamed from `api-only` per codex HIGH 5. User-facing label still allows
    # `api-only` as an alias on read; the canonical internal name is endpoint-ip-allowlist.
    ENDPOINT_IP_ALLOWLIST = "endpoint-ip-allowlist"


class NetworkSpec(BaseModel):
    """#175: host_services field + HostServicePort class
    removed. The aspirational use case ("agent reaches a named host
    service like a local Wolfram kernel") landed instead as
    BindMode.UNIX_SOCKET binds (see plugins/wolfram-sidecar). No
    adapter ever consumed host_services; the inspect/tree render
    just printed "(DECLARED but not currently exposed)" — a lie about
    what the field would do."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: NetworkMode = NetworkMode.NONE
    endpoints: tuple[str, ...] = Field(default_factory=tuple)


class PortForward(BaseModel):
    """A TCP port forwarded from the container to the host.

    The web-ports plugin contributes these. The Docker adapter emits
    `-p <host_bind>:<host_port>:<container_port>`. Apptainer doesn't
    have built-in port forwarding so it refuses these specs.

    `host_bind: 127.0.0.1` (default) keeps the forwarded port reachable
    only from the host's loopback. Any other process on the host can
    still reach it — surfaced in the capability summary.

    host_bind validated via Pydantic so direct construction can't bypass
    the loopback restriction (defense in depth alongside the composition
    layer's check).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    container_port: int = Field(ge=1, le=65535)
    host_port: int = Field(ge=1, le=65535)
    host_bind: str = "127.0.0.1"
    label: str = ""  # optional friendly name shown in capability summary

    @field_validator("host_bind")
    @classmethod
    def _validate_host_bind(cls, v: str) -> str:
        """Refuse any host_bind that could expose the port beyond loopback.

        Sharp-edges / insecure-defaults review (CRITICAL): a string-equality
        check on "0.0.0.0" misses "::", "*", empty string, and bare
        hostnames that resolve to routable NICs. Allowlist is the right
        primitive.
        """
        if v not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(
                f"host_bind {v!r} not allowed; v0.1.0 restricts to loopback "
                f"({sorted({'127.0.0.1', '::1', 'localhost'})})"
            )
        return v


class ResourceSpec(BaseModel):
    """Per-session resource caps. Only fields that an adapter reads
    live here — HPC scheduling parameters (time_minutes, gpus,
    partition, account, gpu_type) belong on the SubmissionPlan in
    plugins/hpc-launcher/, which reads them directly from
    cfg.resources at submit time. Putting them on SessionSpec too
    was duplication: the spec carried them but no adapter ever
    consulted them. #175 — removed."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cpu: int | None = None
    memory_mb: int | None = None


class KernelCapsSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    keep: tuple[str, ...] = Field(default_factory=tuple)


class SidecarSpec(BaseModel):
    """One sidecar declaration.

    Runtime distinction per Codex HIGH 10:
    - `runtime: container` → L1 sidecar bounded by sidecar capabilities.
    - `runtime: host_helper` → HOST subprocess; requires explicit user consent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    runtime: Literal["container", "host_helper"]
    image: str | None = None
    command: tuple[str, ...] = Field(default_factory=tuple)
    own_capabilities: tuple[str, ...] = Field(default_factory=tuple)
    lifetime: Literal["session-scoped", "request-scoped"] = "session-scoped"


class HookSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plugin: str
    when: Literal[
        "pre_session", "post_session", "host_pre_launch",
    ]
    script_path: str  # Absolute host path to the hook script.
    timeout_seconds: int = 30


class EnvSpec(BaseModel):
    """Environment variables passed into the container.

    Per sharp-edges #6: keys are validated against `^[A-Za-z_][A-Za-z0-9_]*$`
    so an attacker-controlled key can't smuggle through argv (e.g., a key
    starting with `-` or containing `=`, `\\n`, etc.). Values are likewise
    checked for `\\0` and `\\n` which would corrupt the env block.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    values: Mapping[str, str] = Field(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        for k, v in self.values.items():
            if not _ENV_KEY_PATTERN.fullmatch(k):
                raise ValueError(
                    f"env var name {k!r} is not a valid POSIX identifier "
                    f"(must match ^[A-Za-z_][A-Za-z0-9_]*$)"
                )
            if "\x00" in v or "\n" in v:
                raise ValueError(f"env var {k!r} value contains NUL or newline")
        # Task #241: freeze the underlying dict so frozen=True is true.
        # Pydantic's frozen=True only blocks attribute reassignment, not
        # mutation of mutable field values. MappingProxyType is the stdlib
        # read-only view.
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @field_serializer("values")
    def _serialize_values(self, v: Mapping[str, str]) -> dict[str, str]:
        # MappingProxyType isn't JSON-serializable directly; unwrap to dict.
        return dict(v)


class CapabilityGrant(BaseModel):
    """A capability with its value, validated, post-intersection."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    value: Any = None
    provenance: Provenance = Provenance.PROJECT
    # Which plugin declared this, when provenance is PLUGIN. Dist audit
    #: a plugin's manifest `capabilities:` list declares capability
    # NAMES it may contribute to, with no value known at compose time — so
    # `botainer inspect` rendered every one as `= None`, and two plugins
    # declaring the same name produced two identical, unattributed rows. On the
    # surface the README sells as "the user sees everything before anything
    # runs", that reads as "this capability is unset" and hides who asked for
    # it. Carrying the plugin name lets the renderer say what is actually true.
    source_plugin: str | None = None


class SessionSpec(BaseModel):
    """The whole picture: what the launcher will execute.

    Immutable once composed. Adapter receives this, renders argv, executes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    project_uuid: str
    project_root: str
    state_dir: str
    runtime: Literal["docker", "apptainer", "mock"]
    image: str  # name@sha256:... or local-build-tag or absolute .sif path
    # #175 partial: SessionSpec.image_digest field was
    # never written during compose_session. The spec.image string
    # itself already encodes the digest (name@sha256:...) for
    # registry-pulled images; the separate field carried no extra
    # info and was always None. Deleted.
    # #238: image_source field had zero readers — _resolve_session_image
    # tracks the source as a LOCAL var to choose between registry pull
    # and local-build path, then discards it. The spec-level field was
    # never written. Deleted.
    # Wraps from plugins (e.g. agent-claude's prompt-injection entrypoint).
    # Each is an argv tuple that prepends to the agent's entrypoint.
    # Composition layers them by (layer, plugin-name) — layer ∈
    # {outer, default, inner} maps to {0, 1, 2}, then plugin name breaks
    # ties. (Pre-2026-05 docstring said alphabetical-only; that was the
    # pre-fix sort order which broke when an outer wrap and an inner
    # wrap were both enabled. Task #242: docstring now matches code.)
    # The final container exec line is:
    #   wrap_0 args ... wrap_n args
    # The outermost wrap (wrap_0) becomes the runtime's --entrypoint /
    # apptainer-exec target; deeper wraps + their args follow. Each
    # wrap is responsible for its own end-of-options sentinel ("--")
    # if needed. (#293,: previous design had separate
    # `entrypoint` + `command` fields the composition layer never
    # populated and the adapters' `if spec.entrypoint:` branch only
    # ever ran in test scaffolding. Deleted; wraps own the exec line.)
    entrypoint_wraps: tuple[tuple[str, ...], ...] = Field(default_factory=tuple)
    # Web-ports plugin contributes these; Docker adapter emits -p flags.
    port_forwards: tuple[PortForward, ...] = Field(default_factory=tuple)
    # env-files (host paths) to pass via `--env-file` to docker/apptainer.
    # Contributed by host_pre_launch hooks (e.g. hpc-modules captures
    # `module load` env into a file the runtime reads). Order matters:
    # later files override earlier on identical keys (docker semantics).
    env_files: tuple[str, ...] = Field(default_factory=tuple)
    # Step B (#216, internal design note DN-005): path-list vars (PATH/LD_LIBRARY_
    # PATH/…) extracted out of a module env_file so they can be PREPENDED at
    # exec time INSIDE the container by a shell trampoline (preserving the
    # container's own /opt/conda/bin etc.) instead of SET via --env-file
    # under --cleanenv (which clobbers). Composition splits them when the
    # opt-in `BOTAINER_USE_INNER_PREPEND=1` is set; the matching env_file
    # entry is rewritten to a scalar-only file (PATH-list vars removed) so
    # `--env-file` only carries SCALARS where SET semantics are correct.
    # Each entry is (var_name, value_to_prepend). Values are validated by
    # `_validate_host_env_text` before reaching here.
    module_env_path_prepends: tuple[tuple[str, str], ...] = Field(
        default_factory=tuple
    )
    mount_plan: MountPlan = Field(default_factory=MountPlan)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    kernel_caps: KernelCapsSpec = Field(default_factory=KernelCapsSpec)
    sidecars: tuple[SidecarSpec, ...] = Field(default_factory=tuple)
    hooks: tuple[HookSpec, ...] = Field(default_factory=tuple)
    env: EnvSpec = Field(default_factory=EnvSpec)
    capabilities: tuple[CapabilityGrant, ...] = Field(default_factory=tuple)
    plugins_enabled: tuple[str, ...] = Field(default_factory=tuple)
    # Trust-degraded plugins (impl review MEDIUM 9). Each entry is
    # (plugin_name, tier, detail). Surfaced in `botainer inspect` and
    # the capability-summary confirmation banner so users can't miss
    # a hash mismatch — previously this only went to stderr.
    plugin_trust_warnings: tuple[tuple[str, str, str], ...] = Field(
        default_factory=tuple
    )
    profile: str = "default"
    # #53 / T0-2: the resolved in-cage permission posture (project config ∩ site
    # policy ceiling). "bypass" = the agent runs with NO in-agent permission
    # prompts (the container is the boundary; required for TTY-less batch);
    # "prompt" = the agent's stock interactive prompts. Composition also injects
    # this as BOTAINER_AGENT_PERMISSIONS into spec.env so each agent's entrypoint
    # wrapper maps it to its own flags. Surfaced by the capability summary.
    # See DN-010.
    agent_permissions: str = "bypass"
    # ISO-8601 UTC timestamp. Read by state/dir.py for last_session_at.
    composed_at: str = ""
    # #238: config_hash and policy_hash fields were
    # computed at compose time but never read. The aspirational use
    # case (TOFU drift detection) landed instead as a per-project
    # baseline file (see design §A14); these spec-level fields were
    # left orphaned. Deleted. If a future feature wants spec-level
    # provenance hashes, re-add them WITH the consumer.

    @field_validator("image")
    @classmethod
    def _validate_image(cls, v: str) -> str:
        # AC7 hole-hunt: no SessionSpec may carry an image
        # that would be parsed as a docker/apptainer flag. This is the
        # type-level backstop — `SessionSpec(image="--privileged")` was
        # ACCEPTED before this and rendered into `docker run` un-`--`-
        # guarded, yielding a privileged container.
        return validate_image_reference(v)

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, v: str) -> str:
        # AUDIT (C1 sibling): profile reaches the per-agent
        # apptainer --bind source + an on-host mkdir/chmod; constrain it to a
        # flat token so it cannot traverse. Type-level backstop.
        return validate_profile_name(v)

    @field_validator("project_uuid")
    @classmethod
    def _validate_project_uuid(cls, v: str) -> str:
        # AC7 validator-parity audit: project_uuid flows into
        # filesystem paths AND (on the HPC submit path) the generated sbatch
        # script. A control char (newline/CR/NUL/tab) would inject sbatch
        # directives / shell lines (a tampered git-shareable .botainer/
        # project-id is the attacker vector — confirmed HIGH). Reject the
        # injection charset here as the type-level backstop. We do NOT
        # require a canonical UUID at this layer (placeholder ids are used
        # widely in tests + some compose paths); the canonical-UUID contract
        # is enforced at the real sources (identity._validate_uuid in the
        # plugin dispatcher / init / resolve) and in SubmissionPlan.
        return validate_project_uuid(v)
