# hpc-modules — capture `module load` env into the container

HPC clusters ship software via Lmod / environment-modules. Users run
`module load python/3.11 gcc/13 cuda/12.3` to add those tools to
PATH/LD_LIBRARY_PATH/etc. By default the agent container doesn't see
that environment because (a) `apptainer --cleanenv` strips host env
and (b) `module` isn't available inside.

`hpc-modules` solves it by:

1. Running `module load <each>` on the host (login or compute node)
   inside the plugin's `host_pre_launch` hook.
2. Capturing the resulting env, filtering out Lmod internals + the
   launcher's denylist, writing the rest to a file.
3. Delivering that env into the container — depends on the runtime:
   - **laptop / single-node (docker or direct `apptainer`):** the adapter
     applies the file via `--env-file` (after `--cleanenv`). ⚠ **KNOWN
     LIMITATION (#1, tracked #216):** `--env-file` SETs (replaces) vars, so
     path-list vars (`PATH`, `LD_LIBRARY_PATH`, …) here **clobber** the
     container's own values rather than prepending — this can drop the agent
     binary's dir and break the launch. The launcher WARNS when this can happen.
     Until the fix lands, invoke module tools by ABSOLUTE PATH (the software
     roots are bound read-only at their host paths) or use the `sbatch` flow.
   - **HPC `sbatch`:** the session is composed on the LOGIN node at submit time
     (compose-at-submit, task #52), where Lmod IS available, so the
     `host_pre_launch` hook captures the module env there. The launcher **splits**
     it into scalar vars (delivered via the container `--env-file`) and PATH-list
     vars (`PATH`, `LD_LIBRARY_PATH`, …), which are **prepended** onto the
     container's own values via a small in-container trampoline (so the agent
     binary's dir is preserved — this is the flow that does it right). The
     compute-node container execs the agent directly; no botainer runs inside it.
4. **Binding the host software-root dirs** (`#160`,
   `caps.modules_software_roots`): the dirs `module load` ADDED to
   PATH/LD_LIBRARY_PATH (e.g. `/apps/python/3.11/bin`) are bind-mounted
   READ-ONLY at their original paths, bounded by the root-owned SitePolicy
   `mounts.cluster_software_roots` ceiling — so the env above points at files
   that actually exist in the container. Empty ceiling → no binds (feature
   OFF). See `docs/CAPABILITY-SURFACE.md` §4h/§4i.

## Setup

```yaml
plugins_enabled:
  - agent-claude
  - hpc-launcher
  - hpc-modules

plugins:
  hpc-modules:
    modules: [python/3.11, gcc/13, cuda/12.3]
    purge_first: true
    fail_on_missing: true
```

> **Bootstrap path is operator-controlled, not project config.** The Lmod
> bootstrap is `source`d in a shell on the node, so there is deliberately no
> `bootstrap_script:` config key (a git-shareable repo could otherwise run
> arbitrary code as you — AUDIT 2026-06-09 C2). The path is auto-detected from
> the host login env (`LMOD_PKG`/`LMOD_CMD` + canonical install paths); an
> operator can override it by exporting `BOTAINER_LMOD_BOOTSTRAP=<path>`.

## What gets through

- PATH, LD_LIBRARY_PATH, LIBRARY_PATH, CPATH
- CMAKE_PREFIX_PATH, PKG_CONFIG_PATH
- MANPATH, INFOPATH
- CUDA_HOME, CUDA_PATH, CUDA_VISIBLE_DEVICES
- MPI_HOME, OMPI_DIR, JAVA_HOME
- Any var in the plugin's `allowed_env_overrides`

## What's filtered out

- Lmod internals (LMOD_*, _LMFILES_*, MODULEPATH, etc.)
- BASH_FUNC_* (bash exported functions)
- Botainer-managed routing vars (PIP_TARGET, PYTHONPATH, NODE_PATH,
  etc.) — the Dockerfile sets these to /packages/* and modules must
  not override.
- Vars on the policy's `env_var_denylist` (unless explicitly in
  `allowed_env_overrides`).

## Software-root binds (so the env points at real files)

For the captured PATH/LD_LIBRARY_PATH to be useful, the host dirs they name
must exist inside the container. The site administrator opts in by listing the
software trees in the **root-owned** site policy:

```yaml
# /etc/botainer/policy.yaml  (root-owned; users cannot set this)
mounts:
  cluster_software_roots: [/apps, /software, /opt]
```

Only dirs `module load` ADDED, contained within that ceiling, are bound (RO,
identity). It is never an umbrella (baseline-diff + system-root + `~/.ssh`-class
denylist + min-depth). Empty ceiling = feature OFF. Point the ceiling at
software trees, never home/scratch (see `docs/CAPABILITY-SURFACE.md` §4h).

## Caveat

The `module` shell function isn't available inside the container — the env +
binds are applied by the launcher before the agent starts. If you need to
switch modules mid-session: edit the config and restart. If a tool isn't on
PATH (its root is outside the site ceiling, or the feature is off), invoke it
by absolute path.

See `/workspace/design/HPC-MODULES-DESIGN.md` and
botainer's internal design notes for the full rationale.
