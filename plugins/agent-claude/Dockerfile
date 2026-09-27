# Dockerfile for agent-claude (botainer v0.1.0)
# Built on the host (no Docker daemon inside the dev container).
#
# Kitchen-sink image: enough languages and tools that most users won't
# need to customize. Per Phase 1 of PLAN-V0.1.0-FINAL.md.
#
# Contents:
#   - node:24 LTS (for Claude CLI)
#   - Anthropic Claude Code CLI (pinned version)
#   - python3 + pip + venv + build-essential
#   - R + r-base-dev
#   - conda (miniforge)
#   - julia (1.10)
#   - Common CLI tools: git, curl, ripgrep, less, jq, ca-certificates
#
# Image size: ~3.5 GB.
# Build time: ~8-12 min first time; layer-cached after.
#
# Pinned versions: bumping requires editing this file then `botainer image build --no-cache`.
# Phase 3 will move version pins to the plugin manifest (image_recipe / features).

FROM node:24-bookworm-slim AS base

# System packages: kept minimal but sufficient for Python/R/conda/julia ecosystems.
# openmpi-bin + libopenmpi-dev: a PMIx-capable MPI so dispatched `mpi: pmix` HPC
# jobs run multi-node out of the box (PMIx cross-version compatible; no match to
# the cluster needed). Mirrors agent-claude.def; unused on the docker/laptop path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg2 git \
        build-essential pkg-config \
        python3 python3-pip python3-venv python3-dev \
        ripgrep less jq vim-tiny \
        libcurl4-openssl-dev libssl-dev libxml2-dev \
        openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# uv (Astral) — fast Python tool. Lighter alternative to conda for the
# common Python case. Available alongside conda + system pip; user picks.
# Per CONDA-AND-LANGUAGES-DESIGN.md.
RUN curl -fsSL https://astral.sh/uv/install.sh | \
        env UV_INSTALL_DIR=/usr/local/bin sh \
    && /usr/local/bin/uv --version

# R: from Debian repos (older but stable; v0.1.x can pin to CRAN snapshots).
RUN apt-get update && apt-get install -y --no-install-recommends \
        r-base r-base-dev \
    && rm -rf /var/lib/apt/lists/*

# Architecture detection. Apple Silicon Macs (M-series) running
# Docker Desktop normally use the Rosetta x86_64 emulator when the
# image is x86_64, which breaks conda/miniforge installers (the
# self-extracting payload exits with "rosetta error: failed to open
# elf at /lib64/ld-linux-x86-64.so.2"). Build a NATIVE arm64 layer
# instead by using `dpkg --print-architecture` to pick the right
# upstream installer. The TARGETARCH BuildKit ARG gives the same
# answer for buildx/multi-platform builds; both are kept so the
# Dockerfile works under classic builder + buildx alike.
ARG TARGETARCH
RUN echo "build arch: $(dpkg --print-architecture) (TARGETARCH=${TARGETARCH:-unset})"

# Conda (miniforge): community-maintained, conda-forge default channel.
ARG MINIFORGE_VERSION=24.3.0-0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64)  mf_arch=x86_64 ;; \
        arm64)  mf_arch=aarch64 ;; \
        *)      echo "unsupported arch: $arch" >&2; exit 2 ;; \
    esac; \
    url="https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-${MINIFORGE_VERSION}-Linux-${mf_arch}.sh"; \
    echo "downloading $url"; \
    curl -fsSL "$url" -o /tmp/miniforge.sh; \
    bash /tmp/miniforge.sh -b -p /opt/conda; \
    rm /tmp/miniforge.sh; \
    /opt/conda/bin/conda clean --all -y
ENV PATH=/opt/conda/bin:$PATH

# Julia: official binary release; pinned major.minor in the recipe.
# Julia's URL structure uses x64/aarch64 paths; map dpkg arch to that.
ARG JULIA_VERSION=1.10.4
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64)  jl_dir=x64;     jl_arch=x86_64 ;; \
        arm64)  jl_dir=aarch64; jl_arch=aarch64 ;; \
        *)      echo "unsupported arch: $arch" >&2; exit 2 ;; \
    esac; \
    jl_minor="$(echo ${JULIA_VERSION} | cut -d. -f1-2)"; \
    url="https://julialang-s3.julialang.org/bin/linux/${jl_dir}/${jl_minor}/julia-${JULIA_VERSION}-linux-${jl_arch}.tar.gz"; \
    echo "downloading $url"; \
    curl -fsSL "$url" -o /tmp/julia.tar.gz; \
    tar -xzf /tmp/julia.tar.gz -C /opt; \
    ln -s /opt/julia-${JULIA_VERSION}/bin/julia /usr/local/bin/julia; \
    rm /tmp/julia.tar.gz

# Create the non-root agent user. UID 1001 to avoid colliding with node:24's
# pre-existing `node` user at 1000.
ARG AGENT_UID=1001
ARG AGENT_GID=1001
RUN if getent group ${AGENT_GID} >/dev/null; then \
        groupmod -n agent $(getent group ${AGENT_GID} | cut -d: -f1); \
    else \
        groupadd --system --gid ${AGENT_GID} agent; \
    fi \
    && if getent passwd ${AGENT_UID} >/dev/null; then \
        usermod -l agent -d /home/agent -m -g agent -s /bin/bash $(getent passwd ${AGENT_UID} | cut -d: -f1); \
    else \
        useradd --system --uid ${AGENT_UID} --gid ${AGENT_GID} --create-home --shell /bin/bash agent; \
    fi

# Claude Code CLI.
#
# Default is `latest`: Anthropic ships claude-code multiple times per
# week and pinning means new fresh builds inherit a stale CLI. We had
# CLAUDE_CODE_VERSION=2.0.39 pinned and it got unpublished by
# 2026-05-18, breaking every fresh build with
# `npm error notarget No matching version found`. Lesson learned.
#
# When you DO need reproducibility (debugging cross-host issues,
# pinning to a known-good for a paper deadline, etc.):
#     botainer image build agent-claude   (the version is pinned by CLAUDE_CODE_VERSION below;
#   edit it and rebuild — `image build` takes no --build-arg)
# The npm version syntax is accepted: ^2, ~2.0.42, =2.0.42, 2.0.42, etc.
#
# After install we run `claude --version` and on failure print the
# available versions, so future drift surfaces what's actually
# publishable.
FROM base AS claude-cli
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && (claude --version \
        || (echo "claude --version failed; available recent versions:" >&2 \
            && npm view @anthropic-ai/claude-code versions --json 2>/dev/null \
               | tail -30 >&2 \
            && exit 1))

# Ship the wrapper script that primes AGENT_HINTS into Claude's system
# prompt. The launcher mounts AGENT_HINTS.md at /workspace/.botainer/
# at session compose; this wrapper exec's `claude --append-system-prompt`
# with the hints when present.
COPY entrypoint_wrap.sh /usr/local/bin/agent-claude-entrypoint
RUN chmod +x /usr/local/bin/agent-claude-entrypoint

# ── Web browser for the `browser` plugin (opt-in at runtime) ──────────────────
# WHY BAKED IN, not fetched at runtime: the §4 cage has no root/apt, and under
# apptainer the container runs as the caller's host uid with HOME=/ (unwritable),
# so a runtime `npx playwright install` CANNOT add Chromium's ~30 system libs and
# `npx` itself fails on the npm cache. All three pieces must be present at build:
#   1. apt `chromium` → pulls in every runtime lib (libnss3/libgbm1/libasound2/…)
#      as package deps; those same libs satisfy Playwright's own Chromium too.
#   2. `@playwright/mcp` installed GLOBALLY → the MCP server is a plain on-PATH
#      binary (`playwright-mcp`), so runtime needs no `npx`/writable HOME.
#   3. Playwright's matching Chromium build, installed via @playwright/mcp's OWN
#      bundled playwright-core (guarantees the version the MCP server expects),
#      into a world-readable baked path.
# Cost: ~400 MB. The `browser` plugin stays opt-in; a build arg to skip this for
# a lean image is a tracked follow-up. See docs/CAPABILITY-SURFACE.md §4au.
ARG PLAYWRIGHT_MCP_VERSION=latest
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends chromium fonts-liberation; \
    rm -rf /var/lib/apt/lists/*; \
    npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"; \
    command -v playwright-mcp >/dev/null \
        || { echo "FATAL: playwright-mcp bin missing after install" >&2; exit 1; }; \
    GROOT="$(npm root -g)"; \
    PW="$(find "$GROOT" -type f -path '*/playwright/cli.js' 2>/dev/null | head -1)"; \
    [ -n "$PW" ] || PW="$(find "$GROOT" -type f -path '*/playwright-core/cli.js' 2>/dev/null | head -1)"; \
    { [ -n "$PW" ] && [ -f "$PW" ]; } \
        || { echo "FATAL: no playwright cli.js under $GROOT; cli.js present:" >&2; find "$GROOT" -name cli.js >&2 || true; exit 1; }; \
    echo "using playwright cli: $PW"; \
    node "$PW" install chromium; \
    chmod -R a+rX /opt/pw-browsers

# ── Watchable VIEWER stack (opt-in `plugins.browser.viewer: true`) ────────────
# Runs INSIDE this container (no separate helper container) so the human can
# WATCH + CLICK the SAME headed Chromium the agent drives. Xvfb = virtual screen,
# x11vnc = share it, noVNC(web assets) + websockify = serve it as a web page.
# websockify from PIP pinned >= 0.12 because Debian bookworm ships 0.10 which
# lacks `--unix-listen` — REQUIRED for the HPC path (0700 unix sockets end-to-end;
# a TCP port on a shared apptainer node is reachable by co-tenants — Fable-5 C1).
# Only runs when the entrypoint sees BOTAINER_BROWSER_VIEWER=1; idle otherwise.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc x11-utils fonts-liberation; \
    rm -rf /var/lib/apt/lists/*; \
    pip3 install --no-cache-dir --break-system-packages "websockify>=0.12"; \
    command -v websockify >/dev/null \
        || { echo "FATAL: websockify not on PATH after pip install" >&2; exit 1; }; \
    ws_ver="$(websockify --help 2>&1 | grep -oiE 'unix-listen' | head -1 || true)"; \
    [ -n "$ws_ver" ] \
        || { echo "FATAL: websockify lacks --unix-listen (need >= 0.12)" >&2; exit 1; }
COPY viewer-start.sh /usr/local/bin/botainer-viewer-start
RUN chmod 0755 /usr/local/bin/botainer-viewer-start

USER agent
WORKDIR /workspace

# Env-var routing for /packages (Phase 2 binds these dirs).
# npm GLOBAL installs must land in /packages and be on PATH. (#230)
#
# NODE_PATH (set below) fixes `require()` resolution for LIBRARIES and does
# nothing for `npm install -g`, which writes BINARIES to the global prefix. That
# prefix defaulted to root-owned /usr/local/lib/node_modules while the container
# runs --user 1000:1000, so a global install FAILED — and the agent hints told
# the agent "both global and local installs work", which was false for global.
#
# It matters beyond tidiness: `inject_credentials` lets a session hold another
# agent's credentials, but the images are single-agent (this one installs only
# its own CLI). Installing the other agent's CLI at runtime is how that gap is
# closed, and it has to persist in /packages to be worth doing once.
ENV NPM_CONFIG_PREFIX=/packages/npm-global
ENV PATH=/packages/npm-global/bin:$PATH

ENV PIP_TARGET=/packages/pip \
    PYTHONPATH=/packages/pip \
    NODE_PATH=/packages/node_modules \
    JULIA_DEPOT_PATH=/packages/julia_depot \
    R_LIBS_USER=/packages/R_libs \
    CONDA_ENVS_PATH=/packages/conda_envs \
    CARGO_HOME=/packages/cargo \
    GOPATH=/packages/go \
    CLAUDE_CONFIG_DIR=/home/agent/.claude

ENTRYPOINT ["/usr/local/bin/agent-claude-entrypoint"]
CMD []
