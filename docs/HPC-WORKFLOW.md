# HPC Workflow — botainer on a Slurm cluster

This is the **user-facing** reference for running botainer on HPC. Every
step ends with "Next:" so you always know what to type. Sections marked
**⛔ NOT YET WIRED** describe the intended workflow but require code in
`HPC-IMPLEMENTATION-PLAN.md` to land first.

If a section says ✅, the path works today (verified by
`tests/integration/test_hpc_smoke.py` or by real-host testing logged
in a HANDOFF doc).

## Audience

You are a researcher with a login on a Slurm cluster
account. You want to run a coding agent (Claude or Codex) on compute
nodes, possibly with GPUs, possibly as a long-running session, possibly
in parallel.

The product you're working with is botainer v0.1.x — the HPC-first
re-design. Docker on laptop is a development scaffold; HPC is the
target. HPC parity is a non-negotiable project rule, not a nice-to-have:
every feature must answer "what does this look like on HPC?" before it ships.

## Table of contents

1. [Concepts you need before installing](#1-concepts)
2. [Install on the login node](#2-install)
3. [First-time cluster setup](#3-cluster-setup)
4. [Build the apptainer image](#4-image-build)
5. [Init your project](#5-init-project)
6. [Choose your run mode](#6-modes) — same-node / submit / attach / pool
7. [Day-to-day use](#7-day-to-day)
8. [Working with nudge across nodes](#8-nudge)
9. [Limits, queues, parallelism](#9-limits)
10. [Templates: cluster-specific configs](#10-templates)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Concepts you need before installing <a name="1-concepts"></a>

botainer wraps your agent (`claude` or `codex`) in a container so that
the agent only sees what you explicitly grant it. On HPC, the container
runtime is **Apptainer** (formerly Singularity); on a laptop it's Docker.

| Thing | What | Where it typically lives |
|---|---|---|
| **State root** | Per-user botainer state | `$HOME/.botainer/` (or `$MY_BOTAINER` if you set one) |
| **Project** | A directory you `botainer init`-ed; gets a UUID | Anywhere on shared FS; state at `<state_root>/state/<uuid>/` |
| **Agent image** | An apptainer `.sif` file built from a `.def` | `<state_root>/images/botainer-agent-claude.sif` |
| **Cluster profile** | YAML describing partitions, accounts, GPU types | `<state_root>/cluster.yaml` |
| **Credentials** | Per-project OR host-wide ("shared") | `<state_root>/state/<uuid>/data/agent-claude/` (per-project) or `<state_root>/shared-auth/agent-claude/` (host-wide) |

**How the agent authenticates.** Two separate questions: *what you pay with*
and *where the credential sits*.

**What you pay with.** botainer signs you in with your normal Claude or Codex
**subscription** (OAuth) — that is the only route that works at v0.1.0.
API-key authentication has code but has never been tested, and the credential
proxy does not start at all; neither is usable. `botainer auth login`.

**Where the credential sits** — the "auth mode", set per project with
`botainer auth use <mode>`:

| mode | where the credential lives | concurrent sessions |
|---|---|---|
| **broker** | on the **host**. The container never holds it; a host-side process answers on its behalf. | yes — several at once |
| **shared** | one host-wide login, copied into each project. The container can read **and overwrite** it. | **one at a time** — see below |
| **isolated** | a separate login per project, in that project's state dir. Container can read and overwrite it. | yes, but you log in per project |
| **proxy** | ⚠ **NOT FUNCTIONAL at v0.1.0**, and refused outright on the sbatch path. | — |

**On a cluster, `shared` is the one that bites.** The token rotates when it
refreshes, so a second live shared-mode session invalidates the first one's
copy — and on HPC "a second session" is easy to cause by accident, because a
submitted job keeps running while you start another. botainer detects it and
refuses, but if you routinely want more than one session, that is what
**broker** is for.

`broker` runs a host-side process, so on HPC it must reach the compute node
from wherever it runs; that path has not been exercised on a real cluster.
`shared` and `isolated` are the modes with cluster mileage.

**Run modes** for an HPC session:
- **same-node** ("here"): run inside the existing Slurm allocation (you `salloc`'d or are already in a job step). No new sbatch.
- **submit**: render an sbatch script, submit it; the agent starts on the compute node.
- **attach**: connect to an already-submitted job (e.g., your sbatch is still running, you want to interact via nudge or `srun --overlap`).
- **pool** ⛔ NOT YET WIRED: pre-allocate N nodes, run multiple agent sessions across them. See HPC-IMPLEMENTATION-PLAN.md.

**Next:** decide which mode you want for your first session. If unsure, **submit** (a single job, you watch it) is the right starting point.

---

## 2. Install on the login node <a name="2-install"></a>

Pre-requisites (most clusters have all of these; yours may vary):
- ssh access
- `apptainer` (or `singularity`) on PATH (or via `module load apptainer`)
- `sbatch`/`squeue`/`scancel`
- Python ≥ 3.10 (often `module load python`)

```bash
ssh <your-cluster>                                 # your cluster login node
git clone <your-remote> ~/src/botainer-v0_1
cd ~/src/botainer-v0_1
git checkout v0.1-dev                              # or whatever tag/branch is current

# Editable install into a dedicated venv (don't pollute your shell Python)
python3 -m venv ~/.venvs/botainer-v0_1
~/.venvs/botainer-v0_1/bin/pip install --quiet --upgrade pip
~/.venvs/botainer-v0_1/bin/pip install --quiet -e .

# Alias so the binary is callable and uses a parallel state dir.
#
# Storage layout in full: docs/STORAGE.md — READ §2 IF YOU ARE ON HPC.
# Keep `MY_BOTAINER` on a PERSISTENT filesystem — `$HOME` or a project
# space. Do NOT put it on `/scratch`: most HPC scratch volumes auto-purge
# (commonly 30-90 days) and the purge takes your CREDENTIALS and every
# project's state with it, unrecoverably. See GETTING_STARTED-HPC.md §2.
source ~/.bashrc

botainer --version
botainer setup
botainer doctor    # confirms apptainer + sbatch are on PATH; warns on anything missing
```

If `botainer doctor` reports anything red, fix that BEFORE moving on.

**Next:** §3 — write your cluster profile.

---

## 3. First-time cluster setup <a name="3-cluster-setup"></a>

botainer needs to know your cluster's quirks (partitions, accounts,
GPU types). Around 50 clusters ship with a bundled profile and are
auto-detected by hostname; anywhere else, `generic-slurm` is the
starting point and you adjust it once.

```bash
botainer hpc setup                                 # interactive: autodetect by hostname
botainer hpc setup --cluster=generic-slurm         # works on any Slurm cluster
botainer hpc setup --cluster=<name>                # a bundled profile, by name
botainer hpc setup --cluster=<name> --non-interactive   # for scripts/CI
```

`<name>` is a filename under `cluster_profiles/` without the `.yaml`
(`ls cluster_profiles/`). Both the full identifier and the short cluster
name work — the full id and the short cluster name both resolve to it.

The wizard writes `<state_root>/cluster.yaml`. After running, edit
that file to set your account, partition, and any cluster-specific
overrides — the bundled templates have sensible defaults but you'll
need to fill in `account:` at minimum.

Inspect what botainer thinks your cluster looks like:

```bash
botainer hpc submit --dry-run --time 60 --cpus 2  # shows the sbatch script it WOULD generate
```

If `default_partition`, `default_account`, or `default_time_minutes`
are wrong, edit `~/.botainer-v0_1/cluster.yaml` and re-run `--dry-run`
to confirm.

**Next:** §4 — build the apptainer image.

---

## 4. Build the apptainer image <a name="4-image-build"></a>

Each agent ships a `.def` file (apptainer build recipe). The launcher
builds it into a `.sif` and records its path + digest into the
installed.lock so subsequent runs find it automatically.

```bash
# Build agent-claude. Takes 10-20 min the first time.
botainer image build agent-claude --runtime apptainer

# Same for codex if you want to use it:
botainer image build agent-codex --runtime apptainer
```

Notes:
- `--runtime auto` (the default) picks apptainer when docker is absent (the HPC case), so you can drop `--runtime apptainer` once you confirm.
- The build runs on the **login node**. Most clusters allow this (it's just `apptainer build`). If your cluster forbids login-node builds, run it inside an interactive allocation: `salloc --time=30 bash -c 'botainer image build agent-claude --runtime apptainer'`.
- The `.sif` lands at `<state_root>/images/botainer-<agent>.sif`.
- `--no-cache` forces a clean rebuild (apptainer's `--force`).

```bash
botainer image list    # see what's built
```

**Next:** §5 — init your project.

---

## 5. Init your project <a name="5-init-project"></a>

`botainer init` makes a project directory botainer-aware. It writes
`.botainer/config.yaml` and registers a UUID.

```bash
cd /scratch/$USER/my-project    # or wherever your project lives
botainer init --agent claude
```

The init prints a banner with the auth mode (default: shared) and
"Next:" commands. **Read it.**

Edit `.botainer/config.yaml` to tune for your project. Minimum viable
config for HPC:

```yaml
agent: claude
profile: default
runtime: apptainer

resources:
  cpu: 4
  memory_mb: 16384
  time_minutes: 240

plugins_enabled:
  - agent-claude-shared        # or agent-claude if isolated mode
  - git
  - hpc-launcher

plugins:
  hpc-launcher:
    partition: day             # cluster-specific; see cluster.yaml
    account: YOUR_PI_GROUP     # cluster-specific
    cpus: 4
    memory_gb: 16
    time_minutes: 240          # 4 hours; max varies by partition
    # gpus: 1                  # uncomment if you need GPU
    # gpu_type: a100           # cluster-specific GPU type name
    submission_mode: submit    # submit | attach | here
```

**Next:** §6 — pick your run mode and launch.

---

## 6. Choose your run mode <a name="6-modes"></a>

### 6a. Same-node ("here" mode) — for use inside an existing allocation

```bash
# You already have an allocation (you ran salloc earlier or sbatch'd a different job)
echo $SLURM_JOB_ID    # should print a number; if blank, you're not in an allocation

# Run the agent inside this allocation, no new sbatch.
botainer hpc submit --mode=here
```

The launcher will `apptainer exec` your image inline using the current
allocation's resources. Useful for interactive debugging or when you've
already pre-arranged the resources.

The session is composed on the LOGIN node at submit time (compose-at-submit,
task #52): the launcher renders the full `apptainer exec … <image>
<agent-entrypoint>` argv — the SAME render the direct-apptainer path uses — and
bakes it into the sbatch script. The compute-node container execs the agent
entrypoint directly; **no botainer runs inside the container** (the agent image
has no botainer CLI). Binds are scoped to the project workspace, /packages,
/scratch, and the active agent's credential dir — other projects' state, and the
per-project state root itself, stay out of the container.

### 6b. Submit — new Slurm job

```bash
# Submit a fresh sbatch job that runs the agent on the compute node.
botainer hpc submit                                     # confirmation prompt
botainer hpc submit --yes                               # non-interactive
botainer hpc submit --dry-run                           # print the sbatch script
botainer hpc submit --time 02:00:00 --mem 16G \
  --gres gpu:a100:1 --partition gpu --account YOUR  # Slurm-style aliases
```

The launcher:
1. Pre-creates the per-project state subtree + per-agent profile dir
   on the host (apptainer refuses on missing bind sources).
2. Renders an sbatch script to
   `<state_root>/state/<uuid>/sessions/_submit-scripts/submit-<timestamp>.sh`.
3. Runs `sbatch <script>`.
4. Parses `Submitted batch job N`, prints the jobid + output path +
   copy-pasteable status command.

**Status / cancel** — there's a top-level wrapper for both:
```bash
botainer hpc list                                 # this user's botainer jobs
botainer hpc status                               # same as list
botainer hpc cancel <jobid>                       # scancel wrapper
botainer hpc cancel --all                         # all your botainer jobs
botainer hpc logs                                 # tail the most recent job's output (auto-resolves UUID + jobid)
botainer hpc logs <jobid> -f                      # follow a specific jobid (`tail -F`)
# (or by hand — note the host-only outputs dir, a sibling of state/, NOT under
#  the container-bound state subtree; see the SLURM directional-isolation fix:)
tail -f <state_root>/hpc-job-outputs/<uuid>/slurm-<jobid>.out
```

### 6c. Attach — connect to a running job

```bash
botainer hpc submit --mode=attach --jobid 12345    # attach to specific job
botainer hpc submit --mode=attach                  # uses $SLURM_JOB_ID
```

Useful for: interactively poking at an agent that's already running.

### 6d. Pool — multiple agents across pre-allocated nodes ⛔ NOT YET WIRED

A pool is a salloc'd block (e.g., 4 nodes for 8 hours) into which the
launcher dispatches multiple agent sessions via `srun --overlap
--jobid=<allocation>`. Each session gets its own agent process; they
share the allocation's resources.

**Status: planned, not implemented.** See HPC-IMPLEMENTATION-PLAN.md
item "pool mode".

---

## 7. Day-to-day use <a name="7-day-to-day"></a>

After the initial setup, day-to-day looks like:

```bash
cd /scratch/$USER/my-project
botainer hpc submit            # or --mode=here / --mode=attach
# ... session runs ...
# Session ends when you /exit the agent OR Slurm times out

botainer status                 # list active sessions for this project
botainer hpc list               # this user's queued/running botainer Slurm jobs
botainer hpc cancel <jobid>     # scancel one
```

**Logs:** `<state_root>/state/<uuid>/sessions/<sid_or_jobid>/`
contains AGENT_HINTS.md (what we told the agent), session metadata,
and the slurm stdout/stderr.

**Conversation history (shared mode):** the OAuth credential is in
`<state_root>/shared-auth/agent-claude/.credentials.json` (one file
covers all projects). Per-project conversation state stays in
`<state_root>/state/<uuid>/data/agent-claude/profiles/default/`.

**Cleanup:** old sessions accumulate. There's no auto-cleanup at
v0.1.x; run `find <state_root>/state/*/sessions/ -mtime +30 -delete`
if you want to prune by hand.

---

## 8. Working with nudge across nodes <a name="8-nudge"></a>

`botainer nudge "<text>"` injects text into a running agent's terminal
via host-side `screen -X stuff` (earlier versions used in-container
`tmux send-keys` — retired). Useful for: telling a long-running
agent to do something without re-prompting from the start.

**On laptop (Docker):** when the `nudge` plugin is enabled,
`botainer start` wraps `docker run -it` in
`screen -dmS botainer-<sid>` on the host. `botainer nudge` runs
`screen -S botainer-<sid> -X stuff -- "<text>\n"` on the same
host — no docker exec, no in-container state.

**On HPC:** the agent is on a compute node; you're on the login node.
The sbatch script that `botainer hpc submit` writes wraps
`apptainer exec ...` in `screen -dmS botainer-${SLURM_JOB_ID}` on
the compute node, then polls `screen -ls` to keep the Slurm step
alive while the agent runs. For nudge to work cross-node:

```bash
# From login node (after `botainer hpc submit`):
botainer nudge "switch to the test branch"
```

The session record stores the Slurm `jobid` login-side: `hpc submit` writes it
(and `screen_session_id = "botainer-<jobid>"` when nudge is enabled) into the
session record after `sbatch` returns the jobid.
`botainer nudge` reads that record and constructs:

```text
srun --jobid=<recorded> --overlap --overcommit --cpus-per-task=1 \
  --mem=0 screen -S botainer-<jobid> -X stuff -- "<text>\n"
```

automatically. No `--jobid` flag needed in the common case.

**Not validated on a real Slurm cluster yet.** The argv construction
is pinned by `tests/integration/test_nudge_hpc_argv.py`. End-to-end
behaviour on a real cluster is still the next acceptance step. If you hit a
problem on the real cluster, the fallback is:

```bash
ssh <compute_node>
MY_BOTAINER=$HOME/.botainer-v0_1 botainer nudge "..."
```

---

## 9. Limits, queues, parallelism <a name="9-limits"></a>

**Per-project concurrency cap.** Set in your project config:

```yaml
plugins:
  hpc-launcher:
    max_concurrent_jobs: 3
```

Before `botainer hpc submit` actually calls `sbatch`, the launcher queries
`squeue -u $USER --name botainer-<uuid-prefix>` and refuses if the
count is already at the cap, printing the running jobs + the
`botainer hpc cancel` command to free a slot.

**Queue overview** — see this user's botainer jobs:

```bash
botainer hpc list                                 # active botainer jobs only
botainer hpc status --all                         # all your Slurm jobs
botainer hpc status --all-users                   # everyone (admin/curiosity)
botainer hpc cancel <jobid>                       # scancel wrapper
botainer hpc cancel --all                         # cancel all your botainer jobs
```

The filter is based on the job-name prefix `botainer-<uuid-prefix>`
that `botainer hpc submit` sets on every sbatch call.

---

## 10. Templates: cluster-specific configs <a name="10-templates"></a>

Bundled templates live at `<clone>/cluster_profiles/`:

| File | Cluster | Status |
|---|---|---|
| `<site>.yaml` | one of ~50 bundled sites | auto-detected by hostname |
| `generic-slurm.yaml` | Anything else | Bundled; pick explicitly via `--cluster=generic-slurm` |

Project-config templates live at `<clone>/examples/`:

| File | Use case |
|---|---|
| `examples/hpc-slurm.yaml` | worked example: claude, shared mode, 4 CPU 16GB 4h |

Copy a project template to your project:
```bash
mkdir -p /scratch/$USER/my-project/.botainer
cp <botainer-checkout>/examples/hpc-slurm.yaml /scratch/$USER/my-project/.botainer/config.yaml
# Edit: set agent/account/partition/time for your needs
```

**Adding your own cluster:** copy `generic-slurm.yaml` to a new file,
edit the `hostname_patterns:` and partition list, then either submit a
PR (so others can benefit) or keep it local at
`<state_root>/cluster.yaml`.

---

## 11. Troubleshooting <a name="11-troubleshooting"></a>

### "botainer hpc submit refuses with `time=0`"

Your cluster wasn't auto-detected and you didn't pass `--time`. Pass
`--time <minutes>` or write a `cluster.yaml` (see §3).

### "`apptainer: not found` on the login node"

`module load apptainer` (or `singularity`). On many clusters it is preloaded.

### "`botainer image build` says `runtime-not-available`"

Either docker (laptop) or apptainer (HPC) must be on PATH. If you're
on HPC, the auto-detector should pick apptainer; if it doesn't, force
it: `botainer image build agent-claude --runtime apptainer`.

### "Session starts but agent shows the login prompt"

Most common causes (in order of likelihood):
1. Shared credential file doesn't exist on host. Run
   `botainer auth login --shared --agent claude` and verify
   `~/.botainer/shared-auth/agent-claude/.credentials.json` exists.
2. The session was submitted from a stale install. Run
   `botainer doctor` and check the `install.code_loaded_from` and
   `install.plugins_source` lines point at the clone you actually
   edit.

### "Two clones of the repo"

If `pip install -e` was run against a different clone path than the
one you're editing, your edits aren't live. Reset:
```bash
~/.venvs/botainer-v0_1/bin/pip install -e <correct-clone-path>
~/.venvs/botainer-v0_1/bin/python -c "import botainer; print(botainer.__file__)"
# Should print the path under <correct-clone-path>
```
v0.1.x as of `7da5438` now reads bundled plugins live from the
editable-install clone, so this only affects Python module imports.

### "Inner agent on compute node can't find credentials"

The submit flow binds the per-project state subtree, the active
agent's shared-auth subdir (RW so OAuth refresh works), and re-injects
`BOTAINER_STATE_ROOT` / `MY_BOTAINER` / `BOTAINER_PROJECT_UUID` /
`SLURM_TMPDIR` across `--cleanenv`. If credentials still don't reach
the inner agent, check:

```bash
# On the host, before submit:
ls -la $MY_BOTAINER/shared-auth/agent-claude/.credentials.json  # mode 0600
botainer doctor                                                      # all green?

# After submit, on the compute node (ssh in, or via srun --overlap):
ls -la /home/agent/.claude/                                      # should have
                                                                  # .credentials.json
                                                                  # symlinked to
                                                                  # /shared-auth/...
```

### "How do I tell what botainer is actually running?"

`botainer doctor` reports it directly under `install.code_loaded_from` and
`install.plugins_source`. For a one-liner:

```bash
~/.venvs/botainer-v0_1/bin/python -c "
import botainer.core.composition as c
print('Code:', c.__file__)
" && echo "MY_BOTAINER=${MY_BOTAINER:-(unset → ~/.botainer)}"
```

---

## Where this doc fits

- **You read this** as a researcher to get an HPC session running.
- **HPC-IMPLEMENTATION-PLAN.md** is for the developer fixing the gaps
  this doc calls out. Items 1-3, 4-9 in P0/P1 are done; pool mode (#8)
  and the P2 polish items are still pending.

Updated as gaps close: when an HPC-IMPL item lands, remove the
remaining ⛔ markers and update the relevant section here. The pool
mode (§6d) is the last ⛔ on this page.
