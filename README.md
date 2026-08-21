# botainer

botainer runs AI coding agents (Claude Code, Codex, …) inside containers, so
they can work as freely as possible while keeping your system tidy. The design
is inspired by a security model that assumes the code inside the container is
hostile — but **botainer is not a complete security product**.

**Two first-class targets:** laptop (Docker on Mac/Linux) and HPC
(Apptainer + Slurm + Lmod). The same launcher, the same plugin model,
the same UX in both worlds. Routes below.

**Status:** `0.1.0a4` — the first public release, and an alpha. Parts are
stubbed, parts are documented as not working.
[`CHANGELOG.md`](CHANGELOG.md) lists what works and what does not;
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) and
[`docs/CAPABILITY-SURFACE.md`](docs/CAPABILITY-SURFACE.md) go into detail.

Design documentation lives in botainer's development repository and is not
part of a release yet. What ships:
[`docs/CAPABILITY-SURFACE.md`](docs/CAPABILITY-SURFACE.md)
for the security contract, [`docs/USER-WORKFLOWS.md`](docs/USER-WORKFLOWS.md)
for common patterns, [`docs/STORAGE.md`](docs/STORAGE.md) for where botainer
puts things (read §2 before running on a cluster — moving `/scratch` off your
home quota is much easier to do at the start than later), and the
getting-started guides below.

**Found a way the cage does not hold?** [`SECURITY.md`](SECURITY.md) says how to
report it privately, what counts, and which trade-offs are known and deliberate.
[`CHANGELOG.md`](CHANGELOG.md) is what changed.

## Get started

- **On a laptop or workstation (Docker)** →
  [GETTING_STARTED.md](GETTING_STARTED.md)
- **On an HPC cluster (Apptainer + Slurm)** →
  [GETTING_STARTED-HPC.md](GETTING_STARTED-HPC.md)

If you're an HPC admin or sysadmin evaluating: read
[docs/SITE-ADMIN.md](docs/SITE-ADMIN.md) for the root-owned site policy
(the trust anchor) and the hpc-modules ON-switch; the cluster-profile
system is designed to make per-user setup zero-config once you've
bundled a profile (not tested yet).

## Quick view (laptop)

```sh
# botainer is not on PyPI. Install from a clone:
git clone https://github.com/botainer/botainer ~/src/botainer
cd ~/src/botainer
pipx install -e .               # (or: pip install -e .)

botainer setup                  # one-time per host; installs plugins + writes your user policy
botainer image build agent-claude   # build the agent image (~8-12 min first time)
botainer auth login --agent claude  # OAuth flow; covers all projects on this host
cd /path/to/your/project
botainer init --agent claude
botainer inspect                # (optional) see the composed plan (dry-run --include-hooks for the full post-hook set)
botainer start                  # actually launch
```

~12 minutes the first time (mostly the image build). After that,
daily use is just `botainer start` in the project folder.

A PyPI release is intended.

> **Note**: `botainer setup` installs plugins and writes your per-user
> policy (`~/.botainer/policy.yaml`) — NOT the root-owned site policy
> (`/etc/botainer/policy.yaml`, admin-only; see docs/SITE-ADMIN.md).

## Quick view (HPC)

```sh
botainer setup                          # one-time per host
botainer hpc setup                      # autodetect the cluster, write cluster.yaml
#   …or name one:  botainer hpc setup --profile generic-slurm
botainer hpc build agent-claude         # apptainer build (or copy .sif over)
botainer auth login --shared --agent claude  # needs the .sif (built above); scp the file to the cluster
cd /path/to/your/project
botainer init --agent claude --runtime apptainer
botainer hpc submit --time 120 --cpus 4 --memory-gb 16 --partition day --account <acct> --dry-run
botainer hpc submit --time 120 --cpus 4 --memory-gb 16 --partition day --account <acct>
```

Flag aliases accepted: `--time 02:00:00` and `--time 2h` work the
same as `--time 120`; `--mem 16G` accepted as `--memory-gb 16`;
`--gres gpu:a100:2` accepted as `--gpus 2 --gpu-type a100`;
`--cpus-per-task` accepted as `--cpus`. All normalize to the typed
form internally.

`botainer hpc status` / `botainer hpc stop` for management. Full
walkthrough: [GETTING_STARTED-HPC.md](GETTING_STARTED-HPC.md).

## Concepts and terms

Words this README uses in a specific way. (Section name: "Concepts and terms"
rather than "Terms" — "Terms" reads like terms-of-service.)

**Project** — a directory with a `.botainer/config.yaml`. `botainer init`
creates it. Everything else is scoped to a project.

**Session** — one run of an agent in a container. `botainer start` begins one.

**Image** — the container image the agent runs in. Built once per host
(`botainer image build <agent>`), shared by every project.

**Auth mode** — *where your login credential is kept*, and therefore whether
the agent can read it. Three of them:

| mode | credential lives | can the agent read it? |
|---|---|---|
| `shared` | one host-wide file, shared by all your projects | **yes** |
| `isolated` | a copy per project | **yes** |
| `broker` | host-side only; the container gets a fake stand-in | **no** |

`shared` is the default. `shared` and `isolated` both put the real file
*inside* the container — that is what "mount" means when you see it in older
notes. `broker` does not, which is why it is the one to pick if this matters
to you.

**Auth profile** — a named credential slot (`default`, `work`, …), so one
machine can hold logins for more than one account. See the caveats under
Credentials.

**Cluster profile** — unrelated to the above, unfortunately: a YAML file
describing one HPC site (partitions, accounts, scratch path). ~50 ship.
`botainer hpc setup` picks one. The flag for both is `--profile`, which is a
naming mistake we intend to fix.

**Plugin** — how botainer's own features are built: agent wiring, the HPC
launcher, the git guard, the browser. **All first-party for now** — there is no
third-party plugin install — and each can be switched on or off per project via
`plugins_enabled` in `.botainer/config.yaml`. They run as your user, with your
privileges, like the launcher itself. botainer does not verify plugin file
integrity at v0.1.0; signed installs are v0.2 work.

**State root** — `~/.botainer` by default (`$MY_BOTAINER` overrides), holding
per-project state, credentials and images.

**`/workspace`, `/packages`, `/scratch`** — the three paths inside the
container: your project (read-write), installed packages that persist between
sessions, and disposable working space. See `docs/STORAGE.md`.

## What botainer protects, and what it doesn't

It narrows agent reach **outside** the project. Concretely, and with the
limits stated:

- **Filesystem** — the agent sees only what the mount plan declares. Every
  bind is listed in the pre-launch capability summary.
- **Credentials** — in `broker` mode the real credential stays host-side and
  the container gets a provably-fake sentinel. In `shared` / `isolated`
  (MOUNT) modes the agent **can read the credential file**; the summary says
  so at launch.
- **Network** — `network.mode` is an explicit grant, but note `botainer init`
  writes **`internet`**, which means *unrestricted IP egress* — including your
  LAN and any service on the host or cluster network. `none` is available and
  is the tighter setting.
- **Scheduler (HPC)** — dispatched jobs are caged and bounded by
  `max_concurrent`, and a site policy can cap partition / account / GPUs. There
  is **no** rate limit, total-job budget, or spend cap at v0.1: an agent can
  consume your allocation and your subscription quota up to those ceilings.

For what is *not* covered at all — co-tenants on a shared node, outbound harm
to third parties, resource exhaustion, and post-session persistence in the
checkout — see `docs/CAPABILITY-SURFACE.md`.

It does **not** protect the project checkout itself from the agent:
`/workspace` is always rw, and the agent can rewrite tests, install
npm packages, modify git hooks, or poison files the user later runs
on the host. (The optional `git` plugin's protected mode mounts
`.git/` read-only, which blocks the hook path specifically.) After an agent
session, **treat the project checkout as
untrusted input** for sensitive operations. This is the design's
hardest line.

### Credentials

botainer signs you in with your normal Claude or Codex **subscription login**
(OAuth) — `botainer auth login`. Where that credential is then kept is the
"auth mode" above, and it is the only choice that affects whether the agent can
read it:

- **`broker`** — the real token never enters the container. Pick this if you
  care.
- **`shared`** (default) — one login, reused by every project. The credential
  file is mounted into the container, so the agent can read **and overwrite**
  it. **Run one shared-mode session at a time.** The token rotates when it
  refreshes, and a second live session invalidates the first one's copy, so you
  end up logged out somewhere. botainer detects a second concurrent session and
  refuses rather than letting it happen.
- **`isolated`** — a separate login per project. Same mount, same agent access,
  but nothing is shared between projects, so concurrent sessions are fine. The
  cost is logging in once per project.

The capability summary says which mode is in force at every launch.

The credential file lives under your state root — for example
`~/.botainer/state/<uuid>/data/agent-claude/profiles/default/.credentials.json`,
mode 0600. That is one example: the exact path depends on the mode, the agent
and the profile. `botainer auth status` prints the real one.

> **API keys and the credential proxy do not work at v0.1.0.** Subscription
> login is the only supported route. Code exists for both, and both are future
> work.

**macOS:** recent Claude Code stores tokens in the Keychain. **botainer does
not** — it uses a flat 0600 file, deliberately, so Linux and HPC behave the
same way. Keychain backing is on the roadmap. Until then, keep FileVault on.

For what is actually enforced, rather than asserted, read
[`docs/CAPABILITY-SURFACE.md`](docs/CAPABILITY-SURFACE.md).

## What is tested, and what is not

"the tests pass" is easy to misread. Three layers, in very different states:

1. **Automated suite (~2,400, green).** Verifies what botainer *decides* — binds,
   env, argv, refusals, plus a hostile suite. Computed without launching a
   container, so it runs anywhere; only **three** tests touch a real
   Docker/Apptainer/Slurm. It does not test what a container then *does*.
2. **A host-side script suite** in the development repository (it needs a real
   Docker/Slurm host). **It predates the broker, the job dispatcher and the
   relocatable storage roots.** Treat it as stale.
3. **Actual use.** One maintainer, daily on a Mac, repeatedly on one Slurm
   cluster. Sample of one.

| | evidence |
|---|---|
| Laptop / Docker / Claude Code, `shared` + `broker` | layer 3, daily |
| HPC: Apptainer + Slurm submit | layer 3, one cluster |
| HPC job dispatcher, warm pool, MPI | layer 3, lightly |
| Codex, `shared` | layers 1-2 |
| Codex, `broker` | **never successfully run, anywhere** |
| Browser viewer | laptop only; carries a trust inversion (see its docs) |
| **Site-admin path** (`/etc/botainer/policy.yaml`) | **layer 1 only — never run on a real multi-user machine.** A sysadmin deploying this is the first. |
| Native-Linux Docker (vs Mac) | thin |
| Windows | not supported, not attempted |

## Agent support

| Agent | `shared` / `isolated` | `broker` |
|---|---|---|
| Anthropic Claude Code | works | **works** — this is the one that is used daily |
| OpenAI Codex CLI | works | implemented, but **has never successfully run**, on any platform |

Those are the only two. There is no plugin for any other agent — not written,
not stubbed. Adding one is not structurally hard, and Google's Gemini CLI is
the obvious candidate, but nothing is in progress and nothing is promised.

## What you get out of the box

First-party plugins (all bundled):

The agent plugins come as a family per agent — one plugin per auth mode.
Pick with `botainer auth use <mode>`; you enable exactly one of each family.

| Plugin | Purpose |
|---|---|
| `agent-claude` | Anthropic Claude Code — **isolated** mode: per-project credentials, bound into the container |
| `agent-claude-shared` | Claude Code — **shared** mode: one host-side login reused across projects |
| `agent-claude-broker` | Claude Code — **broker** mode: real credential stays on the host, agent gets a fake sentinel. The strongest of the three; see `botainer auth use broker` |
| `agent-claude-proxy` | Claude Code — **proxy** mode. **Not functional at v0.1.0** (see the warning above); kept so the refusal is explicit rather than a missing plugin |
| `agent-codex` | OpenAI Codex CLI — isolated mode (per-project API key) |
| `agent-codex-shared` | Codex CLI — shared mode |
| `agent-codex-broker` | Codex CLI — broker mode. **Not working at v0.1.0**: its hooks shipped non-executable, so it has never run on any platform; the first real attempt still failed. Use `agent-codex-shared` (verified on macOS and HPC) |
| `browser` | Chromium in the container for agentic browsing, plus an opt-in watchable viewer. **Read `docs/BROWSER.md` before enabling** — connecting a viewer to a server the container controls is a trust inversion |
| `git` | Protected git mode (filtered config, disposable .git/config) |
| `nudge` | Inject text into the agent's prompt from another shell (opt-in; uses host-side screen) |
| `web-ports` | Forward Jupyter/Gradio/Streamlit etc. to host (opt-in) |
| `hpc-launcher` | Apptainer + Slurm submission |
| `hpc-modules` | Bind login-node modules into container (Lmod-aware) |
| `wolfram-sidecar` | Host-side wolframscript proxy via unix socket (Mac; Linux opt-in). **Not working at v0.1.0.** |

`botainer plugin list` is the authoritative answer for your install.

Per project, state lives at:

- `$MY_BOTAINER/state/<uuid>/` — project state (sessions, hashes, plugin data, credentials)
- `$MY_BOTAINER/state/<uuid>/packages/` — your `pip install`s, `Pkg.add()`s, etc. (persists across sessions)
- `$MY_BOTAINER/state/<uuid>/scratch/` — ephemeral
- `$MY_BOTAINER/state/by-name/<projectname>-<short-uuid>/` — discoverability symlinks

The agent inside the container sees:

- `/workspace` (rw) — your project
- `/packages` (rw) — package install dirs, env-routed automatically
- `/scratch` (rw) — ephemeral
- `/workspace/.botainer/AGENT_HINTS.md` (ro) — env documentation (where to install, what's ephemeral, time remaining on HPC, etc.)
- `/workspace/.botainer/AGENT_ACCESS.txt` (ro) — what the agent is told about its own confinement

## Layout

```
botainer/         core launcher (Python package)
  cli/              subcommands (start, init, doctor, hpc, plugin, ...)
  core/             SessionSpec composition, policy, refusal
  adapters/         docker + apptainer adapters; render argv
  mount_plan/       typed bind objects + validators
  plugins/          install / manifest / trust verification
  state/            session records, cluster profiles, liveness
  inspect/          inspect / dry-run / preflight rendering
plugins/          first-party plugins (the 14 listed above)
cluster_profiles/ per-cluster YAML profiles (generic-slurm + example)
docs/             user docs, plugin authoring
licenses/         third-party license texts (see THIRD-PARTY-LICENSES.md)
tests/            test suite (unit + integration + hostile)
tools/pkg/        install + distribution scripts
```

The development repository additionally carries design notes, prior planning
notes and the dev-tooling gates. Those are working material, not product, and
are excluded from every release path — so a release contains exactly the tree
above.

## Status & roadmap

- **v0.1.0 (this prototype):** Docker + macOS + per-project credentials;
  Apptainer + generic-slurm path; agent-claude + agent-codex shipped.
- **v0.1.x:** generic `credential-proxy` plugin (Anthropic + OpenAI + Gemini in one bundle); agent-gemini plugin; cluster-profile bundles for additional sites; `botainer hpc pool` for agent introspection on shared interactive nodes.
## Useful commands

```sh
botainer help install           # full install + package-manager guide
botainer help nudge             # nudge feature deep-dive
botainer doctor                 # diagnose any issue
botainer doctor --strict        # warn about anything off (HPC-friendly)
botainer plugin list            # what's installed, what's bundled
botainer plugin info <name>     # full manifest for one plugin
botainer hpc info               # show active cluster profile (HPC)
botainer list                   # all projects with state on this host
```

## Reading the source: `#NNN` and `DN-###`

Comments and docs cite two kinds of identifier you cannot look up in the public
repo, and it is better to say so than let you go hunting:

- **`#160`, `#54`, `#68` …** — items in botainer's **internal issue tracker**.
  These are *not* issue numbers in this repository, and following them here will
  land you somewhere unrelated.
- **`DN-002` …** — internal design notes.

Both are **provenance, never a dependency**. The rule they are written under is
that the substance must be stated where the reference appears: if a comment says
*why* a check exists, that reasoning is in the comment, and the id only records
where the longer discussion happened. Someone changing that code has everything
they need without the private half. If you ever find a reference that is
load-bearing — where the code cannot be understood without the thing you cannot
read — that is a bug in the comment; please report it.

The same applies to the occasional quoted path into the development
repository's working notes: those directories are not distributed, deliberately.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE), with the copyright notice in
[`NOTICE`](NOTICE).

A botainer *distribution* is not purely Apache-2.0: it bundles third-party
code under other licences. [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md)
says what, and why.
