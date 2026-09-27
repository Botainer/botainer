# Getting started with botainer v0.1.0

The five-command flow, expanded. For install/package-manager
specifics: `botainer help install`.

## Prerequisites

| Required | Why |
|---|---|
| Python ≥ 3.10 | Run the launcher |
| Docker (Docker Desktop on Mac; native on Linux) | The agent runs in Docker; `botainer image build` runs `docker build` |
| _(optional)_ Anthropic Claude Code CLI on the host | NOT required by botainer — login runs `claude /login` INSIDE the built agent container, not on the host. Install it only if you separately use the standalone `claude` CLI. (Do build the agent image first: `botainer image build agent-claude`.) |
| ~5 GB free disk in `$HOME` | The agent image is ~3.5 GB |

`botainer setup` runs a preflight that checks each of these and tells
you what's missing.

**Install from a clone.** botainer is not on PyPI — the name is not
registered and nothing is published under it, so `pip install botainer`
will fail. A PyPI release is intended; until then, clone the repo and
`pipx install -e .`. `pipx` keeps botainer in its own venv, avoiding the
conda-env lock-in that breaks scripts later.

### macOS users: credentials storage

Anthropic's Claude Code on macOS uses the macOS **Keychain** for OAuth
tokens (recent versions). **botainer does NOT use Keychain** — your
botainer-managed Anthropic credentials live in a flat file at:

```
~/.botainer/state/<project-uuid>/data/agent-claude/profiles/default/.credentials.json
```

Mode `0600` (user read/write only). This is a deliberate v0.1.0
design choice for cross-platform consistency (Linux + HPC don't have
Keychain). Keychain backing is on the v0.1.x roadmap.

**Recommendation for Mac users:** ensure FileVault is enabled, so the
credential is encrypted at rest. Without FileVault the file is plaintext
on your SSD and anyone with disk access reads it directly.

FileVault protects a powered-off disk. It does not protect a running,
logged-in Mac: any process running as you can read a 0600 file, where a
Keychain item is ACL-gated per application. That gap is why Keychain
backing is on the roadmap, and FileVault is not a substitute for it.

## 1. Install (one-time per host)

```sh
git clone https://github.com/botainer/botainer ~/src/botainer
cd ~/src/botainer
pipx install -e .       # (or: pip install -e . into your existing venv)
```

(Editable mode from a clone is the only supported install today.)

The `botainer` console script is now on your PATH.

### v0.0.x coexistence (if applicable)

If you have an existing v0.0.x botainer install at `~/.botainer/`, you
have two options:

- **A. Move v0.0.x out of the way** before installing v0.1.0:
  `mv ~/.botainer ~/.botainer-v0.0.x-backup`
- **B. Run v0.1.0 in parallel** with v0.0.x. `MY_BOTAINER` only redirects the
  STATE dir, not the binary — so `pipx install -e .` above makes the single
  `botainer` on PATH be v0.1.0 (it would shadow v0.0.x). To keep BOTH working,
  install v0.1.0 in a separate venv and alias it, so each version has its own
  binary AND its own state dir:
  ```sh
  python3 -m venv ~/.botainer-venv-v0_1 && . ~/.botainer-venv-v0_1/bin/activate
  pip install -e .
  alias bot1='MY_BOTAINER=~/.botainer-v0_1 botainer'   # v0.1.0 → separate state
  bot1 setup            # `bot1 ...` = v0.1.0; plain `botainer` stays v0.0.x
  ```

See [DEPLOY.md](DEPLOY.md) (Scenario 2) for the full parallel-install recipe.

## 2. Setup (one-time per host)

```sh
botainer setup
```

What happens:

1. **Preflight checks** (~5 sec): container runtime reachable, ≥5 GB
   free disk, registry reachable. (A host agent CLI such as `claude` is
   reported if present but is NOT required — OAuth runs INSIDE the agent
   container, not on the host.) Any problem is reported with a
   remediation hint.
2. **State dir created** at `~/.botainer/` (or `$MY_BOTAINER`),
   mode 0700.
3. **Default policy written** at `~/.botainer/policy.yaml` (capability
   allowlists, env-var denylist, etc.).
4. **Bundled plugins discovered and installed**: agent-claude, git,
   hpc-launcher. `installed.lock` records sha256 tree hashes (the
   trust store).

`setup` does **not** build the agent image. Run that step explicitly:

```sh
botainer image build agent-claude
```

The first build is ~8-12 minutes (it pulls a base image, installs
python+conda+julia+R+claude-code, then commits the layers). Subsequent
runs are cached and fast. If you'd rather use a prebuilt image, set
`image:` in your project's `.botainer/config.yaml`.

## 3. Initialize a project (one per project)

```sh
cd /path/to/your/project
botainer init --agent claude
```

What gets written:

- `.botainer/project-id` — stable UUID (commit this to git; identifies
  the project across moves and team-shares).
- `.botainer/config.yaml` — capabilities, plugins enabled. Edit as
  needed; defaults are sensible.

And on the host:

- `~/.botainer/state/<uuid>/` — your project's state (sessions,
  credentials, package installs, scratch, hashes).

`botainer init --name <custom-name>` lets you set a display name for
the project (default is the cwd basename).

## 4. Log in to the agent

```sh
botainer auth login --agent claude
```

This is the easy path: it walks each installed auth family (Claude,
Codex, …) and runs the right login for the active mode (`isolated`,
`shared`, or `proxy`). For an explicit single-plugin invocation,
`botainer plugin agent-claude login` still works.

botainer runs `claude /login` INSIDE the built agent-claude container (the
host does NOT need the Claude CLI; it does need the image built first), with
`CLAUDE_CONFIG_DIR` pointed at either the project's per-project credential dir
(`isolated`) or the host-wide shared-auth dir (`shared`).

### Per-project vs shared vs proxy

| Mode | Credentials live at | Switch with |
|---|---|---|
| `shared` | `~/.botainer/shared-auth/agent-claude/.credentials.json` (one host, all projects — less isolated; `start` warns) | `botainer auth use shared` |
| `isolated` (**default**) | `~/.botainer/state/<uuid>/data/agent-claude/profiles/default/` (per-project) | `botainer auth use isolated` |
| `proxy` (⚠ NOT FUNCTIONAL at v0.1.0) | *designed to* keep the real key on the host with the container seeing an ephemeral token — but a proxy session **refuses to start** today (see below) | (don't — use shared/isolated) |

`botainer auth status` prints the active mode + where the credential
file lives. Mode `proxy` is **NOT FUNCTIONAL at v0.1.0**: the proxy hands
the agent an ephemeral `ANTHROPIC_API_KEY`, but botainer's credential-leak
guard refuses any env var by that name on principle, so a proxy session
cannot start on any runtime. Use `shared` or `isolated`. Proxy support requires further implementation and runtime testing.

## 5. Inspect, dry-run, then launch

```sh
botainer inspect                # see the SessionSpec the launcher would build
botainer dry-run                # see the exact docker-run argv
botainer start                  # launch
```

On first `botainer start` for a project (or after the image changes),
you'll see a **capability summary** + confirmation prompt:

```
========================================================================
Confirm session launch:
  Reason for confirmation: first launch of this project
========================================================================
Project:   /path/to/project
Session:   ce6f9169030...
Image:     botainer/agent-claude:0.1@sha256:5cdfb31...
Network:   none
Mounts:
  /path/to/project           →  /workspace                                   (rw)
  /host/anchor               →  /workspace/.botainer                         (null-bind)
  ...                        →  /workspace/.botainer/AGENT_ACCESS.txt        (ro)
  ...                        →  /workspace/.botainer/AGENT_HINTS.md          (ro)
  ~/.botainer/state/<...>/packages/  →  /packages                            (rw)
  ~/.botainer/state/<...>/scratch/   →  /scratch                             (rw)
Auth profile: default
Plugins:   agent-claude, git
========================================================================
Launch this session? [y/N]:
```

Subsequent launches print a one-line summary instead of the gate.

## Daily workflow

```sh
cd /path/to/project
botainer start
```

That's the loop.

### Background sessions

If you want to nudge the agent from another shell (e.g., to tell it
"continue" after a rate limit clears), use `--detach`:

```sh
botainer start --detach            # returns immediately
botainer status                    # list running sessions; nudge hint shown
botainer nudge "continue"          # inject input (requires nudge plugin enabled)
botainer attach                    # bring stdio back into this terminal
botainer stop                      # terminate when done
```

The `nudge` plugin is opt-in. To enable, uncomment it in
`.botainer/config.yaml`:

```yaml
plugins_enabled:
  - agent-claude
  - git
  - nudge      # wraps agent in host-side `screen`; see `botainer help nudge` for tradeoff
```

### Web ports (Jupyter, Streamlit, etc.)

If the agent runs web apps you want to open in your browser:

```yaml
# .botainer/config.yaml
plugins_enabled:
  - agent-claude
  - git
  - web-ports

plugins:
  web-ports:
    ports:
      - 8888                                    # Jupyter
      - {container: 7860, host: 7860, label: gradio}
      - {container: 8501, host: 8501, label: streamlit}
```

Then `botainer start` exposes those ports at `http://localhost:<port>`.

## What the agent sees inside the container

- **`/workspace`** (rw) — your project.
- **`/workspace/.botainer/AGENT_HINTS.md`** (ro) — launcher-generated
  doc telling the agent where to install packages (`/packages/pip`,
  `/packages/julia_depot`, etc.) and that `/scratch` is ephemeral.
- **`/workspace/.botainer/AGENT_ACCESS.txt`** (ro) — sanitized summary
  of capabilities + reminder.
- **`/packages`** (rw) — your `pip install`s, `npm install`s,
  `Pkg.add()`s. Persists across sessions. Env vars (`PIP_TARGET`, etc.)
  pre-set so installs land here automatically.
- **`/scratch`** (rw) — ephemeral. AGENT_HINTS tells the agent the
  user may delete it at any time.

Anything else is invisible to the agent. The agent is confined to the
project; the rest of your filesystem is not bound in.

## Common config edits

`.botainer/config.yaml`:

```yaml
# Allow internet access (only when you actually need it):
network:
  mode: internet
```

```yaml
# Bind an extra read-only dataset (must be in policy allowlist):
mounts:
  extra:
    - source: /Users/me/datasets/foo
      target: /data
      mode: ro
      reason: dataset for analysis
```

## Finding your project's state dir

Three ways:

```sh
botainer list                                       # all projects on this host
botainer status                                     # this project's sessions
ls ~/.botainer/state/by-name/                       # symlinks by project name
```

The `by-name/` dir contains `<projectname>-<short-uuid>` symlinks to
the canonical UUID-named state dirs.

## Cleaning up disk space

Packages and scratch can grow over months. To recover disk for one
project:

```sh
# Find the project (UUID-based, or use the by-name symlink)
botainer list

# Nuke its scratch (always safe)
rm -rf ~/.botainer/state/<uuid>/scratch/

# Nuke its packages (agent will re-install on next session)
rm -rf ~/.botainer/state/<uuid>/packages/
```

Or to wipe a project entirely:

```sh
rm -rf ~/.botainer/state/<uuid>/
```

(That doesn't delete your project files at `/path/to/project/` — just
the host-side state.)

## Troubleshooting

```sh
botainer doctor                 # diagnose docker / state / plugins / etc.
```

Doctor returns non-zero only if there's something actionable to fix.

For deeper issues, see [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) and
[`docs/CAPABILITY-SURFACE.md`](docs/CAPABILITY-SURFACE.md) for known
prototype gaps.
