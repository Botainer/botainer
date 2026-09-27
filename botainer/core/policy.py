"""Site policy loading and intersection.

Per codex HIGH 7: write a minimal authoritative schema. This module is that
schema in code form. Top-level fields, defaults, and intersection rules are
documented here; extension goes through new fields, not free-form override.

Intersection model: site ∩ user ∩ project ∩ CLI. Lists intersect; bools
AND; enums must match; scalars take min(). This is the "ceiling" — policy
narrows, never widens.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from botainer.core import agent_permissions
from botainer.core.refusal import RefusalCategory, Refused
from botainer.state import dir as state_dir


class PluginsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_tiers: list[str] = Field(default_factory=lambda: ["first-party"])
    first_party_allowlist: list[str] = Field(default_factory=list)
    third_party_must_be_declarative: bool = True
    sidecars_allowed: list[str] = Field(default_factory=list)
    sched_slurm_allowed: list[str] = Field(default_factory=list)


class MountsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_source_roots: list[str] = Field(default_factory=list)
    extra_targets_allowlist: list[str] = Field(
        default_factory=lambda: ["/data", "/datasets", "/shared", "/scratch", "/mnt"]
    )
    # #160: the host software-root prefixes the hpc-modules software-root binds
    # are allowed to live under (the ceiling for derive_software_root_binds).
    # Default [] → the feature is OFF (the only safe default). This is the
    # AUTHORITATIVE trust anchor for module binds and is taken from the
    # root-owned SitePolicy ONLY (see intersect) — NOT the user-writable
    # cluster.yaml/user policy, since on a multi-tenant cluster that would be a
    # self-grant (internal design note DN-002).
    cluster_software_roots: list[str] = Field(default_factory=list)
    # caps.modules_inner_load: the Lmod install tree that will be
    # bound RO into the container so the agent can run `module load` INSIDE.
    # Default "" → feature OFF. Same SITE-ONLY trust anchor as
    # cluster_software_roots — the admin's choice, not the user's.
    cluster_lmod_root: str = ""
    # caps.modules_inner_load: MODULEPATH directories the agent's in-container
    # `module` command may resolve modulefiles from. Each is bound RO. Default
    # [] → feature OFF (the module command would find nothing to load).
    # SITE-ONLY trust anchor, same as cluster_software_roots.
    cluster_modulepath_roots: list[str] = Field(default_factory=list)


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # AUDIT (H4): default_mode is a CEILING — the most-permissive
    # network mode the SITE allows. It defaults to "internet" (no restriction
    # imposed) because with no admin-deployed /etc/botainer/policy.yaml there
    # is no site to restrict; a multi-tenant HPC admin LOWERS it (to
    # "endpoint-ip-allowlist" or "none") and compose_session enforces that
    # ceiling against the project config (which itself defaults to the
    # most-restrictive "none" per DN-026 §342). A "none" default here was
    # wrong for a ceiling: once enforced it would refuse every out-of-the-box
    # session (the config template requests "internet"). The ordering is
    # none < endpoint-ip-allowlist < internet; a project may request a mode at
    # or below the ceiling, never above it.
    default_mode: str = "internet"
    allowed_endpoint_groups: list[str] = Field(default_factory=list)
    # #175: host_services_allowed_ports policy field removed
    #; paired with the deleted network.host_services
    # capability + cfg.network.host_services config field.


class CapabilitiesPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kernel_caps_keep_allowed: list[str] = Field(default_factory=list)
    env_var_denylist: list[str] = Field(
        # Per sharp-edges F5: expand to include the full set of known-
        # dangerous env vars across language runtimes and tools.
        default_factory=lambda: [
            "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_BIND_NOW",
            "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
            "NODE_PATH", "NODE_OPTIONS",
            "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT",
            "BASH_ENV", "ENV", "PROMPT_COMMAND",
            "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
            "JULIA_LOAD_PATH", "R_HOME",
            "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
            "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        ]
    )


class NamingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Task #173: NOT honored in v0.1.0. ~15 production sites hardcode
    # ".botainer" directly. The field is reserved for v0.2 when a
    # central resolver replaces the hardcoded values. Setting this to
    # anything else right now silently desyncs from launcher behavior.
    host_managed_folder: str = ".botainer"


class JobPolicy(BaseModel):
    """#54: the SITE ceiling on dispatched jobs — the REAL trust boundary.

    `job_profiles` live in the git-shareable (untrusted) project config, so the
    profile is NOT the boundary; this root-owned ceiling is. A profile that
    exceeds it is refused at submit (`check_profile_against_ceiling`). Defaults
    are PERMISSIVE (no botainer cap — Slurm/the user's account is the backstop);
    a multi-tenant admin LOWERS them. Empty allowlist = "no restriction" (allow
    all); `max_* = None` = no cap.
    """

    model_config = ConfigDict(extra="forbid")
    allowed_partitions: list[str] = Field(default_factory=list)
    allowed_accounts: list[str] = Field(default_factory=list)
    max_gpus_per_job: int | None = None
    max_concurrent_cap: int | None = None
    max_nodes_per_job: int | None = None   # #68 MPI: cap --nodes (None = no cap)
    # jobs v2 (audit): per-site ceilings on the remaining resolved
    # dims, so the "agent ≤ profile max ≤ site policy" guarantee holds for ALL of
    # cpus/mem/time/gpus/nodes, not just gpus/nodes. Checked against the RESOLVED
    # resources at submit (after any per-request override). None = no cap.
    max_cpus_per_job: int | None = None
    max_mem_mb_per_job: int | None = None
    max_time_seconds_per_job: int | None = None


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # #53 / T0-2: max_permissions is a CEILING — the MOST-AUTONOMOUS in-cage
    # permission posture the SITE allows. Defaults to "bypass" (no restriction
    # imposed) because with no admin-deployed /etc/botainer/policy.yaml there is
    # no site to restrict, and the product default is "the container is the
    # boundary". A cautious multi-tenant admin may LOWER it; compose_session
    # then refuses a project that asks for more. See DN-010.
    #
    # THE CEILING RANKS TIERS, NOT MODES, and that is deliberate. A project may
    # now name the AGENT'S OWN mode (claude `acceptEdits`, codex `on-request`),
    # and those do not sort onto one line: where does `plan` (executes nothing)
    # sit relative to `acceptEdits`? The question has no answer, and the old
    # two-value rank pretended it did — any value it had not heard of scored 99
    # and was refused, so every new mode would have been dead on arrival.
    #
    # There are exactly two things a ceiling can usefully say, so there are two
    # tiers (`agent_permissions.CAP_VALUES`):
    #
    #   default  the agent's own permission system must stay ON
    #   bypass   botainer may switch it off
    #
    # `prompt` remains accepted as the legacy spelling of `default`, because
    # policy files in the field say it. An admin who wrote `max_permissions:
    # prompt` meaning "no bypass" keeps meaning exactly that — and now also
    # admits the middle modes they never had.
    max_permissions: str = "bypass"

    @field_validator("max_permissions")
    @classmethod
    def _validate_max_permissions(cls, v: str) -> str:
        if v not in agent_permissions.CAP_VALUES:
            raise ValueError(
                f"agent.max_permissions {v!r} must be one of "
                f"{sorted(agent_permissions.CAP_VALUES)} — a ceiling is a TIER, "
                f"not one of the agents' own mode names. 'default' means the "
                f"agent's permission system must stay on; 'bypass' allows "
                f"turning it off. ('prompt' is the older spelling of 'default'.)"
            )
        return v


class SitePolicy(BaseModel):
    """Authoritative site/user policy schema v1.

    Per codex HIGH 7: this *is* the minimal v1 schema. Field-by-field defaults
    here. Intersection semantics:
      - list fields: site ∩ user ∩ project ∩ CLI (set intersection)
      - bool fields: AND (False sticks)
      - enum/string fields: must match across levels; mismatch → refuse
      - integer fields: min() across levels

    `default_auth_mode` is the host-wide default for new projects (used by
    `botainer init`). Empty string = "fall back to whatever code default the
    init flow picks"; the value is set explicitly below so the intention is
    visible in the schema.

    **The default is `isolated`, changed from `shared`.** Shared mode reuses
    one login across projects; isolated mode limits that sharing. The relevant
    credential-handling tradeoffs are:

    - shared mode's directory holds a REAL refresh token the container can read
      AND overwrite, so a compromised agent in ANY shared project can change
      which account every other shared project uses. The init banner already
      says this in full.
    - refresh tokens ROTATE (measured 2026-07-29), so shared mode's N+1 holders
      are unsound the moment two projects are used in sequence — every
      container-side refresh invalidates the other copies. That is the root
      cause of the "new project not logged in" reports.
    - isolated and broker are the two modes with a coherent story: isolated
      keeps one credential per project, broker keeps the refresh token out of
      the container entirely.

    A default is a recommendation the product makes on the user's behalf, and
    recommending the mode with the worst isolation story because it is the most
    convenient is the wrong way round. Users who want the old behaviour set it
    once, host-wide:  `botainer policy set default_auth_mode shared`.

    Set via `botainer policy set default_auth_mode <mode>` or
    `botainer auth use <mode> --global`.
    """

    model_config = ConfigDict(extra="forbid")
    version: str = "policy-v1"

    @field_validator("version", mode="before")
    @classmethod
    def _explain_version(cls, v):
        """Turn the commonest site-admin mistake into an actionable message.

        UX audit (B1): docs/SITE-ADMIN.md's copy-pasteable snippet
        wrote `version: 1`. YAML parses that as an int, pydantic's default
        string_type error is a wall of JSON ending in an errors.pydantic.dev
        URL, and — because this is the SITE policy — every botainer command for
        every user on the host then refuses. Name the fix instead.
        """
        if not isinstance(v, str):
            raise ValueError(
                f"policy `version:` must be the string \"policy-v1\", not "
                f"{v!r}. In YAML, write:  version: policy-v1   "
                f"(a bare 1 is parsed as an integer)."
            )
        return v

    plugins: PluginsPolicy = Field(default_factory=PluginsPolicy)
    mounts: MountsPolicy = Field(default_factory=MountsPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    capabilities: CapabilitiesPolicy = Field(default_factory=CapabilitiesPolicy)
    naming: NamingPolicy = Field(default_factory=NamingPolicy)
    agent: AgentPolicy = Field(default_factory=AgentPolicy)
    jobs: JobPolicy = Field(default_factory=JobPolicy)
    default_auth_mode: str = "isolated"

    @field_validator("default_auth_mode")
    @classmethod
    def _validate_default_auth_mode(cls, v: str) -> str:
        if v and v not in {"isolated", "shared", "proxy", "broker"}:
            raise ValueError(
                f"default_auth_mode {v!r} must be empty, 'isolated', "
                f"'shared', 'proxy', or 'broker'"
            )
        return v


SITE_POLICY_PATHS = [
    Path("/etc/botainer/policy.yaml"),
]


def load_user_policy() -> SitePolicy:
    """Load the user's policy. Returns defaults if missing.

    Per codex MEDIUM 7: on laptop `setup` writes explicit user policy; missing
    after setup → refuse. For now (no `setup` enforcement), we return defaults.
    """
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    return _load_policy_at(paths.policy_path)


def _is_root_owned(path: Path) -> bool:
    """True if `path` is owned by uid 0. Used to trust a BOTAINER_SITE_POLICY
    override only when it is root-owned (AUDIT H4). Separate function
    so tests can monkeypatch the ownership decision without needing real root."""
    try:
        return path.stat().st_uid == 0
    except OSError:
        return False


def load_site_policy() -> SitePolicy:
    """Load the root-owned site policy (the capability ceiling); else defaults.

    AUDIT (H4, HPC parity): on the HPC apptainer compute-node path
    the session runs under `apptainer exec --containall`, so `/etc` is the
    container image's `/etc` and the host's `/etc/botainer/policy.yaml` is
    invisible — the site ceiling would silently fall back to defaults and an
    admin's lowered ceiling would NOT bind. The hpc-launcher host_helper binds
    the host site policy into the container and points `BOTAINER_SITE_POLICY`
    at it, so the ceiling is enforced on HPC the same as on docker. This env
    override is consulted FIRST; it is launcher-set (not user-set) and the file
    it names is bound read-only.
    """
    env_override = os.environ.get("BOTAINER_SITE_POLICY")
    if env_override:
        p = Path(env_override)
        # SECURITY: os.environ is USER-CONTROLLED on the direct/docker path, so
        # the override must be trustworthy by construction or a user could point
        # it at their own permissive file and bypass the admin ceiling. Honor it
        # only if the file is ROOT-OWNED: the HPC host_helper binds the host's
        # root-owned /etc/botainer/policy.yaml read-only and a bind preserves
        # owner uid, so the legitimate compute-node copy passes; a user's own
        # /tmp file (their uid) does not and is ignored (fall through to the
        # real /etc path / defaults).
        if p.exists() and _is_root_owned(p):
            return _load_policy_at(p)
    for p in SITE_POLICY_PATHS:
        if p.exists():
            return _load_policy_at(p)
    return SitePolicy()


def site_policy_present() -> bool:
    """True iff a root-owned SITE policy file actually exists (i.e. the ceiling
    is admin-imposed), vs load_site_policy() falling back to permissive defaults.

    Used to tell the user whether a capability refusal comes from an ADMIN/site
    policy (only an admin can raise it) or from their OWN user policy (they can
    raise it themselves) — the difference the generic refusal used to muddy.
    Mirrors load_site_policy's discovery: the root-owned BOTAINER_SITE_POLICY
    override first, then the SITE_POLICY_PATHS."""
    env_override = os.environ.get("BOTAINER_SITE_POLICY")
    if env_override:
        p = Path(env_override)
        if p.exists() and _is_root_owned(p):
            return True
    return any(p.exists() for p in SITE_POLICY_PATHS)


def stale_restrictive_user_ceiling() -> str | None:
    """Detect the upgrade footgun: the USER policy's network ceiling is more
    restrictive than the frictionless default (`internet`, what `botainer init`
    writes) AND no site policy forces it — so the very next `botainer start`
    hard-refuses. This usually means an old `policy.yaml` (older builds defaulted
    the ceiling more restrictively) was carried forward by setup's
    add-missing-fields-only merge, which never updates values inside an existing
    block. Returns an actionable one-line warning, or None if fine.

    Non-destructive by design: we do NOT auto-raise (someone may have set a
    restrictive ceiling deliberately — silently raising it on upgrade would be a
    security regression). We surface it so the user decides."""
    _RANK = {"none": 0, "endpoint-ip-allowlist": 1, "internet": 2}
    user_mode = load_user_policy().network.default_mode
    if _RANK.get(user_mode, 2) >= _RANK["internet"]:
        return None  # user ceiling already permissive enough; nothing to warn
    if site_policy_present():
        site_mode = load_site_policy().network.default_mode
        if _RANK.get(site_mode, 2) < _RANK["internet"]:
            return None  # the (admin) SITE policy is the real constraint, not a stale user default
    return (
        f"your user policy pins network.default_mode={user_mode!r}, more "
        f"restrictive than the 'internet' config `botainer init` writes — the "
        f"next `botainer start` will REFUSE. If unintended (commonly carried over "
        f"from an older botainer version), run: "
        f"botainer policy set network.default_mode internet"
    )


def _load_policy_at(path: Path) -> SitePolicy:
    if not path.exists():
        return SitePolicy()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise Refused(RefusalCategory.POLICY_INVALID, f"yaml parse error in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise Refused(RefusalCategory.POLICY_INVALID, f"{path}: top-level must be a mapping")
    try:
        return SitePolicy.model_validate(raw)
    except Exception as exc:
        raise Refused(RefusalCategory.POLICY_INVALID, f"{path}: {exc}") from exc


def _isect_allow_empty_all(lists: list[list[str]]) -> list[str]:
    """Intersect allowlists where an EMPTY list means 'no restriction'.

    Result preserves the order of the first restricting list. If every list is
    empty → empty (no restriction). If exactly one restricts → that one. If
    several restrict → their set-intersection.
    """
    restricting = [lst for lst in lists if lst]
    if not restricting:
        return []
    acc = list(restricting[0])
    for lst in restricting[1:]:
        acc = [x for x in acc if x in lst]
    return acc


def _min_optional(vals: list[int | None]) -> int | None:
    """min() treating None as 'no cap'. All-None → None; else the min of the set caps."""
    present = [v for v in vals if v is not None]
    return min(present) if present else None


def check_profile_against_ceiling(
    profile_name: str,
    partition: str,
    account: str | None,
    gpus: int,
    max_concurrent: int,
    jobs_policy: JobPolicy,
    nodes: int = 1,
    *,
    cpus: int | None = None,
    mem_mb: int | None = None,
    time_seconds: int | None = None,
) -> None:
    """Refuse a dispatched job profile that exceeds the root-owned site ceiling.

    The profile is UNTRUSTED (git-shareable config); this is the boundary. Called
    by the dispatcher before composing/submitting. Fail-closed on each capped
    dimension; empty allowlist / None cap = no restriction on that dimension.

    ``cpus``/``mem_mb``/``time_seconds`` are the RESOLVED (post-override) values;
    pass them so the site policy bounds those dims too. A None arg (caller didn't
    resolve it) or None cap skips that check.
    """
    ap = jobs_policy.allowed_partitions
    if ap and partition not in ap:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} partition {partition!r} is not in the "
            f"site policy jobs.allowed_partitions {ap}.",
        )
    aa = jobs_policy.allowed_accounts
    if account and aa and account not in aa:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} account {account!r} is not in the "
            f"site policy jobs.allowed_accounts {aa}.",
        )
    mg = jobs_policy.max_gpus_per_job
    if mg is not None and gpus > mg:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} requests {gpus} GPUs > site policy "
            f"jobs.max_gpus_per_job {mg}.",
        )
    mc = jobs_policy.max_concurrent_cap
    if mc is not None and max_concurrent > mc:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} max_concurrent {max_concurrent} > site "
            f"policy jobs.max_concurrent_cap {mc}.",
        )
    mn = jobs_policy.max_nodes_per_job
    if mn is not None and nodes > mn:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} requests {nodes} nodes > site policy "
            f"jobs.max_nodes_per_job {mn}.",
        )
    mcpu = jobs_policy.max_cpus_per_job
    if mcpu is not None and cpus is not None and cpus > mcpu:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} requests {cpus} cpus > site policy "
            f"jobs.max_cpus_per_job {mcpu}.",
        )
    mm = jobs_policy.max_mem_mb_per_job
    if mm is not None and mem_mb is not None and mem_mb > mm:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} requests {mem_mb} MB memory > site "
            f"policy jobs.max_mem_mb_per_job {mm}.",
        )
    mt = jobs_policy.max_time_seconds_per_job
    if mt is not None and time_seconds is not None and time_seconds > mt:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"job profile {profile_name!r} requests {time_seconds}s walltime > "
            f"site policy jobs.max_time_seconds_per_job {mt}.",
        )


def intersect(*policies: SitePolicy) -> SitePolicy:
    """Site ∩ user ∩ project ∩ CLI.

    Test-grade intersection: list fields use set intersection (preserves order
    from the FIRST policy that contained the value); bools AND; ints min();
    enum/strings must match (any mismatch → return the most restrictive value,
    which is the first policy's value if all agree, else raises).
    """
    if not policies:
        return SitePolicy()
    if len(policies) == 1:
        return policies[0]

    def isect_list(getter):
        seen = list(getter(policies[0]))
        for p in policies[1:]:
            seen = [x for x in seen if x in getter(p)]
        return seen

    def most_restrictive_bool(getter):
        # OR: for a bool where TRUE is the more-restrictive value (a REQUIREMENT
        # any level can impose), True sticks. AUDIT: using AND for
        # third_party_must_be_declarative let a user policy set it False and
        # disable the SITE's hooked-plugin consent requirement — a ceiling
        # bypass. A requirement the site imposes cannot be relaxed downstream.
        return any(getter(p) for p in policies)

    # Sharp-edges #7: an earlier `must_match_str` intersect-by-equality
    # helper violated the "ceiling" model (site narrows, project widens
    # → intersection picks the most restrictive). For network.default_mode
    # we use an explicit ordering: none < endpoint-ip-allowlist < internet.
    _NETWORK_RESTRICTIVENESS = {
        "none": 0,
        "endpoint-ip-allowlist": 1,
        "internet": 2,
    }

    def most_restrictive_network(getter):
        vals = [getter(p) for p in policies]
        try:
            return min(vals, key=_NETWORK_RESTRICTIVENESS.__getitem__)
        except KeyError as exc:
            raise Refused(
                RefusalCategory.POLICY_INVALID,
                f"unknown network.default_mode in policy: {exc.args[0]!r}",
            ) from exc

    # #53 / T0-2: agent.max_permissions is a ceiling. Intersection takes the
    # most-restrictive (lowest tier) so a site policy capping at "default"
    # cannot be relaxed by a downstream user policy setting "bypass". Same shape
    # as most_restrictive_network.
    #
    # Two spellings share tier 0 ("prompt" is the legacy name for "default"),
    # which `min` handles: they are equally restrictive, so whichever wins is
    # the same ceiling either way.
    _PERMISSIONS_RESTRICTIVENESS = dict(agent_permissions.CAP_VALUES)

    def most_restrictive_permissions(getter):
        vals = [getter(p) for p in policies]
        try:
            return min(vals, key=_PERMISSIONS_RESTRICTIVENESS.__getitem__)
        except KeyError as exc:
            raise Refused(
                RefusalCategory.POLICY_INVALID,
                f"unknown agent.max_permissions in policy: {exc.args[0]!r}",
            ) from exc

    out = SitePolicy(
        version=policies[0].version,
        plugins=PluginsPolicy(
            allowed_tiers=isect_list(lambda p: p.plugins.allowed_tiers),
            first_party_allowlist=isect_list(lambda p: p.plugins.first_party_allowlist),
            third_party_must_be_declarative=most_restrictive_bool(
                lambda p: p.plugins.third_party_must_be_declarative
            ),
            sidecars_allowed=isect_list(lambda p: p.plugins.sidecars_allowed),
            sched_slurm_allowed=isect_list(lambda p: p.plugins.sched_slurm_allowed),
        ),
        mounts=MountsPolicy(
            trusted_source_roots=isect_list(lambda p: p.mounts.trusted_source_roots),
            extra_targets_allowlist=isect_list(lambda p: p.mounts.extra_targets_allowlist),
            # #160: SitePolicy-AUTHORITATIVE, NOT intersected. Every caller
            # passes intersect(site, user) site-first (verified), so
            # policies[0] is the root-owned /etc/botainer/policy.yaml. We take
            # its value verbatim rather than isect_list because:
            #   (a) intersecting with the user policy's default-[] would ZERO
            #       a site-set ceiling (isect_list is a true set-intersection),
            #       silently turning the feature OFF whenever the admin — not
            #       the user — configured it (which is the normal case); and
            #   (b) the user-writable cluster.yaml/user policy must never be
            #       able to influence this list at all (widening = self-grant
            #       of a host bind on a multi-tenant cluster). Taking the site
            #       value verbatim denies both widening and (harmlessly)
            #       narrowing by the user. See internal design note DN-002.
            cluster_software_roots=list(policies[0].mounts.cluster_software_roots),
            # caps.modules_inner_load: same SITE-ONLY discipline as
            # cluster_software_roots — admin sets in root-owned policy, user
            # cannot self-grant a bind.
            cluster_lmod_root=policies[0].mounts.cluster_lmod_root,
            cluster_modulepath_roots=list(policies[0].mounts.cluster_modulepath_roots),
        ),
        network=NetworkPolicy(
            default_mode=most_restrictive_network(lambda p: p.network.default_mode),
            allowed_endpoint_groups=isect_list(lambda p: p.network.allowed_endpoint_groups),
        ),
        agent=AgentPolicy(
            max_permissions=most_restrictive_permissions(
                lambda p: p.agent.max_permissions
            ),
        ),
        jobs=JobPolicy(
            # allowlists: empty = "no restriction". Most-restrictive = if either
            # side restricts (non-empty), intersect; if one is empty (=all), take
            # the other. So a site restriction can't be widened by an empty user
            # policy, and vice versa.
            allowed_partitions=_isect_allow_empty_all(
                [p.jobs.allowed_partitions for p in policies]
            ),
            allowed_accounts=_isect_allow_empty_all(
                [p.jobs.allowed_accounts for p in policies]
            ),
            max_gpus_per_job=_min_optional(
                [p.jobs.max_gpus_per_job for p in policies]
            ),
            max_concurrent_cap=_min_optional(
                [p.jobs.max_concurrent_cap for p in policies]
            ),
            # sharp-edges F1 (HIGH): these four were OMITTED here, so
            # intersect() defaulted them to None (= no cap) and the site ceiling
            # for cpus/mem/time/nodes was SILENTLY NOT ENFORCED — a malicious
            # git-shared profile could exceed them. The dispatcher always runs
            # through intersect(), so the "agent ≤ profile max ≤ site policy"
            # guarantee was severed at the site link for these dims.
            max_nodes_per_job=_min_optional(
                [p.jobs.max_nodes_per_job for p in policies]
            ),
            max_cpus_per_job=_min_optional(
                [p.jobs.max_cpus_per_job for p in policies]
            ),
            max_mem_mb_per_job=_min_optional(
                [p.jobs.max_mem_mb_per_job for p in policies]
            ),
            max_time_seconds_per_job=_min_optional(
                [p.jobs.max_time_seconds_per_job for p in policies]
            ),
        ),
        capabilities=CapabilitiesPolicy(
            kernel_caps_keep_allowed=isect_list(lambda p: p.capabilities.kernel_caps_keep_allowed),
            # denylist UNION (more restrictive when more is denied)
            env_var_denylist=sorted({v for p in policies for v in p.capabilities.env_var_denylist}),
        ),
        naming=policies[0].naming,
        # default_auth_mode is a USER PREFERENCE (UX default for new
        # projects), not a security ceiling. Last-non-empty wins so a
        # project-level override would win over site default. Today we
        # only intersect site+user, so this collapses to user's value
        # when set, falling back to site.
        default_auth_mode=next(
            (p.default_auth_mode for p in reversed(policies) if p.default_auth_mode),
            "",
        ),
    )
    return out


def render_human(policy: SitePolicy) -> str:
    lines = ["botainer site policy (effective):"]
    lines.append(f"  version:                       {policy.version}")
    lines.append(f"  plugins.allowed_tiers:         {policy.plugins.allowed_tiers}")
    lines.append(f"  plugins.first_party_allowlist: {policy.plugins.first_party_allowlist}")
    lines.append(
        f"  plugins.third_party_declarative_only: {policy.plugins.third_party_must_be_declarative}"
    )
    lines.append(f"  plugins.sidecars_allowed:      {policy.plugins.sidecars_allowed}")
    lines.append(f"  plugins.sched_slurm_allowed:   {policy.plugins.sched_slurm_allowed}")
    lines.append(f"  mounts.extra_targets:          {policy.mounts.extra_targets_allowlist}")
    lines.append(f"  mounts.cluster_software_roots: {policy.mounts.cluster_software_roots}  [SITE-ONLY]")
    lines.append(f"  mounts.cluster_lmod_root:      {policy.mounts.cluster_lmod_root!r}  [SITE-ONLY]")
    lines.append(f"  mounts.cluster_modulepath_roots: {policy.mounts.cluster_modulepath_roots}  [SITE-ONLY]")
    lines.append(f"  network.default_mode:          {policy.network.default_mode}")
    lines.append(f"  network.allowed_endpoint_grps: {policy.network.allowed_endpoint_groups}")
    lines.append(f"  agent.max_permissions:         {policy.agent.max_permissions}")
    lines.append(f"  jobs.allowed_partitions:       {policy.jobs.allowed_partitions or '(any)'}")
    lines.append(f"  jobs.allowed_accounts:         {policy.jobs.allowed_accounts or '(any)'}")
    lines.append(f"  jobs.max_gpus_per_job:         {policy.jobs.max_gpus_per_job if policy.jobs.max_gpus_per_job is not None else '(no cap)'}")
    lines.append(f"  capabilities.env_var_denylist: {policy.capabilities.env_var_denylist}")
    lines.append("")
    lines.append(
        "  [SITE-ONLY] = read from the root-owned /etc/botainer/policy.yaml only "
        "(your user policy is ignored for it; ask the cluster admin to change it)."
    )
    return "\n".join(lines)
