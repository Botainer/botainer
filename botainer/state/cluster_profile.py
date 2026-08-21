"""HPC cluster profile loader.

Per DN-031 Phase 0: the operator publishes a
profile YAML for their cluster. Users `botainer hpc setup` to write it
to `~/.botainer/cluster.yaml`. The launcher consults it for:

- Lmod bootstrap path
- Default Slurm partition / account / time
- Scratch FS path (for MY_BOTAINER redirect suggestion)
- Per-partition resource caps
- Apptainer cache + prebuilt-image URL
- Module denylist / always-load list
- Agent-hints preamble (per-cluster operator note to the agent)

Bundled cluster profiles live in `<package>/cluster_profiles/<name>.yaml`.
Users can override with their own `~/.botainer/cluster.yaml`.

When no profile is found, the launcher falls back to runtime detection
(hostname pattern matching against bundled profiles) and finally to
generic defaults.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from botainer.state import dir as state_dir


@dataclass(frozen=True)
class PartitionSpec:
    name: str
    max_time_minutes: int | None = None
    max_cpus: int | None = None
    max_memory_gb: int | None = None
    gpu_types: tuple[str, ...] = field(default_factory=tuple)

    # ── properties that change what a job COSTS or whether it SURVIVES ──
    # Added while transcribing ~40 real sites. Both were previously
    # unrepresentable, so botainer described a preemptible or whole-node
    # partition in exactly the same terms as an ordinary one.
    #
    # These are not cosmetic labels. They are the two facts that make an
    # otherwise sensible partition choice expensive or destructive, and in both
    # cases the trap is baited: the affected partitions are the CHEAPEST and
    # FASTEST TO START, so they are precisely what an optimising agent (or a
    # user reading a queue table) will pick.

    #: The job can be killed and requeued at any moment, without warning.
    #: Yale `scavenge`, Harvard `serial_requeue`/`gpu_requeue`, Stanford
    #: `owners`, Berkeley `savio_lowprio`, NERSC `preempt`/`overrun`.
    #: An agent that does not know this will not checkpoint and will silently
    #: lose hours. Several bundled profiles DEFAULT to such a partition.
    preemptible: bool = False

    #: Allocates — and charges for — a whole node regardless of what you
    #: request. SDSC Expanse `compute` bills all 128 cores for a 1-core job;
    #: same for `gpu` vs `gpu-shared`, and Bridges-2 `RM` vs `RM-shared`.
    #: This is the most expensive silent mistake available on those machines.
    exclusive: bool = False

    #: Multiplier applied to allocation charging. NERSC `premium` is 2-4x;
    #: `preempt` is discounted below 1.0. None ⇒ not documented (NOT 1.0 —
    #: "unknown" and "charged normally" are different claims).
    charge_factor: float | None = None


@dataclass(frozen=True)
class ClusterProfile:
    name: str
    hostname_patterns: tuple[str, ...] = field(default_factory=tuple)
    #: Other names this cluster answers to. TWO DIFFERENT JOBS were conflated
    #: in `name` until, and separating them is the fix:
    #:
    #:   `name`    the CATALOGUE identifier — must be globally unique, because
    #:             cluster names collide across institutions (Grace is both
    #:             Yale's and Texas A&M's). Hence `us-yale-grace`.
    #:   `aliases` what the SITE calls itself — what `$SLURM_CLUSTER_NAME` and
    #:             `$LMOD_SYSHOST` actually contain on its compute nodes, and
    #:             what a user naturally types. That is `grace`, and no site is
    #:             going to start reporting our prefixed identifier.
    #:
    #: Renaming profiles to unique identifiers silently broke the compute-node
    #: autodetect fallback and `hpc setup --profile grace`, because both matched
    #: on `name`. Caught by a subagent reviewing the rename, not by me.
    aliases: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""
    lmod_bootstrap: str = ""
    slurm_default_partition: str = ""
    slurm_default_account: str = ""
    slurm_default_time_minutes: int = 240
    #: Batch system this site actually runs. "slurm" unless a profile says
    #: otherwise. Every hpc command emits sbatch/#SBATCH and parses squeue, so
    #: anything else is UNSUPPORTED — and the point of recording it is to
    #: REFUSE clearly instead of failing with command-not-found.
    #: "none" = no shared batch scheduler at all (e.g. a cloud VM resource).
    scheduler: str = "slurm"
    partitions: tuple[PartitionSpec, ...] = field(default_factory=tuple)
    scratch_template: str = ""
    #: Same mechanism as scratch_template, for the components that consume
    #: INODES rather than bytes. Added: a 500,000-file cap on a
    #: cluster $HOME was exhausted by `packages/` (conda/pip, ~8.6 GB but
    #: hundreds of thousands of tiny files) while `scratch` — the only
    #: movable component — was already elsewhere and did not help.
    packages_template: str = ""
    home_template: str = ""
    # Task #270 status note: scratch_cleanup_days and apptainer_prebuilt_url
    # are DISPLAY-ONLY at v0.1 — `botainer hpc info` shows them but no
    # production code triggers cleanup or downloads a prebuilt image.
    # apptainer_cachedir IS consumed (botainer/cli/hpc.py:308 sets
    # APPTAINER_CACHEDIR env). Display-only fields are kept for the
    # cluster.yaml shape contract; v0.2 wiring will make them load-
    # bearing or remove them.
    scratch_cleanup_days: int = 0  # display-only at v0.1
    apptainer_cachedir: str = ""   # consumed at hpc.py:308
    apptainer_prebuilt_url: str = ""  # display-only at v0.1
    modules_denylist: tuple[str, ...] = field(default_factory=tuple)
    modules_always_load: tuple[str, ...] = field(default_factory=tuple)
    policy_refuse_network: bool = False
    agent_hints_preamble: str = ""
    # ── provenance: how much should a user trust this profile? ──
    # User requirement: "clearly mark what's tested and not tested".
    #
    # Most bundled site profiles already carried a free-text comment of the form
    # "# last verified: <date> against <the site's public docs URL>", in several
    # different phrasings — and the ONE profile actually exercised on real
    # hardware carried none at all. A comment cannot be surfaced, so a user
    # picking a profile learned nothing about whether anyone had ever run it.
    #
    # Structured so the CLI can SHOW it at selection time. Unset ⇒ "unknown",
    # which is the correct default for a profile that says nothing: absence of a
    # claim must never read as a claim of having been tested.
    verification_status: str = ""     # tested-on-hardware|from-public-docs|from-probe|community-contributed
    verification_checked: str = ""    # ISO date, free-form
    verification_source: str = ""     # URL or short provenance note

    #: Recognised values, most-trusted first. Anything else (including "") is
    #: reported as unknown rather than silently accepted as verified.
    VERIFICATION_LEVELS: ClassVar[tuple[str, ...]] = (
        "tested-on-hardware",       # someone ran botainer on this cluster
        "from-probe",               # generated by probing a live scheduler
        "from-public-docs",         # transcribed from the site's documentation
        "community-contributed",    # submitted, provenance unstated
    )

    def verification_label(self) -> str:
        """One line for humans. NEVER implies more confidence than is recorded."""
        st = self.verification_status
        if st not in self.VERIFICATION_LEVELS:
            return ("UNVERIFIED — this profile does not record how it was "
                    "produced or whether anyone has run it")
        when = f", checked {self.verification_checked}" if self.verification_checked else ""
        src = f" ({self.verification_source})" if self.verification_source else ""
        if st == "tested-on-hardware":
            return f"tested on real hardware{when}{src}"
        if st == "from-probe":
            return f"generated by probing this cluster's scheduler{when}{src}"
        if st == "from-public-docs":
            return (f"transcribed from public documentation — NOT run on the "
                    f"cluster{when}{src}")
        return f"community-contributed, provenance unstated{when}{src}"

    def match_names(self) -> set[str]:
        """Every name this profile answers to, lowercased.

        Used by autodetect's env-var fallback and by `hpc setup --profile`.
        Includes the catalogue identifier AND the site's own names, so a
        cluster that reports itself as `grace` still resolves to
        `us-yale-grace` — and a user may type either.
        """
        return {self.name.lower()} | {a.lower() for a in self.aliases}

    def is_verified_on_hardware(self) -> bool:
        return self.verification_status == "tested-on-hardware"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClusterProfile:
        if d.get("version") != "cluster-profile-v1":
            raise ValueError(
                f"unsupported cluster-profile version "
                f"{d.get('version')!r}; expected 'cluster-profile-v1'"
            )
        cluster = d.get("cluster") or {}
        lmod = d.get("lmod") or {}
        slurm = d.get("slurm") or {}
        scratch = d.get("scratch") or {}
        packages = d.get("packages") or {}
        home_blk = d.get("home") or {}
        apptainer = d.get("apptainer") or {}
        modules = d.get("modules") or {}
        policy = d.get("policy") or {}
        hints = d.get("agent_hints") or {}
        verification = d.get("verification") or {}
        partitions = tuple(
            PartitionSpec(
                name=name,
                max_time_minutes=p.get("max_time_minutes"),
                max_cpus=p.get("max_cpus"),
                max_memory_gb=p.get("max_memory_gb"),
                gpu_types=tuple(p.get("gpu_types") or []),
                preemptible=bool(p.get("preemptible", False)),
                exclusive=bool(p.get("exclusive", False)),
                charge_factor=(
                    float(p["charge_factor"])
                    if p.get("charge_factor") is not None else None
                ),
            )
            for name, p in (slurm.get("partitions") or {}).items()
        )
        # A site that is not Slurm must be able to SAY so. Roughly a quarter of
        # the profiles gathered  use PBS Pro, LSF, Grid Engine, or
        # no batch scheduler at all (Jetstream2 is a cloud VM resource). Without
        # this field such a profile looked fully supported and `hpc submit`
        # failed with a bare command-not-found — a profile we ship, that cannot
        # work, saying nothing about why.
        #
        # Declaring the gap is deliberately separated from CLOSING it: real
        # PBS/LSF support is a v0.2 project needing its own security review,
        # because the dispatcher's directional-mailbox reasoning is specific to
        # what slurmstepd does with --output and would have to be re-derived.
        # See DN-016 §4a.
        scheduler = str(slurm.get("scheduler", "slurm")).strip().lower() or "slurm"
        return cls(
            name=cluster.get("name", ""),
            hostname_patterns=tuple(cluster.get("hostname_patterns") or []),
            aliases=tuple(cluster.get("aliases") or []),
            description=cluster.get("description", ""),
            lmod_bootstrap=lmod.get("bootstrap", ""),
            slurm_default_partition=slurm.get("default_partition", ""),
            slurm_default_account=slurm.get("default_account", ""),
            slurm_default_time_minutes=int(
                slurm.get("default_time_minutes", 240)
            ),
            scheduler=scheduler,
            partitions=partitions,
            scratch_template=scratch.get("template", ""),
            packages_template=packages.get("template", ""),
            home_template=home_blk.get("template", ""),
            scratch_cleanup_days=int(scratch.get("auto_cleanup_days", 0)),
            apptainer_cachedir=apptainer.get("cachedir", ""),
            apptainer_prebuilt_url=apptainer.get("prebuilt_url", ""),
            modules_denylist=tuple(modules.get("denylist") or []),
            modules_always_load=tuple(modules.get("always_load") or []),
            policy_refuse_network=bool(policy.get("refuse_network", False)),
            agent_hints_preamble=hints.get("preamble", ""),
            verification_status=str(verification.get("status") or ""),
            verification_checked=str(verification.get("last_checked") or ""),
            verification_source=str(verification.get("source") or ""),
        )

    def matches_hostname(self, hostname: str) -> bool:
        """Does this profile claim the given hostname?

        Supports glob-style patterns (fnmatch); falls back to literal
        substring match if the pattern has no glob chars.
        """
        import fnmatch
        for pat in self.hostname_patterns:
            if fnmatch.fnmatch(hostname, pat):
                return True
            if "*" not in pat and "?" not in pat and pat in hostname:
                return True
        return False


def _bundled_profiles_root() -> Path | None:
    """Where bundled cluster profiles ship in the package."""
    import botainer
    pkg_root = Path(botainer.__file__).resolve().parent
    candidate = pkg_root / "cluster_profiles"
    if candidate.exists():
        return candidate
    # Dev fallback: repo's bundled-profiles dir at the package root level.
    repo_root = pkg_root.parent
    dev = repo_root / "cluster_profiles"
    if dev.exists():
        return dev
    return None


def list_bundled() -> list[ClusterProfile]:
    """Return all bundled cluster profiles."""
    root = _bundled_profiles_root()
    if root is None:
        return []
    out: list[ClusterProfile] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        try:
            out.append(ClusterProfile.from_dict(data))
        except (ValueError, KeyError):
            continue
    return out


def load_user_profile() -> ClusterProfile | None:
    """Load `~/.botainer/cluster.yaml` if present.

    Returns None if no profile is configured (caller falls back to
    autodetect or generic defaults).
    """
    try:
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    except Exception:
        return None
    profile_path = paths.root / "cluster.yaml"
    if not profile_path.exists():
        return None
    # AUDIT (MEDIUM): a PRESENT-but-malformed cluster.yaml must FAIL
    # CLOSED (like SitePolicy does), not silently return None and degrade to
    # autodetect. cluster.yaml is operator HPC config (partitions, account,
    # Lmod bootstrap); a typo silently picking wrong settings yields confusing
    # downstream sbatch failures. Absent → None (autodetect, fine); present but
    # unparseable/invalid → raise so the operator sees the error immediately.
    from botainer.core.refusal import RefusalCategory, Refused
    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise Refused(
            RefusalCategory.CONFIG_INVALID,
            f"cluster profile {profile_path} is present but unreadable/unparseable: "
            f"{exc}. Fix the YAML or remove the file to fall back to autodetect.",
        ) from exc
    if not isinstance(data, dict):
        raise Refused(
            RefusalCategory.CONFIG_INVALID,
            f"cluster profile {profile_path} must be a YAML mapping (got "
            f"{type(data).__name__}). Fix it or remove the file to fall back to "
            f"autodetect.",
        )
    try:
        return ClusterProfile.from_dict(data)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Refused(
            RefusalCategory.CONFIG_INVALID,
            f"cluster profile {profile_path} is present but invalid: {exc}. "
            f"Fix it or remove the file to fall back to autodetect.",
        ) from exc


def write_user_profile(profile: ClusterProfile) -> Path:
    """Persist a profile to `~/.botainer/cluster.yaml` (mode 0600)."""
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    profile_path = paths.root / "cluster.yaml"
    data = {
        "version": "cluster-profile-v1",
        "cluster": {
            "name": profile.name,
            "aliases": list(profile.aliases),
            "hostname_patterns": list(profile.hostname_patterns),
            "description": profile.description,
        },
        "verification": {
            "status": profile.verification_status,
            # KEY NAME IS LOAD-BEARING: from_dict reads "last_checked"
            # (line ~242) and every shipped profile writes that. Writing
            # "checked" here would round-trip to "" — which is how the
            # round-trip test caught this one.
            "last_checked": profile.verification_checked,
            "source": profile.verification_source,
        },
        "lmod": {"bootstrap": profile.lmod_bootstrap},
        "slurm": {
            # LOAD-BEARING, and it used to be dropped here. `scheduler` is what
            # tells botainer a site is PBS/LSF/SGE rather than Slurm. Omitting
            # it from this dict meant `hpc setup` silently rewrote a correct
            # non-Slurm declaration back to the "slurm" default on the round
            # trip — so a Polaris or Summit user got Slurm output with no
            # warning, from a profile whose own header says in capitals that
            # the site is not Slurm.
            "scheduler": profile.scheduler,
            "default_partition": profile.slurm_default_partition,
            "default_account": profile.slurm_default_account,
            "default_time_minutes": profile.slurm_default_time_minutes,
            "partitions": {
                p.name: {
                    "max_time_minutes": p.max_time_minutes,
                    "max_cpus": p.max_cpus,
                    "max_memory_gb": p.max_memory_gb,
                    "gpu_types": list(p.gpu_types),
                    "preemptible": p.preemptible,
                    "exclusive": p.exclusive,
                    "charge_factor": p.charge_factor,
                }
                for p in profile.partitions
            },
        },
        "scratch": {
            "template": profile.scratch_template,
            "auto_cleanup_days": profile.scratch_cleanup_days,
        },
        "packages": {"template": profile.packages_template},
        "home": {"template": profile.home_template},
        "apptainer": {
            "cachedir": profile.apptainer_cachedir,
            "prebuilt_url": profile.apptainer_prebuilt_url,
        },
        "modules": {
            "denylist": list(profile.modules_denylist),
            "always_load": list(profile.modules_always_load),
        },
        "policy": {"refuse_network": profile.policy_refuse_network},
        "agent_hints": {"preamble": profile.agent_hints_preamble},
    }
    fd = os.open(
        str(profile_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        mode=0o600,
    )
    try:
        os.write(fd, yaml.safe_dump(data, sort_keys=False).encode("utf-8"))
    finally:
        os.close(fd)
    return profile_path


def autodetect() -> ClusterProfile | None:
    """Pick a bundled profile matching the current environment.

    Order (cluster-ease B2):
      1. hostname match (fnmatch/substring on each profile's
         hostname_patterns) — the login-node case.
      2. Slurm/Lmod env-var match: `$SLURM_CLUSTER_NAME` or `$LMOD_SYSHOST`
         equal to a profile's `name` (case-insensitive). This covers COMPUTE
         nodes whose bare nodename doesn't match the login-node hostname
         patterns but where Slurm/Lmod still name the cluster — the case the
         hostname-only autodetect silently missed.

    Returns None if nothing matches (caller can prompt or use defaults).
    """
    bundled = list_bundled()
    host = socket.gethostname()
    for p in bundled:
        if p.matches_hostname(host):
            return p
    # Env-signal fallback. These env vars are set by the scheduler / module
    # system, not user config, so they're a trustworthy cluster identifier.
    for env_key in ("SLURM_CLUSTER_NAME", "LMOD_SYSHOST"):
        val = (os.environ.get(env_key) or "").strip().lower()
        if not val:
            continue
        for p in bundled:
            if val in p.match_names():
                return p
    return None


def active_profile() -> ClusterProfile | None:
    """Effective profile: user override > autodetect > None."""
    user = load_user_profile()
    if user is not None:
        return user
    return autodetect()
