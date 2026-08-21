"""`botainer help install` — print install / package-manager guidance.

What the user gets:
- How to install botainer itself (pip / pipx)
- What `botainer setup` will do (download/build the agent image)
- How the agent installs packages inside the container (and how that
  interacts with conda, uv, pip)
- Recommended workflow per use case

Shown automatically the first time the user runs `botainer setup`
(controlled by a flag in state dir); also available on demand via
`botainer help install`.
"""

from __future__ import annotations

import sys

import click

_INSTALL_GUIDE_TEXT = """
INSTALLING BOTAINER ON YOUR HOST
================================

botainer is NOT on PyPI. `pip install botainer` and `pipx install
botainer` will not work — the name is unclaimed and nothing is
published under it. Publishing there is the intention, not the
current state.

Install from a clone. Recommended: pipx (one isolated venv per CLI;
doesn't pollute system Python, doesn't collide with any conda env
you might be in):

    git clone <the botainer repository> ~/opt/botainer
    pipx install -e ~/opt/botainer

Or, if you don't have pipx and don't want to install it, a dedicated
venv:

    python -m venv ~/opt/botainer-venv
    ~/opt/botainer-venv/bin/pip install -e ~/opt/botainer
    ln -s ~/opt/botainer-venv/bin/botainer ~/.local/bin/

    # Then make sure `~/.local/bin` is on your PATH.

Do NOT install into a conda env's Python — the `botainer` script ends
up locked to that env and breaks when you deactivate.


WHAT `botainer setup` DOES (and DOES NOT)
=========================================

1. Preflight: checks docker (or apptainer), free disk, network.
2. Creates state dir at `~/.botainer/` (or wherever $MY_BOTAINER points).
3. Writes a default site policy at `<state-dir>/policy.yaml`.
4. Installs the bundled first-party plugins:
%%PLUGIN_LIST%%
   (`installed.lock` records sha256 tree hashes.)

It does NOT build the agent image. That's a separate step:

    botainer image build agent-claude     # Docker (Mac/Linux desktop)
    botainer hpc build agent-claude       # Apptainer (HPC)

First-time build is ~8-12 minutes (pulls a base image, installs
python+conda+julia+R+claude-code). Re-run with `--force` to rebuild.

The split exists because building takes a long time, requires
network reach to Docker Hub / Apptainer registries, and isn't
needed at all if you set `image:` in `.botainer/config.yaml` to
point at a prebuilt image (HPC users often do this).

After setup + image build, `cd` into a project and run
`botainer init`.


HOW THE AGENT INSTALLS PACKAGES INSIDE THE CONTAINER
====================================================

The agent image ships with multiple Python tools:

- **System python3** (apt-installed; debian's version, typically 3.11).
  `pip install <pkg>` → `/packages/pip/` (persists per-project).
  Best for: pure-Python libs.

- **uv** (Astral; fast, modern). Installs Python versions on demand:
  `uv python install 3.10 && uv venv` → `.venv/`.
  Best for: when you need a Python version that differs from system.

- **conda** (miniforge). Creates fully-isolated envs with system libs:
  `conda create -n myproj python=3.11 numpy pytorch -c pytorch`.
  Envs land in `/packages/conda_envs/`. Best for: scientific Python
  with native dependencies where pip-wheels are flaky (torch+CUDA,
  scipy+BLAS, etc.).

You don't need to pick one upfront. The agent picks per task; the user
can override by editing `.botainer/config.yaml`.

OTHER LANGUAGES (in the agent image, ready to use):

- **Node** + npm — installs to `/packages/node_modules/`
- **Julia** 1.10 — `Pkg.add(...)` → `/packages/julia_depot/`
- **R** — `install.packages(...)` → `/packages/R_libs/`

Rust and Go have env-var routing pre-set (CARGO_HOME, GOPATH → /packages/)
but the compilers themselves aren't in the base agent image. Install them
yourself inside the container (network mode allowing) or use a plugin
that bundles them.

ALL of these persist per-project; `rm -rf
~/.botainer/state/<uuid>/packages/` clears them. The agent re-installs
on next session.


HOW THE AGENT GETS NETWORK ACCESS FOR INSTALLS
==============================================

`botainer init` writes `network.mode: internet` by DEFAULT (frictionless;
pip/npm/git clone work without extra setup), so a freshly-init'd project
does NOT block these. The hardened opt-in is `network.mode: none`, which
refuses outbound — use it when you want the agent locked down and you'll
pre-install everything on the host yourself.

Two configurations:

A. Default — internet (recommended for daily dev):

    # .botainer/config.yaml (already the default)
    network:
      mode: internet

   The agent can pip/npm/curl freely; convenient but it could exfil
   project data via HTTP POST. Fine for day-to-day; switch to (B) for
   sensitive work.

B. Opt-in lockdown — none, pre-install on host:

    # .botainer/config.yaml — change after init
    network:
      mode: none

    # On host (before botainer start)
    pip install --target ~/.botainer/state/<uuid>/packages/pip pkg-you-need

    # Then `botainer start` and the agent finds it under /packages/pip.

Tradeoff: (A) is convenient but the agent reaches the internet; (B) is
locked down but every dependency must be pre-installed.

Apptainer caveat: `network.mode: none` is NOT enforceable under apptainer
(it shares the host network namespace), so the launcher REFUSES it on the
apptainer path rather than running fail-open — gate at the cluster
firewall or use the sbatch flow.


RUNNING MULTIPLE PROJECTS (the daily flow)
==========================================

Per project:

    cd /path/to/project
    botainer init --agent claude       # one time per project
    botainer plugin agent-claude login # one time per project
    botainer start                     # daily

Or for background-able sessions you nudge from another shell:

    botainer start --detach            # returns immediately
    botainer status                    # see what's running
    botainer nudge "continue"          # if you enabled the nudge plugin
    botainer attach                    # bring stdio back
    botainer stop                      # terminate

State for different projects is isolated:
`~/.botainer/state/<uuid>/`. Find your projects easily under
`~/.botainer/state/by-name/<projectname>-<short-uuid>/` (symlink).


TROUBLESHOOTING
===============

    botainer doctor                    # diagnose everything
    botainer doctor --json             # machine-readable

`botainer doctor` returns nonzero only if there's something
actionable. Otherwise everything is fine.

For deep issues, run `botainer doctor` — it names what is wrong and how to fix it.
"""


@click.group("help")
def help_group() -> None:
    """Topic-based help. Run `botainer help install` for the install guide."""


def _guide_text() -> str:
    """The install guide with the bundled-plugin list filled in from the single
    source of truth (BUILTIN_PLUGIN_NAMES) — so adding a plugin (e.g. the codex
    broker) can never leave this help stale."""
    from textwrap import fill

    from botainer.plugins.builtin import BUILTIN_PLUGIN_NAMES
    listing = fill(", ".join(BUILTIN_PLUGIN_NAMES) + ".",
                   width=68, initial_indent="   ", subsequent_indent="   ",
                   break_on_hyphens=False, break_long_words=False)
    return _INSTALL_GUIDE_TEXT.replace("%%PLUGIN_LIST%%", listing)


@help_group.command("install")
@click.option(
    "--plain",
    is_flag=True,
    help="Suppress color/decoration (good for piping to a file).",
)
def help_install(plain: bool) -> None:
    """Print the install + package-manager guide."""
    if plain or not sys.stdout.isatty():
        click.echo(_guide_text().strip())
        return
    # Colorize section headers + key commands.
    for line in _guide_text().strip().split("\n"):
        if line.startswith("====") or (line.isupper() and len(line.strip()) > 3 and "=" not in line):
            click.secho(line, fg="cyan", bold=True)
        elif line.startswith("    "):
            click.secho(line, fg="green")
        elif line.startswith("- **"):
            click.secho(line, fg="yellow")
        else:
            click.echo(line)


@help_group.command("nudge")
def help_nudge() -> None:
    """Print the nudge feature guide."""
    text = """
NUDGE: INJECTING INPUT INTO A RUNNING AGENT
===========================================

When to use it:
  - Agent hits a rate limit and is idle at its prompt; you want to
    send "continue" when the limit clears.
  - You're at lunch and want to give the agent a redirect mid-task.
  - You want to schedule a prompt for later via at(1).

Setup (per project):
    # .botainer/config.yaml
    plugins_enabled:
      - agent-claude
      - git
      - nudge        # uncomment to enable

Note: nudge wraps the agent in a host-side `screen` session (§A19).
Copy/paste behavior in your terminal changes (mouse selects within
the screen window; use shift+mouse for native copy, or screen's
default copy-mode binding Ctrl-A [).

Usage:

    # In one shell:
    botainer start --detach

    # In another shell, same project:
    botainer nudge "continue from where you left off"
    botainer nudge --keys C-c                  # send Ctrl-C
    botainer nudge --in 30m "rate limit should be clear now"

HPC (Apptainer + Slurm):
    Same nudge command. The CLI uses `srun --overlap --jobid <jid>
    --pty` to reach into the running Slurm step on the compute node.
"""
    click.echo(text.strip())
