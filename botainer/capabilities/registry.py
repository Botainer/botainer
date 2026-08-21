"""The 13 capabilities (closed namespace at v0.1.0).

Per DN-026. Each entry: a name, a human description, the
enforcement mechanism the adapter uses, the failure mode if absent, and the
self-test that verifies it at preflight.

Adding a new capability: append to `CAPABILITIES`. Removing one is a breaking
change.
"""

from __future__ import annotations

from dataclasses import dataclass

from botainer.core.refusal import RefusalCategory


@dataclass(frozen=True)
class CapabilityDef:
    name: str
    description: str
    value_schema: dict[str, object]  # JSON Schema fragment describing valid values
    enforcement: str  # short text: how the adapter enforces this
    failure_mode: str  # text: what happens if enforcement isn't possible
    verification: str  # name of the self-test that verifies post-launch
    refusal_category: RefusalCategory = RefusalCategory.CAPABILITY_DENIED_BY_POLICY
    site_policy_field: str = ""  # dotted path into policy.yaml that gates this
    requires_hooked_plugin: bool = False
    requires_host_helper: bool = False


def _bool_schema() -> dict[str, object]:
    return {"type": "boolean"}


def _int_schema(min_: int = 0) -> dict[str, object]:
    return {"type": "integer", "minimum": min_}


def _list_str() -> dict[str, object]:
    return {"type": "array", "items": {"type": "string"}}


CAPABILITIES: list[CapabilityDef] = [
    CapabilityDef(
        name="mounts.workspace",
        description="The project root is bind-mounted at /workspace (always rw at v0.1.0).",
        value_schema={"type": "object"},
        enforcement="docker --mount type=bind,src=...,target=/workspace",
        failure_mode="refuse (mount setup failure)",
        verification="SELFTEST_WORKSPACE_BIND",
        site_policy_field="",  # always granted
    ),
    CapabilityDef(
        name="mounts.extra",
        description="User-requested extra bind mounts; target must be in policy allowlist.",
        value_schema={"type": "array", "items": {"type": "object"}},
        enforcement="docker --mount per bind, with allowlist check at compose time",
        failure_mode="refuse with mount-target-off-allowlist",
        verification="SELFTEST_EXTRA_BIND",
        site_policy_field="mounts.extra_targets_allowlist",
    ),
    # #175: mounts.tmp capability removed. cfg.mounts.tmp
    # exists for backward-compat (every project created since v0.1
    # init has it in config.yaml) but no adapter ever read it.
    # UPDATE: /tmp + /var/tmp are now UNCONDITIONALLY a RAM
    # tmpfs on docker (adapters/docker.py) so runtime temp stays off the
    # (fills-easily) Docker VM disk; apptainer already gets a private /tmp
    # from --containall. This is NOT gated on cfg.mounts.tmp — existing
    # frozen configs all have `tmp: false`, so gating would deny the fix to
    # everyone. cfg.mounts.tmp remains UNWIRED (a no-op vestige); a
    # disk-backed-/tmp opt-out could repurpose it in v0.2.
    CapabilityDef(
        name="network",
        description="Network policy: none, internet, or endpoint IP allowlist (best-effort).",
        value_schema={
            "type": "object",
            "properties": {
                "mode": {"enum": ["none", "internet", "endpoint-ip-allowlist", "api-only"]},
                "endpoints": _list_str(),
            },
            "additionalProperties": False,
        },
        enforcement=(
            "docker --network none / default / namespace + iptables. "
            "endpoint-ip-allowlist is best-effort (codex HIGH 5)."
        ),
        failure_mode="refuse with network-setup-failed if enforcement can't be set up",
        verification="SELFTEST_NETWORK_MODE",
        site_policy_field="network.default_mode",
    ),
    # #175: network.host_services capability removed.
    # Was supposed to gate "reach a named host service" but no adapter
    # ever consumed it; UNIX-socket binds (BindMode.UNIX_SOCKET) are
    # the v0.1 mechanism for the use case it described.
    # #175: the following CapabilityDefs were removed
    # because they had no declarations by any bundled plugin AND no
    # consumers in code. The features they DESCRIBED are real, but
    # the cap-registry surface wasn't where they got gated:
    #   - resources: cpu/memory enforced directly by docker adapter
    #     reading spec.resources.cpu/memory_mb; no cap-grant check.
    #     HPC scheduling lives on cfg.resources, not via the registry.
    #   - caps.kernel: refused (non-empty) directly in adapter
    #     .validate(); no cap-grant check.
    #   - sidecars: contributes.sidecars refused fail-closed in
    #     composition (#95/#291); no cap-grant check.
    #   - lifecycle.hooks: every plugin can declare hooks; the
    #     site policy gates by plugin-tier, not by this cap name.
    # The closed namespace stays closed (the validator still refuses
    # any unknown cap name from plugins); we just stopped pretending
    # the registry had gates it didn't.
    CapabilityDef(
        name="env.values",
        description="Environment variables passed into the container.",
        value_schema={"type": "object", "additionalProperties": {"type": "string"}},
        enforcement="docker -e KEY=VALUE; denylisted vars refused at compose time",
        failure_mode="refuse with env-var-denied for any key on the policy denylist",
        verification="SELFTEST_ENV_VARS",
        site_policy_field="capabilities.env_var_denylist",
        refusal_category=RefusalCategory.ENV_VAR_DENIED,
    ),
    CapabilityDef(
        name="caps.modules_env_override",
        description="hpc-modules: env vars set by module load may override the global denylist (PATH, LD_LIBRARY_PATH, etc.).",
        value_schema={"type": "object"},
        # AUDIT / (T2/T5): composition's
        # _validate_host_env_text refuses, for EVERY host_pre_launch env_file
        # (the hook's filtering is not trusted): execution-injection vars
        # (LD_PRELOAD, BASH_ENV, GIT_SSH_COMMAND, …), botainer-managed package
        # routes (PIP_TARGET/PYTHONPATH/NODE_PATH/… — _BOTAINER_MANAGED_ROUTES),
        # and credentials; and the env_file path is contained under the session
        # state dir. Only PATH/LD_LIBRARY_PATH + the curated module location
        # vars still flow (the bounded residual). NOT yet implemented: a finer
        # PER-PLUGIN grant (only a cap-holding plugin may override) — that is
        # future work, NOT shipped with #160 (which shipped the binds +
        # launcher-side denylist enforcement, not a per-plugin env grant).
        enforcement="composition._validate_host_env_text refuses execution-injection + botainer-managed routes + credentials in any host_pre_launch env_file (path-contained); only PATH/LD_LIBRARY_PATH + curated location vars flow; per-plugin grant is future work (not in #160)",
        failure_mode="refuse execution-injection / credential env entries from a module env_file",
        verification="(none — composition-time)",
        site_policy_field="",
        requires_hooked_plugin=True,
    ),
    CapabilityDef(
        name="caps.modules_software_roots",
        description=(
            "hpc-modules: bind the host software-root dirs that `module load` "
            "added to PATH/LD_LIBRARY_PATH/etc. into the container (read-only) "
            "so module-loaded software is reachable, bounded by the root-owned "
            "SitePolicy mounts.cluster_software_roots ceiling. (#160)"
        ),
        value_schema={"type": "object"},
        enforcement=(
            "BOTH flows call the same derive_software_root_binds(): composition "
            "run_host_pre_launch_hooks (docker/direct-apptainer) AND the "
            "hpc-launcher host_helper sbatch OUTER argv. Binds only dirs `module "
            "load` ADDED (baseline-diff), each within the "
            "mounts.cluster_software_roots SitePolicy ceiling; ro identity binds; "
            "min-depth floor + coarse-root + sensitive-home denylist (never an "
            "umbrella); validate_mount_plan backstops the composition flow; "
            "manifest must declare the cap (both flows); empty ceiling → OFF"
        ),
        failure_mode=(
            "empty ceiling → feature OFF (no binds); a candidate root outside "
            "the ceiling is dropped; >max_roots RAISES (no silent truncation)"
        ),
        verification="SELFTEST_MODULE_SOFTWARE_BIND",
        site_policy_field="mounts.cluster_software_roots",
        requires_hooked_plugin=True,
    ),
    CapabilityDef(
        name="caps.modules_inner_load",
        description=(
            "hpc-modules: bind the Lmod install tree + MODULEPATH dirs into the "
            "container (RO) and inject LMOD_PKG/LMOD_DIR/LMOD_CMD/MODULEPATH env "
            "so the agent can run `module load foo` INSIDE the container (dynamic; "
            "batch-job friendly). Bounded by root-owned SitePolicy "
            "mounts.cluster_lmod_root + mounts.cluster_modulepath_roots. "
            "Complements caps.modules_software_roots (which binds the /apps trees "
            "the modulefiles point at)."
        ),
        value_schema={"type": "object"},
        enforcement=(
            "Flow 1 (composition._compute_inner_load_contribution) + Flow 2 "
            "(hpc-launcher _inner_load_contribution_for_plan) + the inner "
            "disclosure both add RO binds for mounts.cluster_lmod_root + each "
            "mounts.cluster_modulepath_roots entry, plus env LMOD_PKG/LMOD_DIR/"
            "LMOD_CMD/MODULEPATH/BASH_ENV. Empty ceiling (no cluster_lmod_root) "
            "→ feature OFF (no binds, no env). ALL THREE paths run the SAME "
            "module_binds.is_unsafe_module_tree_source guard (no mirror drift): "
            "refuses /etc-class subtrees + sensitive-home + shallow-system "
            "mount points, permits a deep admin-named tree like "
            "/usr/share/lmod/lmod. Flow 1 additionally runs validate_mount_plan "
            "as an independent backstop. BASH_ENV = <cluster_lmod_root>/init/bash "
            "so bash -c subshells (claude's Bash tool, sbatch job scripts) "
            "inherit the module function."
        ),
        failure_mode=(
            "empty ceiling → feature OFF; a candidate root under /etc-class or "
            "sensitive-home or a shallow system mount is refused fail-closed on "
            "every path; a non-existent Lmod tree makes `module` an unknown "
            "command in-container (loud, not silent)"
        ),
        verification="SELFTEST_MODULE_INNER_LOAD",
        site_policy_field="mounts.cluster_lmod_root",
        requires_hooked_plugin=True,
    ),
    CapabilityDef(
        name="host.credential_access",
        description="Plugin's host hooks read credentials from the host state dir (e.g. agent-claude OAuth).",
        value_schema={"type": "object"},
        enforcement="hook runs as the user; access is OS-level. Capability marks the grant.",
        failure_mode="refuse if site policy disallows host credential plugins",
        verification="(none — host-side)",
        site_policy_field="plugins.allowed_tiers",
        requires_hooked_plugin=True,
    ),
    CapabilityDef(
        name="host.subprocess",
        description="Plugin's host hooks spawn host subprocesses (e.g. wolfram-sidecar runs wolframscript).",
        value_schema={"type": "object"},
        enforcement="hook runs as the user; subprocess starts in user context.",
        failure_mode="refuse if site policy disallows host-subprocess plugins",
        verification="(none — host-side)",
        site_policy_field="plugins.allowed_tiers",
        requires_host_helper=True,
    ),
    CapabilityDef(
        name="net.outbound_proxy",
        description="Plugin acts as an outbound network proxy for the agent (intercepts container egress).",
        value_schema={"type": "object"},
        enforcement="agent traffic routed through plugin-owned host socket.",
        failure_mode="refuse if site policy disallows outbound proxy plugins",
        verification="(none — runtime route)",
        site_policy_field="plugins.allowed_tiers",
        requires_hooked_plugin=True,
    ),
    CapabilityDef(
        name="net.port_forward",
        description="Plugin contributes container→host port forwards (e.g. web-ports).",
        value_schema={"type": "array"},
        enforcement=(
            "docker -p flags from PortForward entries; refused on apptainer "
            "(no Docker namespace). Per-port host_bind validation lives in "
            "PortForward._validate_host_bind (loopback-only allowlist)."
        ),
        failure_mode="refuse port forwards on apptainer; refuse non-loopback host_bind",
        verification="SELFTEST_PORT_FORWARD_LOOPBACK",
        # #175: site_policy_field was
        # "network.host_services_allowed_ports", which was deleted along
        # with the host_services capability. v0.1 has no host-policy gate
        # for port_forward ports — the bind-address allowlist in
        # PortForward (loopback only) is the security control.
        site_policy_field="",
        requires_hooked_plugin=True,
    ),
    CapabilityDef(
        name="sched.slurm",
        description=(
            "Permission to invoke sbatch/srun (HPC-only). Granted to first-party "
            "hpc-launcher and hpc-pool plugins; third-party plugins refused by default."
        ),
        value_schema={
            "type": "object",
            "properties": {
                "partition": {"type": "string"},
                "account": {"type": "string"},
                "time_minutes": _int_schema(min_=1),
                "gpus": _int_schema(min_=0),
            },
            "additionalProperties": True,
        },
        enforcement="sbatch / srun argv built from typed fields (never shell-quoted)",
        failure_mode="refuse if site policy disallows sched.slurm for this plugin",
        verification="SELFTEST_SCHED_SLURM_ALLOWED",
        site_policy_field="plugins.sched_slurm_allowed",
        requires_host_helper=True,
    ),
]


_BY_NAME = {c.name: c for c in CAPABILITIES}


def get_capability(name: str) -> CapabilityDef | None:
    return _BY_NAME.get(name)


def all_capability_names() -> list[str]:
    return [c.name for c in CAPABILITIES]
