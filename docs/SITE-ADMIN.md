# Site administration — the root-owned site policy

This is for the **cluster administrator** (root), not end users. It covers the
one file an admin owns: `/etc/botainer/policy.yaml`.

## What the site policy is

`/etc/botainer/policy.yaml` is the **root-owned** ceiling for everything botainer
will let a session do on this host. It is the trust anchor: a user's own
`~/.botainer/policy.yaml` can only *tighten* most fields (never widen), and a few
fields are read from the site policy **only** (a user value is ignored).

- **Root-owned, root-writable.** Create it as root; mode `0644` (world-readable,
  root-writable). The trust of the default `/etc/botainer/policy.yaml` read path
  rests on **filesystem permissions**: `/etc` (and the file) must be writable
  ONLY by root, so a non-root user cannot tamper with the ceiling. (botainer does
  not re-check the owner uid on this default path — on a correctly-permissioned
  `/etc` it would be redundant, and a user who can write `/etc` already has
  root-equivalent power.) The explicit `_is_root_owned` owner check IS applied to
  the `BOTAINER_SITE_POLICY` env-override path (used by the HPC compute-node
  bind), where the env var is user-controlled and the owner check is the actual
  defense. Net: keep `/etc/botainer/policy.yaml` root-writable-only.
- **Optional.** With no site policy, botainer falls back to safe defaults
  (first-party plugins only; the relevant features below are OFF).

```sh
sudo install -m 0644 /dev/stdin /etc/botainer/policy.yaml <<'YAML'
version: policy-v1   # MUST be the string "policy-v1" — `1` is refused
plugins:
  allowed_tiers: [first-party]        # add 'third-party' to permit them
mounts:
  cluster_software_roots:             # see "HPC modules" below — OFF if empty
    - /apps
    - /software
    - /opt
network:
  default_mode: internet              # ceiling: none | internet | endpoint-ip-allowlist
YAML
```

Verify what a session will see: `botainer policy show` (prints the effective
policy = site ∩ user; fields marked `[SITE-ONLY]` come from this file alone).

## HPC modules — the `cluster_software_roots` ON-switch (#160)

The `hpc-modules` plugin can bind the host software dirs that `module load` adds
to `PATH`/`LD_LIBRARY_PATH` (e.g. `/apps/python/3.11/bin`) **read-only** into the
container, so module-loaded software is reachable. **This is OFF until you list
the software roots here:**

```yaml
mounts:
  cluster_software_roots: [/apps, /software, /opt]
```

- **SITE-ONLY.** This field is read from `/etc/botainer/policy.yaml` ONLY — a
  user's `~/.botainer/policy.yaml` value is **ignored** (otherwise a user on a
  multi-tenant node could self-grant host binds). `botainer policy set
  mounts.cluster_software_roots ...` as a user has no effect and warns.
- **Point it at software trees, never home/scratch.** Anything within a listed
  root that `module load` adds AND is co-tenant-readable becomes visible (RO) in
  the container. List `/apps`, `/software`, `/opt/<pkg>` — not `/home` or
  `/scratch`. (The launcher additionally refuses system roots + `~/.ssh`-class
  dirs + shallow paths, but the ceiling is your control.) See
  `docs/CAPABILITY-SURFACE.md` §4h.
- **Empty / unset = OFF.** If a user has `hpc-modules` enabled with modules
  configured but no root matches, botainer WARNS at launch (and in `botainer hpc
  submit`) that module tools will be unreachable — they should ask you to add
  the root.

### In-container `module load` — the `cluster_lmod_root` ON-switch

Optionally, let agents run `module load` **inside** the container (dynamic —
a batch job can load different software per stage instead of predicting
everything at submit time). OFF until you set both fields:

```yaml
mounts:
  cluster_lmod_root: /apps/lmod/lmod              # your Lmod install tree
  cluster_modulepath_roots: [/apps/modulefiles]   # dirs `module avail` searches
```

- **SITE-ONLY**, same rule as `cluster_software_roots` above.
- When set, botainer binds both trees read-only and injects
  `LMOD_PKG`/`MODULEPATH`/`BASH_ENV` so `module avail` / `module load` work
  in the agent's shell. Pair with `cluster_software_roots` so the software
  the modulefiles point at is also reachable.
- **Only name admin-managed trees.** A user-writable directory in
  `cluster_modulepath_roots` would let any co-tenant author modulefiles the
  agent might execute. Same admin-discipline note as `cluster_software_roots`.
- Find your Lmod tree with `echo $LMOD_PKG` on a login node.
  See `docs/CAPABILITY-SURFACE.md` §4k.

### Lmod bootstrap (if auto-detection misses it)

The hook auto-detects Lmod (`LMOD_PKG`/`LMOD_CMD` + canonical install paths). If
your site installs Lmod somewhere non-standard, export the bootstrap path for
users (e.g. in `/etc/profile.d/`): `export BOTAINER_LMOD_BOOTSTRAP=/path/to/lmod/init/bash`.
It is **never** read from project config (that would be an RCE vector).

## Network ceiling

`network.default_mode` is the most-permissive mode any session may use (sessions
can request a stricter mode). **Apptainer caveat:** apptainer shares the host
network namespace, so `none`/`endpoint-ip-allowlist` are *not enforced* on the
apptainer/HPC path — botainer surfaces this rather than pretending to isolate.
See `docs/CAPABILITY-SURFACE.md` §4.

## Plugin tiers

`plugins.allowed_tiers` defaults to `[first-party]`. To permit third-party
plugins on this host, add `third-party` (they then still require declarative
consent — see `docs/CAPABILITY-SURFACE.md`).
