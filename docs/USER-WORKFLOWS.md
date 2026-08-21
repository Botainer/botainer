# Common user workflows

Two reference flows, side by side.

## Local (laptop, Docker)

```
┌────────────────────────────────────────────────────────────────────────┐
│ One-time install                                                       │
├────────────────────────────────────────────────────────────────────────┤
│ git clone .../botainer-v0_1 ~/src/botainer-v0_1                            │
│ cd ~/src/botainer-v0_1                                                   │
│ pipx install -e .   # puts `botainer` on PATH (or: pip install -e . in venv)│
│ botainer setup                                                             │
│                                                                        │
│ docker build -t botainer/agent-claude:0.1 plugins/agent-claude/        │
│ # Record the digest into your project's config.yaml below.             │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Per-project setup (once per project)                                   │
├────────────────────────────────────────────────────────────────────────┤
│ cd /path/to/project                                                    │
│ botainer init --agent claude                                               │
│ # Edit .botainer/config.yaml: set image: name@sha256:<digest>          │
│ botainer plugin agent-claude login    # OAuth (writes to per-project state)│
│ botainer inspect                       # confirm the plan looks right      │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Daily loop                                                             │
├────────────────────────────────────────────────────────────────────────┤
│ cd /path/to/project                                                    │
│ botainer start                                                             │
│ # ... agent work ...                                                   │
│ # exit / Ctrl-D → back on host                                         │
│ git status                                                             │
└────────────────────────────────────────────────────────────────────────┘
```

## HPC (login node + Slurm + Apptainer)

```
┌────────────────────────────────────────────────────────────────────────┐
│ One-time install (on login node)                                       │
├────────────────────────────────────────────────────────────────────────┤
│ module load python apptainer    # cluster-specific                     │
│ git clone .../botainer-v0_1 ~/src/botainer-v0_1                            │
│ cd ~/src/botainer-v0_1                                                   │
│ python3 -m venv ~/.botainer-venv && . ~/.botainer-venv/bin/activate     │
│ pip install -e .            # puts `botainer` on PATH in the venv       │
│ botainer setup                                                             │
│                                                                        │
│ # Build the image (writable area; avoid $HOME if quota-tight):         │
│ cd /tmp                                                                │
│ apptainer build ~/.botainer-v0_1/images/botainer-agent-claude.sif \      │
│    ~/src/botainer-v0_1/plugins/agent-claude/agent-claude.def             │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Per-project setup (once per project)                                   │
├────────────────────────────────────────────────────────────────────────┤
│ cd ~/scratch/your-project                                              │
│ botainer init --agent claude                                               │
│ # Edit .botainer/config.yaml:                                          │
│ #   runtime: apptainer                                                 │
│ #   image: /home/<you>/.botainer-v0_1/images/botainer-agent-claude.sif   │
│ #   plugins.hpc-launcher: {partition, account, cpus, memory_gb, time}  │
│ botainer plugin agent-claude login                                         │
│ botainer inspect                                                           │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Submit the agent's Slurm job                                           │
├────────────────────────────────────────────────────────────────────────┤
│ botainer hpc submit --dry-run    # confirm the sbatch script              │
│ botainer hpc submit              # actually submit                        │
│ botainer hpc status              # squeue --user=$USER                    │
│                                                                        │
│ # Watch it, then attach when it's running:                             │
│ botainer hpc logs <jobid> -f    # tail the job output                     │
│ botainer hpc attach --jobid <jobid>                                        │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Inside an interactive allocation (alternative path)                    │
├────────────────────────────────────────────────────────────────────────┤
│ salloc --partition=day --time=4:00:00 --cpus-per-task=4               │
│ # ... allocation granted ...                                           │
│ cd ~/scratch/your-project                                              │
│ botainer plugin hpc-launcher submit --mode=here    # no sbatch; run now    │
└────────────────────────────────────────────────────────────────────────┘
```

## Mental model

```
┌───────────────────────────────────────────┐
│            YOU (USER)                     │
│            host shell                     │
└───────────────────────────────────────────┘
            │
            │ botainer command
            ▼
┌───────────────────────────────────────────┐
│         botainer launcher (HOST)          │
│ - reads config + policy                   │
│ - builds typed SessionSpec                │
│ - runs preflight host readback            │
│ - shows you everything via `inspect`      │
└───────────────────────────────────────────┘
            │
            │ docker run / apptainer exec / sbatch
            ▼
┌───────────────────────────────────────────┐
│        Container (CONTAINER)              │
│ - agent process (claude / codex / ...)    │
│ - workspace at /workspace (rw)            │
│ - AGENT_ACCESS.txt (ro)                   │
│ - network: as declared in config          │
└───────────────────────────────────────────┘
```

## Common config patterns

### Internet-enabled agent

```yaml
network:
  mode: internet
```

### Endpoint-IP-allowlist — REFUSED at v0.1.0, do not use

`network.mode: endpoint-ip-allowlist` parses, and then the session **refuses to
start**. The per-endpoint iptables enforcement it needed was retired (#176), and
botainer will not run a restriction it cannot enforce — a mode that silently
degraded to a plain bridge would be worse than no mode at all.

There is no partial win to be had here. Use one of:

```yaml
network:
  mode: none        # no route off the host at all — structural, nothing to bypass
```

```yaml
network:
  mode: internet    # agent is online; gate egress OUTSIDE botainer
                    # (host egress proxy, cluster ACLs, firewall)
```

For the credential half of the problem — keeping your API credential out of a
compromised agent's reach — see `botainer auth use broker` and
`examples/secure.yaml`. That part works on both runtimes.

### Bind an extra dataset (read-only)

```yaml
mounts:
  extra:
    - source: /home/me/datasets/foo
      target: /data
      mode: ro
      reason: dataset for analysis
```

### HPC GPU

```yaml
plugins:
  hpc-launcher:
    partition: gpu
    account: YOURPI
    cpus: 4
    memory_gb: 32
    gpus: 1
    gpu_type: a100
    time_minutes: 240
```

## What to read next

- `DEPLOY.md` — non-colliding install with v0.0.x.
- `GETTING_STARTED.md` — five-minute walkthrough.
- `GETTING_STARTED-HPC.md` — cluster onboarding, start to finish.
- `docs/CAPABILITY-SURFACE.md` — what the runtime exposes to the agent, and why.
- `IMPLEMENTATION-NOTES.md` — prototype gaps + things to discuss before
  rebuilding.
