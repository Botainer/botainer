# DEPLOY — install strategies

Three scenarios covered:

1. **Clean install** (no v0.0.x).
2. **Parallel install** (you have v0.0.x and want both).
3. **HPC install** (Apptainer + Slurm).

## Scenario 1 — clean install

```sh
git clone https://github.com/botainer/botainer ~/src/botainer
cd ~/src/botainer
pip install -e .                 # editable until published
botainer setup
```

State dir defaults to `~/.botainer/`. Binary on PATH: `botainer`.

That's it. Now follow `GETTING_STARTED.md`.

## Scenario 2 — parallel with v0.0.x

If you already have v0.0.x at `~/.botainer/`, install v0.1.0 in
parallel using `MY_BOTAINER`:

```sh
git clone https://github.com/botainer/botainer ~/src/botainer-v0_1
cd ~/src/botainer-v0_1

# Use a separate venv so v0.1.0's pip-installed CLI doesn't shadow v0.0.x
python -m venv ~/opt/botainer-v0_1/.venv
~/opt/botainer-v0_1/.venv/bin/pip install -e .

# Define an alias that uses a separate state dir + the v0.1.0 binary, and
# persist it to your shell rc (~/.zshrc, or ~/.bashrc):
echo "alias bot1='MY_BOTAINER=~/.botainer-v0_1 ~/opt/botainer-v0_1/.venv/bin/botainer'" >> ~/.zshrc
source ~/.zshrc
```

(Or skip the hand-written steps and run `bash tools/pkg/install.sh`, which
derives the venv/state-dir/binary and PRINTS the exact alias line to paste —
the single source of truth for this flow. It prints; it does not edit your rc.)

Verify the v0.0.x install is untouched and v0.1 runs under the alias:

```sh
ls ~/.botainer/                 # v0.0.x state, unchanged
which botainer                  # v0.0.x binary if it's on PATH (unchanged)

bot1 --version                  # 0.1.0a1  (the aliased v0.1 binary)
bot1 setup                      # creates ~/.botainer-v0_1/
```

After v0.0.x sunset:

```sh
unalias botainer
mv ~/.botainer-v0_1 ~/.botainer    # migrate state location
unset MY_BOTAINER
# botainer just works now
```

## Scenario 3 — HPC

See [`GETTING_STARTED-HPC.md`](GETTING_STARTED-HPC.md) (user onboarding) and
[`docs/SITE-ADMIN.md`](docs/SITE-ADMIN.md) (root-owned site policy + the
hpc-modules ON-switch). Summary:

1. Install on a login node (same as Scenario 1 but typically into
   `$HOME`).
2. Build the Apptainer `.sif` image (instead of Docker). The
   `hpc-launcher` plugin handles `sbatch` submission of the agent's
   Slurm job.
3. Per-project state lives in `~/.botainer/state/<uuid>/` on the
   cluster shared filesystem. `/packages` and `/scratch` are
   per-project as on laptop, but HPC `/scratch` filesystems
   (60-day-cleanup style) can be used as a backing store via the
   `scratch_extra:` policy field.

## File layout after install

```
$HOME/
├── .botainer/                     # state dir (or $MY_BOTAINER)
│   ├── policy.yaml                # site/user policy
│   ├── plugins/                   # installed plugin trees
│   │   ├── agent-claude/          # default-enabled
│   │   ├── agent-claude-proxy/    # opt-in (real key stays on host)
│   │   ├── git/                   # default-enabled (guarded mode)
│   │   ├── hpc-launcher/          # opt-in (HPC only)
│   │   ├── hpc-modules/           # opt-in (HPC: `module load`)
│   │   ├── nudge/                 # opt-in (inject text into running agent)
│   │   ├── web-ports/             # opt-in (Jupyter/Streamlit/Gradio forwarding)
│   │   └── installed.lock         # provenance: name + sha256:hash
│   ├── state/<uuid>/              # per-project state (one per project)
│   │   ├── meta.json              # uuid, name, path_history (host-keyed)
│   │   ├── protected.hashes
│   │   ├── data/<plugin>/profiles/<auth_profile>/  # credentials
│   │   ├── packages/              # per-project package installs
│   │   │   ├── pip/
│   │   │   ├── conda_envs/
│   │   │   ├── julia_depot/
│   │   │   ├── R_libs/
│   │   │   └── ...
│   │   ├── scratch/               # per-project ephemeral
│   │   ├── sessions/<sid>/        # session logs + AGENT_HINTS.md
│   │   └── locks/
│   ├── logs/
│   └── images/                    # built docker images (digest-named)
└── ...
```

## Environment variables

| Var | Purpose | Default |
|---|---|---|
| `MY_BOTAINER` | Override state-dir root (for v0.0.x coexistence) | unset → `~/.botainer/` |

No other env vars affect security. Setting `MY_BOTAINER` to a path
containing `..` is refused.

## Verifying the install

```sh
botainer --version              # 0.1.0a1
botainer doctor                 # all checks, with remediation if any
botainer doctor --json          # machine-readable
botainer doctor --auth-only     # credential-surface only
botainer plugin list --available  # see what plugins are installed + bundled
botainer schema config           # JSON schema for IDE / LLM use
```

`botainer doctor` returns 0 unless something actionable needs fixing.

## Building the agent image

The first time you `botainer start`, the launcher needs a built image.
Build it with:

```sh
botainer image build agent-claude   # ~8-12 min first time
botainer image list                  # see what's built
```

For non-default agents or to rebuild after Dockerfile changes:
```sh
botainer image build agent-claude --no-cache
botainer image build --all           # rebuild all installed plugins with Dockerfiles
```

### Reproducibility caveat (#144)

Botainer image builds are NOT bit-exact-reproducible across hosts.
Two builds of the same plugin tree on different machines produce
different sha256 digests because:

- The base image (`node:20-bookworm-slim`) gets re-pulled and can
  receive security patches between builds.
- `apt-get update` + `npm install -g foo@latest` resolve to whatever
  is current on the registries that day.
- Local-time stamps and filesystem ordering can leak into apptainer
  `.sif` payloads.

For repeatable builds, pin everything (base image SHA, package
versions in the Dockerfile/.def, `--build-arg CLAUDE_CODE_VERSION=…`)
and trust the recorded digest in `~/.botainer/installed.lock` to
detect drift between rebuilds. Full reproducible-build infrastructure
(buildkit reproducible, locked apt mirror, etc.) is v0.2 work.

## Uninstall

```sh
pip uninstall botainer
rm -rf ~/.botainer/             # or your MY_BOTAINER dir
```

That removes the launcher and all state. Your project repositories
are unaffected (project-id lives in their `.botainer/project-id`
files; that's the only on-project trace).
