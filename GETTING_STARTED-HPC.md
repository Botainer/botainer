# Getting started on HPC

This is the HPC version of [GETTING_STARTED.md](GETTING_STARTED.md). It
covers Apptainer + Slurm + Lmod + per-cluster scratch. If you're on a
laptop, read [GETTING_STARTED.md](GETTING_STARTED.md) instead.

## What's different on HPC

| | Laptop | HPC |
|---|---|---|
| Runtime | Docker | Apptainer |
| Image | `docker pull` or `docker build` | Build `.sif` once; copy to cluster |
| Network | Docker can enforce `none`/allowlist | Apptainer **shares the host network namespace** — it CANNOT enforce `none`/allowlist; gate at the cluster firewall or a host-side proxy (see §network notes below) |
| Auth | Log in: open the link, paste the code back | Same — log in on the cluster, nothing to copy |
| Scratch | container `/scratch` → a dir under your state root (nothing else to point at) | container `/scratch` → **the cluster's real scratch filesystem**, set by the profile's `scratch.template` |
| Modules | n/a | Lmod with cluster-specific paths |
| Submission | Foreground `docker run` | `sbatch` to scheduler |

## 0. Before you start: know your cluster

Check with your cluster docs (or system staff) for:

- **Scratch path template** — `/scratch/$USER`, `/gpfs/scratch/$USER`, etc.
- **Module system** — usually Lmod; the init path varies
  (`/etc/profile.d/lmod.sh`, `/opt/apps/lmod/lmod/init/...`, etc.).
- **Partitions** — `scavenge`, `day`, `gpu`, `bigmem` and their
  walltime caps.
- **Account / project** to charge.
- **GPU resources** — `--gpus=1`, `--gres=gpu:a100:1`, etc.

botainer's `cluster.yaml` captures all of this once.

## 1. Install botainer on the cluster

**Prerequisites** (login node): Python ≥ 3.10, `apptainer` (or `singularity`)
on PATH, and `sbatch`/`srun`/`squeue`. Many clusters gate Python behind the
module system — `module load python` (or a specific version) first if the
system `python3` is < 3.10. `apptainer` is often login-node-available but
compute-only on some sites (see `botainer hpc setup --probe`).

```sh
# On the LOGIN node
module load python 2>/dev/null || true   # if your cluster gates python
git clone https://github.com/botainer/botainer ~/src/botainer
bash ~/src/botainer/tools/pkg/install-hpc.sh
```

That script does the venv, the `pip install`, `botainer setup`, and the shell-rc
line — and it knows two things about clusters that are easy to get wrong by
hand: it picks the rc file matching your `$SHELL` rather than assuming
`.bashrc`, and if you run it on a login node with no apptainer it **defers the
image build** and tells you to redo that step inside `salloc`, instead of
failing halfway through.

<details>
<summary>Prefer to do it by hand?</summary>

```sh
# On the LOGIN node
module load python 2>/dev/null || true
git clone https://github.com/botainer/botainer ~/src/botainer
cd ~/src/botainer
python3 -m venv ~/.botainer-venv          # python3 (>=3.10)
source ~/.botainer-venv/bin/activate
pip install -e .

# Add to your shell rc (use ~/.zshrc if your $SHELL is zsh)
echo 'source ~/.botainer-venv/bin/activate' >> ~/.bashrc

# One-time per-host setup: installs the bundled plugins + writes your user
# policy (~/.botainer/policy.yaml). Required before `hpc setup`/`hpc build`.
botainer setup
```
</details>

(A PyPI release is intended, which would replace the clone+venv step. Nothing is published yet.)

## 2. Storage layout (where each component goes)

**Keep botainer state on `$HOME`** (default `~/.botainer/`). The state
dir is small (~100 MB typical) but holds things that are unrecoverable
if wiped:

- OAuth credentials and per-project profiles (`shared-auth/`)
- Project UUIDs (`state/<uuid>/project-id`) — lose this and the project's
  identity becomes unrecognized; agents refuse to launch
- Built `.sif` images (3–5 GB each; ~15 min rebuild on a compute node)
- The installed plugin tree (`plugins/`)

**Do NOT redirect `MY_BOTAINER` to scratch.** Earlier versions of this
guide and `botainer hpc setup` suggested `export MY_BOTAINER=$SCRATCH/...`
— that was actively dangerous. Most HPC scratch volumes auto-purge
(commonly 30-90 days; check your cluster's policy); the next purge wipes all
of the above and the next session breaks in confusing ways. If you've
already set this, `unset MY_BOTAINER` (and remove it from `~/.bashrc`);
`botainer doctor` will flag the condition.

**Per-session scratch IS configured per cluster** via the profile's
`scratch.template` (e.g. `/path/to/scratch/${USER}/${SLURM_JOBID}` — the
exact root depends on your cluster; the bundled profiles ship the right
one for each). That's where genuinely-disposable session work goes —
and it IS on scratch, by design, because losing it is harmless.
`botainer hpc info` shows the active template.

**To point it somewhere else** — your cluster is not bundled, the bundled
profile guessed wrong, or you want a different volume — edit the `scratch:`
block of your own profile at `<state_root>/cluster.yaml` (the file
`botainer hpc setup` wrote):

```yaml
scratch:
  template: /scratch/${USER}/botainer/${SLURM_JOB_ID}
```

`${USER}` and Slurm's job variables expand at session compose. Re-run
`botainer hpc info` to confirm the template changed, and `botainer hpc setup
--probe` to check the base path actually exists on this cluster.

If the template cannot be expanded or its base does not exist, botainer does
NOT fail — it falls back to a scratch dir under your state root (i.e. under
`$HOME`) and warns. That fallback is the one to watch: `$HOME` quotas are
small, and bulk agent output will hit them. `generic-slurm` in particular
ships a PLACEHOLDER template that you are expected to edit.

## 3. One-time HPC setup

```sh
botainer hpc setup
```

This is an interactive wizard that:

1. **Autodetects your cluster** — matches the host's `hostname` against
   the `hostname_patterns` of each bundled profile (~50 ship;
   others on request). Note: `$LMOD_SYSHOST` and `$SLURM_CLUSTER_NAME` are
   not yet used — autodetect is hostname-only at v0.1 (env-signal layer is a
   tracked enhancement, cluster-ease roadmap B2). On compute nodes the bare
   nodename usually won't match a login-node pattern; run setup from the
   login node, or pick the profile explicitly with `--cluster=<name>`.
2. **Writes `~/.botainer/cluster.yaml`** — your active profile with
   partitions, scratch template, Lmod init path, account.
3. **Prints the storage layout** for this cluster — what stays on
   `$HOME` (state, credentials, `.sif`) vs the per-session scratch
   template the profile carries.

By default the wizard writes the profile only — it does NOT verify the
module system or the scheduler. Add `--probe` to run a read-only round
of cluster-prereq checks right after writing the profile: apptainer on
PATH, sbatch on PATH, Slurm accounts visible via `sacctmgr`, default
partition reachable via `sinfo`, `$LMOD_PKG` set, and the profile's
scratch base path existing on disk.

```sh
botainer hpc setup --probe              # interactive autodetect + probe
botainer hpc setup --profile <name> --probe # explicit profile + probe
                                            # e.g. --profile generic-slurm
                                            # names: ls cluster_profiles/*.yaml
```

The probe is purely diagnostic — no submit, no chmod, no root — so it's
safe to re-run any time. If you prefer to defer the checks, you can
also run `botainer doctor` (state dir + runtime checks) and let module
/ `sinfo` reachability surface at `botainer hpc submit` time.

If your cluster isn't bundled, `botainer hpc setup` copies `example.yaml` to
`~/.botainer/cluster.yaml` for you to edit — it does NOT prompt for the values.
Open that file and fill in `default_partition` / `default_account` / scratch
template (see [cluster_profiles/example.yaml](cluster_profiles/example.yaml) for
the fields), then set your account with
`botainer config set plugins.hpc-launcher.account <name>`.

`botainer hpc info` shows the active profile any time.

## 4. Build the Apptainer image

Two options:

### A. Build on cluster (if your cluster allows it)

```sh
botainer hpc build agent-claude
```

Uses the profile's `apptainer_cachedir` (typically scratch). Takes
~10-15 min the first time; cached afterwards. Writes
`$MY_BOTAINER/images/botainer-agent-claude.sif` (the `botainer-<agent>.sif`
name the launcher resolves — see §config below).

### B. Build on a workstation, copy over

Many sites don't allow `apptainer build` on login nodes (it needs
fakeroot or sudo). Build on a workstation with Apptainer or Docker
installed, then `scp` the `.sif`:

```sh
# On workstation with apptainer (the .sif MUST be named botainer-agent-claude.sif
# — that's the name `botainer hpc submit` resolves under $MY_BOTAINER/images/):
apptainer build botainer-agent-claude.sif plugins/agent-claude/agent-claude.def
scp botainer-agent-claude.sif <cluster-login-host>:$MY_BOTAINER/images/

# Or via docker (workstation has docker, not apptainer):
docker build -t botainer/agent-claude:0.1 plugins/agent-claude/
docker save botainer/agent-claude:0.1 | gzip > agent-claude.tar.gz
scp agent-claude.tar.gz <cluster-login-host>:/scratch/$USER/

# On cluster:
zcat /scratch/$USER/agent-claude.tar.gz | apptainer build botainer-agent-claude.sif docker-archive:/dev/stdin
mv botainer-agent-claude.sif $MY_BOTAINER/images/
```

The `hpc build` subcommand wraps the workflow for you.

**If you have built through botainer before on this cluster, run one more
command after copying:**

```sh
botainer image forget agent-claude
```

`botainer hpc build` and `botainer image build` record the `.sif`'s sha256 when
they build it, and `start` / `hpc submit` refuse an image whose hash no longer
matches — that check is what catches an image replaced out from under you. A
`.sif` you built elsewhere and copied in is, correctly, not the one botainer
hashed, so it is refused until you say the replacement was deliberate.
`image forget` drops the recorded hash: the file in place is then accepted and
**no longer verified**, until you build it through botainer again.

If this is a fresh cluster install with nothing recorded yet, skip it — there
is nothing to forget, and the command will tell you so.

## 5. Log in — on the cluster, nothing to copy

The login is headless-friendly: run it **on the cluster**, open the link it prints
in any browser (your laptop's is fine), and **paste the authorization code back**
into the terminal. The credential is created on the cluster — you do NOT copy any
file from your laptop.

### Pick a mode first — the example below is not the default

`botainer init` gives a project **`isolated`**: its own credential, its own
login. The worked example in this section uses **`shared`** instead, so running
it is a deliberate change, not a continuation.

| | log in | run at the same time |
|---|---|---|
| **`isolated`** — what `init` gives you | once per project | as many sessions as you like |
| **`shared`** — the example below | once, for all projects | **one session at a time**, across every shared-mode project |

Neither is more correct. The login here is a paste-a-code exchange, so `shared`
saves repeating it for each project; the price is the concurrency limit, and on
a cluster that is easy to hit, because a submitted job keeps running while you
start something else. **If you expect overlapping jobs and sessions, take
`isolated`.** If you work on one thing at a time, `shared` is less setup.

#### Staying on the default (`isolated`)

```sh
cd /path/to/your/project
botainer plugin agent-claude login            # prints a link; paste the code back
```

Nothing else to do — `init` already put the project in this mode.

#### Switching to `shared`

```sh
# On the cluster login node (after the .sif is built — §4):
botainer auth login --shared --agent claude   # prints a link; paste the code back

# Point each project at that shared login:
cd /path/to/your/project
botainer auth use shared --family anthropic
```

`--shared` means one login, reused by all your projects. Commit your project's
`.botainer/project-id` to git so the same UUID is used wherever you check out the
repo.

**This is your subscription, not an API key.** API-key auth and the credential
proxy do not work at v0.1.0; subscription login is the only route.

**Run one shared-mode session at a time.** The token rotates when it refreshes,
so a second live session invalidates the first one's copy. This is easy to hit
on a cluster, where a submitted job keeps running while you start something
else — botainer detects it and refuses rather than logging you out silently.
If you want several at once, that is what `broker` mode is for
(`botainer auth use broker`): the credential stays on the host and the container
never holds it. Broker has not been exercised on a real cluster, so on HPC treat
it as untested; `shared` and `isolated` are the modes with cluster mileage.

<details>
<summary>Fallback: copy a credential file from your laptop (rarely needed)</summary>

Only if the paste-a-code login isn't available in your setup. Log in on your
laptop, then copy the resulting file to the cluster (use the LITERAL cluster path —
`$MY_BOTAINER` would wrongly expand on your laptop):

```sh
# on your laptop
botainer auth login --shared --agent claude
scp ~/.botainer/shared-auth/agent-claude/.credentials.json \
    <cluster-login-host>:'<cluster $MY_BOTAINER>/shared-auth/agent-claude/.credentials.json'
ssh <cluster-login-host> chmod 0600 \
    '<cluster $MY_BOTAINER>/shared-auth/agent-claude/.credentials.json'
```
</details>

## 6. Initialize the project on the cluster

```sh
cd /path/to/your/project           # already cloned via git
botainer init --agent claude --runtime apptainer
```

`--runtime apptainer` is important: it picks the adapter and embeds
HPC-aware defaults in `config.yaml`.
Note: `network.mode` defaults to `internet` — apptainer shares the host
network namespace and cannot enforce `none`/`endpoint-ip-allowlist` (botainer
refuses those rather than pretend to isolate; see `docs/CAPABILITY-SURFACE.md`
§4). Network restriction on HPC is the cluster admin's job (Slurm/site config).

## 7. Inspect, dry-run, then submit

### Foreground (interactive: pwd is login node or interactive srun)

```sh
botainer inspect                          # see SessionSpec
botainer dry-run                          # see apptainer-exec argv
salloc --partition=day --time=02:00:00 --cpus-per-task=4 --mem=16G
botainer start                            # interactive on allocated node
```

### Background via sbatch

```sh
botainer hpc submit \
    --time 120 --cpus 4 --memory-gb 16 \
    --partition day --account <your-account>
```

The CLI accepts both botainer-native flags (`--time 120`, `--cpus 4`,
`--memory-gb 16`, `--gpus N`, `--gpu-type a100`) and a small set of
Slurm-style aliases (`--time 02:00:00` / `--time 2h`, `--cpus-per-task`,
`--mem 16G`, `--gres gpu:a100:1`) which normalize to the same typed
form internally.

`botainer hpc submit` prints a capability summary on the **login node**
and asks for confirmation BEFORE submitting. After
confirmation, `--yes` is automatically threaded into the in-container
`botainer start` so it doesn't block on its own prompt where there's
no TTY. On success the command parses sbatch's stdout for the jobid
and prints next-command hints (`squeue -u $USER -j <jobid>`,
`scancel <jobid>`, `tail -f <output-path>`).

Use `botainer hpc submit --dry-run` to preview the exact sbatch script
without submitting.

**Watch the job:** `botainer hpc logs <jobid> -f` tails the job's output
(it auto-resolves the host-only output path — you don't need to know where
SLURM wrote it). Omit `<jobid>` to use the most recent job. This is the
supported way to follow a batch session; prefer it over a raw `tail -f`.

### GPU

```sh
botainer hpc submit --time 240 --cpus 4 --memory-gb 32 \
    --partition gpu --gpus 1 --gpu-type a100 \
    --account <your-account>
```

`--gres gpu:a100:1` is accepted as a Slurm-style alias and gets
normalized to `--gpus 1 --gpu-type a100`.

The `agent-claude.sif` is `--nv`-enabled (NVIDIA passthrough); the
agent sees `nvidia-smi` and CUDA-aware libraries.

## 8. Let the agent submit its own Slurm jobs

This is the reason to run botainer on a cluster rather than a laptop. The caged
agent can dispatch compute jobs itself — no asking you, no babysitting — and
each one runs in the same cage, never bare on the host.

Paste a `job_profiles:` block into the project's `.botainer/config.yaml`
(`examples/hpc-job-profiles.yaml` is a starter you can edit):

```yaml
job_profiles:                    # TOP-LEVEL — not under `plugins:`
  cpu-small:
    description: "Short CPU task"
    partition: "day"               # ← replace: `sinfo` lists your partitions
    account: "my_allocation"       # ← replace: your Slurm account"
    cpus: 4
    memory: "16G"
    time: "01:00:00"
    max_cpus: 16                 # optional: the agent may request UP TO this
```

Replace `partition` and `account` with your cluster's. They are not
`<angle-bracket>` placeholders on purpose: those characters are refused by the
sbatch-injection charset guard, so a config carrying them would fail at your
first run rather than at the moment you forgot to edit it.

The scalar fields are what a job gets by default; a `max_*` field is the
ceiling the agent may request up to. Without a `max_*`, the default is also the
limit — the agent cannot ask for more.

Then the agent, inside the session, runs:

```sh
botainer-job submit cpu-small -- python train.py
botainer-job status
```

You watch from the login node:

```sh
botainer hpc job-profiles     # what this project offers the agent
botainer hpc jobs-status      # what it has actually submitted
botainer hpc jobs-explain     # what a dispatched job can see and do
botainer hpc jobs-doctor      # why dispatch is not working
```

`job_profiles` must be **top level**. Nested under `plugins:` it is silently
ignored — config validation forbids unknown keys there, so the block vanishes
and nothing says why. `jobs-doctor` diagnoses exactly this.

The dispatcher starts itself when a session with `job_profiles` launches. It
polls a directional mailbox: the agent writes requests, the host writes results,
and the host-private side is never bound into any container.

## 9. Inspect / stop / cleanup

```sh
botainer hpc status                  # squeue -u $USER filtered to botainer jobs
botainer hpc attach --jobid <jobid>  # srun --overlap into a running job (Slurm)
botainer hpc stop <jobid>            # scancel
botainer hpc stop --all              # scancel all botainer jobs across projects
```

## Extras

### Nudging a running session
When the agent hits a rate limit or stops, you usually want to tell
it "continue" without re-attaching. From a *separate* login-node
shell:

```sh
botainer hpc status                      # show running jobs + nudge target
botainer nudge "continue"                # injects "continue\n" into agent's prompt
botainer nudge --in 30m "rate limit clears in 30 min"   # schedule via at(1)
```

This works by `srun --overlap`-ing into the running step on the
compute node and running `screen -S botainer-<jobid> -X stuff -- ...`
against the host-side screen session the sbatch script created. The
`nudge` plugin must be enabled. (Previously: in-container tmux socket;
retired so nothing has to be mounted into the agent's filesystem.)



## What the agent sees inside the .sif

Same as the Docker version, plus HPC-specific bits via the
`hpc-modules` plugin:

- `/workspace` (rw) — your project (bound from `$PROJECT_ROOT`).
- `/packages` (rw) — bound from `$MY_BOTAINER/state/<uuid>/packages/`;
  persists across jobs.
- `/scratch` (rw) — **see `docs/STORAGE.md`; on a cluster you probably
  want to move this off your home quota.** Bound from
  `$MY_BOTAINER/state/<uuid>/scratch`,
  ALWAYS. Not `$SCRATCH`, not `$SLURM_TMPDIR` — botainer never reads
  either. So `/scratch` lives wherever you pointed `MY_BOTAINER`, and
  its quota is that filesystem's quota. If you want big intermediates
  on a fast node-local disk, that is not wired at v0.1
  (per cluster profile).
- `/workspace/.botainer/AGENT_HINTS.md` (ro) — includes cluster
  preamble: time remaining, GPU info, scratch path, module
  availability.
- Captured Lmod env from login node if `hpc-modules` is enabled.
- Module software dirs (e.g. `/apps/python/3.11/bin`) bound **read-only** —
  **only if** the cluster admin enabled it (see the admin note below).

> **Module software (`hpc-modules`) requires an admin ON-switch.** Binding the
> host `/apps/...` dirs that `module load` adds is OFF until the cluster admin
> lists the software roots in the **root-owned** `/etc/botainer/policy.yaml`
> (`mounts.cluster_software_roots`). A USER cannot set this (it's ignored from
> the user policy). If your module tools are unreachable by name, botainer warns
> at launch and you should ask your admin — point them at
> [`docs/SITE-ADMIN.md`](docs/SITE-ADMIN.md). Until then, module binaries are
> still reachable by absolute path (e.g. `/apps/python/3.11/bin/python`).
>
> **In-container `module load` (optional, admin-set).** If your admin also sets
> `mounts.cluster_lmod_root` + `cluster_modulepath_roots`, the `module` command
> works INSIDE the container — the agent (or your batch job) can run
> `module avail` / `module load <name>/<version>` dynamically, instead of
> pre-declaring modules at submit time. Check with `botainer hpc setup --probe`
> or `botainer doctor` (both report whether it's ENABLED). If `module` isn't
> found in the container, this feature is OFF at your site.

## Cluster-specific config (.botainer/config.yaml)

```yaml
runtime: apptainer            # a string; `botainer init --runtime apptainer` sets it
# Absolute path to the built .sif (no ${...} interpolation). The image-build
# default name is botainer-<agent>.sif under $MY_BOTAINER/images/:
image: /home/USER/.botainer/images/botainer-agent-claude.sif

network:
  mode: internet              # apptainer shares the host network; it CANNOT
                              # enforce none/endpoint-ip-allowlist (those are
                              # refused, not silently ignored — see §4 of
                              # docs/CAPABILITY-SURFACE.md)

mounts:
  extra:
    - source: /path/to/your/datasets   # your cluster's data path
      target: /data
      mode: ro
      reason: training datasets

plugins_enabled:
  - agent-claude
  - git
  - hpc-launcher
  - hpc-modules               # bind login-node modules into container
  - nudge                     # host-side screen-based input injection

plugins:
  hpc-launcher:
    account: my-account         # your Slurm account (sbatch --account)
    partition: day
    time_minutes: 240           # 4 hours, as an integer count of minutes
  hpc-modules:
    modules:
      - cuda/12.4
      - cmake/3.28
```

## Troubleshooting

```sh
botainer hpc info                  # show active cluster profile
botainer doctor --strict           # warns about anything off
botainer doctor --auth-only        # just check credential state
```

Common issues:

- **"no Lmod bootstrap found"** — your cluster's Lmod is in an unusual path;
  export `BOTAINER_LMOD_BOOTSTRAP=/path/to/lmod/init/bash` (operator/host env —
  it is sourced on the node and is deliberately NOT read from project config,
  which would be an RCE vector). The cluster admin can set it once in
  `/etc/profile.d/`. (There is no `cluster.yaml lmod_init_path` key.)
- **module tools not found by name** — the admin hasn't set
  `mounts.cluster_software_roots` in the root-owned `/etc/botainer/policy.yaml`
  (the #160 ON-switch; see [`docs/SITE-ADMIN.md`](docs/SITE-ADMIN.md)). botainer
  warns at launch when this is the case. Until then, use absolute paths.
- **"sbatch: error: invalid account"** — set `plugins.hpc-launcher.account`.
- **"no apptainer command"** — `module load apptainer` first
  (or add to the cluster profile's bootstrap).
- **"image not found"** — `botainer hpc build agent-claude` or copy the
  `.sif` to `$MY_BOTAINER/images/`.
- **Quota errors during install** — the agent `.sif` images are 3–5 GB
  each and live under `$MY_BOTAINER/images/`. If `$HOME` quota is too
  tight: move ONLY the images out via `apptainer.cachedir` in your
  `~/.botainer/cluster.yaml` (the profile already points it at a sane
  default; override if needed). **Do not** redirect the entire
  `MY_BOTAINER` to scratch — see §2 above (scratch auto-purges and would
  wipe credentials, project UUIDs, and the plugin tree). Image rebuild
  cost matters but is recoverable; credential loss is not.

## See also

- [GETTING_STARTED.md](GETTING_STARTED.md) — laptop quickstart (Docker)
- [docs/SITE-ADMIN.md](docs/SITE-ADMIN.md) — for admins: the root-owned
  site policy (trust anchor) + the hpc-modules ON-switch
- [docs/HPC-WORKFLOW.md](docs/HPC-WORKFLOW.md) — day-to-day HPC flow
- [cluster_profiles/](cluster_profiles/) — bundled profiles + example
- `botainer help nudge` — nudge tradeoff for HPC users
- `botainer doctor --strict` — surface issues before launching jobs
