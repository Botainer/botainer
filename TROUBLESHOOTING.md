# Troubleshooting

The fastest path to "what's wrong":

```sh
botainer doctor             # everything-is-fine returns 0; problems surface here
botainer doctor --json      # for IDE / CI parsing
botainer doctor --auth-only # credential-surface only
botainer status             # what sessions are running, if any
botainer config check       # validate the project's config.yaml
```

If those don't reveal the issue, the table below covers the common ones.

If what you hit is not a fault but a limitation botainer already knows about,
it is likely in [`docs/ROUGH-EDGES.md`](docs/ROUGH-EDGES.md) — case-insensitive
filenames on macOS, codex's databases on some network filesystems, what a
badly-killed session leaves behind. That file says plainly where there is no fix
yet.

## Install / setup

| Symptom | Likely cause | Fix |
|---|---|---|
| `pip install botainer` says "No matching distribution found" | **botainer is not on PyPI.** Install from a clone: `git clone https://github.com/botainer/botainer && cd botainer && pipx install -e .` — see README "Quick view". |
| `pip install -e .` finishes but `botainer --version` says command not found | pip installed into a venv or user dir that is not on PATH | Use `pipx install -e .` instead, or add `~/.local/bin` to PATH. |
| `botainer setup` warns `runtime.docker not found` and ends | Docker not installed | Install Docker Desktop (Mac/Windows) or `apt install docker.io` (Linux). Then re-run setup. |
| Setup completes but `botainer start` says image not recorded | The agent image hasn't been built yet | Run `botainer image build agent-claude` (or `--all`). First build takes 8-12 min. |
| `botainer plugin agent-claude login` does nothing visible | Browser flow opened in background | Look for an Anthropic login URL in your default browser. Allow popup; complete login. |

## Running sessions

| Symptom | Likely cause | Fix |
|---|---|---|
| `botainer start` says `not inside a botainer project` | `.botainer/project-id` missing | Run `botainer init --agent claude` first, or `cd` into a project that has it. |
| `botainer start` says `no credentials at .../profiles/default/.credentials.json` | Haven't logged in | `botainer plugin agent-claude login` |
| Agent in container can't `pip install` / `npm install` / `curl` | The project's `.botainer/config.yaml` has `network.mode: none` (note: `botainer init` writes `internet` by DEFAULT, so a freshly-init'd project doesn't block this) — OR the cluster blocks outbound egress from compute nodes | Edit `.botainer/config.yaml`: `network.mode: internet`. On HPC, outbound may still be firewalled (apptainer shares the host network and can't change that); pre-install packages on the host with `pip install --target ~/.botainer/state/<uuid>/packages/pip <pkg>`. |
| `botainer status` shows nothing while a session is clearly running | Session was started without `--detach`; status only sees recorded runtime handles | For background-able sessions use `botainer start --detach`. |
| `botainer nudge "..."` says no session found | nudge plugin not enabled OR no `--detach` session running | Check `.botainer/config.yaml` enables `nudge`; run `botainer start --detach`. |
| Container exits immediately, no clear error | `screen` missing on the host (nudge enabled) | Install `screen` on the host (`apt install screen` / `brew install screen`) or remove `nudge` from `plugins_enabled`. The screen wrap runs OUTSIDE the container; nothing needs to be in the agent image. |

## Auth / credentials

| Symptom | Likely cause | Fix |
|---|---|---|
| `botainer config check` flags credential-shaped env var | Your `.botainer/config.yaml` `env:` has e.g. `ANTHROPIC_API_KEY` | Remove it. Use `botainer plugin agent-claude login` for per-project credentials. |
| Agent in container can read my real API key | You're in a mount-based auth mode — `isolated` (the default) or `shared`. Both put the credential FILE inside the cage, so anything the agent runs can read it. `botainer auth status` prints which mode this project uses. | Enable the `agent-claude-broker` plugin — broker mode keeps the real key on the host and hands the container only a sentinel. (The old `proxy` plugin is retired; use the broker.) See `docs/CAPABILITY-SURFACE.md` for what each mode does and does not keep out of the container. |
| `botainer doctor --auth-only` flags shell-env credentials | Your shell has `ANTHROPIC_API_KEY` exported | If intentional, fine — they won't enter the container unless you explicitly add them to `.botainer/config.yaml`. |
| Broker session fails with `invalid_grant` / "Refresh token not found or invalid" (worked yesterday, dead today) | You ran a **`broker` session and a `shared` session on the same account at the same time**. OAuth refresh tokens rotate on use. Botainer serializes its *own* refreshes with a lock, but in `shared` mode the refresher is Claude Code **inside the container**, which rotates the token without taking that lock — stranding the other session. (Separately: `shared` and `isolated` keep genuinely different credential files, so one account used in both modes can't re-converge.) | Don't run `broker` and `shared` on one account concurrently — pick **one auth mode per account**, or use a **separate account/profile per concurrent project** (`botainer auth login --profile <name>`). Re-auth the stranded one with `botainer auth login`. |
| Agent inside the cage reports "can't connect to api" mid-session (started fine) | The host-side broker daemon likely **died after launch** — often the same token-rotation cause as the row above, hitting a periodic refresh. Start-time failures are caught and refused up front; a *mid-session* death is not yet surfaced in-cage. | Check `~/.botainer/.../sessions/<id>/agent-<agent>-broker-daemon.err` for the reason (the filename names the broker that failed; `botainer status` prints the exact path), then `botainer auth login` and restart the session. (Surfacing this failure live is a tracked follow-up.) |

## HPC (Slurm + Apptainer)

| Symptom | Likely cause | Fix |
|---|---|---|
| `module load` env not visible in container | hpc-modules plugin not enabled | Enable it in `.botainer/config.yaml`: `plugins_enabled: [..., hpc-modules]` and list modules in `plugins.hpc-modules.modules`. |
| module tools present but NOT found by name (binaries exist at `/apps/...` but `python`/etc. don't resolve) | The software-root binds are OFF: the **admin** hasn't set `mounts.cluster_software_roots` in the root-owned `/etc/botainer/policy.yaml`. botainer warns at launch when this happens. | Ask the cluster admin to add the software root(s) — see [`docs/SITE-ADMIN.md`](docs/SITE-ADMIN.md). A user's own policy is ignored for this field. Meanwhile, invoke tools by absolute path (`/apps/python/3.11/bin/python`). |
| `module: command not found` INSIDE the container (you wanted to `module load` yourself / in a batch job) | The in-container `module load` feature (`caps.modules_inner_load`) is OFF: the **admin** hasn't set `mounts.cluster_lmod_root` + `cluster_modulepath_roots` in the root-owned `/etc/botainer/policy.yaml`. | Check status with `botainer hpc setup --probe` or `botainer doctor`. Ask the admin to enable it — see [`docs/SITE-ADMIN.md`](docs/SITE-ADMIN.md) §4k. This is a distinct switch from `cluster_software_roots` above. |
| `botainer nudge` from login node fails with `socket not found` | nudge uses **screen** (not tmux); the screen socket is node-local (under the compute node's `$XDG_RUNTIME_DIR`/`$SLURM_TMPDIR`) and isn't visible from the login node | Run nudge from the same node, or use `srun --overlap --jobid=<jid>` first. (The launcher does this automatically when it can detect the jobid.) |
| `--detach` says not supported on Apptainer | Apptainer doesn't have docker -d equivalent | Use `sbatch` directly for batched submission; the `hpc-launcher` plugin handles this in the foreground path. |

## Plugin issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Plugin enabled in config but doesn't appear in `botainer status`'s plugin list | Plugin not installed | Run `botainer setup` (re-installs bundled) or `botainer plugin add <source>`. |
| `botainer plugin enable foo` says `config-missing` | Not in a project | `cd` into a project, or run `botainer init` first. |
| Plugin trust verification warns `user-modified` | You edited a bundled plugin tree | If intentional: regenerate the trust lock by re-running `botainer setup`. If not: re-clone the repo or `git restore`. |

## Disk space

| Symptom | Likely cause | Fix |
|---|---|---|
| `botainer image build` runs out of disk | Docker images are big (~3.5 GB each) | `docker system prune -a` to reclaim. Also see `~/.botainer/state/<uuid>/packages/` which can grow per project. |
| `~/.botainer/state/<uuid>/packages/` is huge | Months of `pip install` etc. accumulating | Safe to `rm -rf` — agent will re-install on next session. Or use `botainer packages clean <uuid>` (TODO; manual `rm -rf` works today). |
| `~/.botainer/state/<uuid>/scratch/` is huge | Agent dumped data there | Safe to `rm -rf` (designed to be ephemeral). |

## Architecture / state confusion

| Symptom | Likely cause | Fix |
|---|---|---|
| Two `~/.botainer/` dirs, different state | You have v0.0.x and v0.1.0 installed | The v0.1.0 honor `$MY_BOTAINER` env var if set; see `DEPLOY.md` § "parallel with v0.0.x". |
| Can't find which `state/<uuid>/` belongs to a project | UUID is opaque | `ls ~/.botainer/state/by-name/` — symlinks named `<project>-<short-uuid>`. Or `botainer list`. |
| Renamed a project; state still under old name | by-name symlinks aren't garbage-collected | Old symlinks are harmless; new symlink will be created on next `botainer start`. To clean up: `rm ~/.botainer/state/by-name/old-name-*`. |

## When all else fails

```sh
botainer inspect              # see exactly what would be composed
botainer dry-run              # see the exact docker/apptainer command
botainer doctor --json        # machine-readable findings
```

Open an issue with: output of `botainer doctor --json`, `botainer
inspect --json` (sanitized — it includes your project path), the
exact command you ran, and the error you got.
