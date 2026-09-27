# check=skip=SecretsUsedInArgOrEnv
#
# WHY THAT CHECK IS OFF HERE, and what replaces it.
#
# BuildKit flags `ENV OPENAI_API_KEY_FILE=...` because the NAME contains
# API_KEY. The value is a PATH — /home/agent/.openai/api_key — and the `_FILE`
# suffix is the convention that exists so a secret never becomes an env VALUE
# in the first place. The rule matches on the name and does not understand the
# suffix, so it fires on the secure pattern and would fire on every image build
# forever. A warning nobody can act on trains people to skip the channel, which
# is how the next real one gets missed.
#
# The skip is file-scoped, which means it would also hide a GENUINE secret
# added here later. So it does not stand alone:
# `tests/unit/test_no_secret_values_in_images.py` scans every shipped Dockerfile
# and .def for an ENV/ARG/export whose VALUE looks like key material, and
# permits a `*_FILE` name only when its value is an absolute path. That is a
# narrower check than the one disabled, and one we can actually keep true.
#
# The key itself arrives at runtime as a read-only bind; nothing is baked in.
# Dockerfile for agent-codex (botainer v0.1.x)
#
# Mirrors agent-claude's structure: kitchen-sink language tools so the
# agent can pip install / npm install / etc. inside the container.
# OpenAI Codex CLI is the headline addition.
#
# Image tag: botainer/agent-codex:0.1
# Build: docker build -t botainer/agent-codex:0.1 plugins/agent-codex/

FROM node:24-bookworm-slim AS base

# openmpi-bin + libopenmpi-dev: a PMIx-capable MPI so dispatched `mpi: pmix` HPC
# jobs run multi-node out of the box (PMIx cross-version compatible; no match to
# the cluster needed). Mirrors agent-codex.def; unused on the docker/laptop path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg2 git \
        build-essential pkg-config \
        python3 python3-pip python3-venv python3-dev \
        ripgrep less jq vim-tiny \
        libcurl4-openssl-dev libssl-dev libxml2-dev \
        openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# uv (Astral): fast Python tool.
RUN curl -fsSL https://astral.sh/uv/install.sh | \
        env UV_INSTALL_DIR=/usr/local/bin sh \
    && /usr/local/bin/uv --version

# R for any users mixing Codex + R.
RUN apt-get update && apt-get install -y --no-install-recommends \
        r-base r-base-dev \
    && rm -rf /var/lib/apt/lists/*

# Architecture detection (same fix as agent-claude). Without this,
# Apple Silicon builds fail with rosetta `failed to open elf` on
# miniforge install. See DN-032.
ARG TARGETARCH

# Conda (miniforge): scientific Python stack.
ARG MINIFORGE_VERSION=24.3.0-0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64)  mf_arch=x86_64 ;; \
        arm64)  mf_arch=aarch64 ;; \
        *)      echo "unsupported arch: $arch" >&2; exit 2 ;; \
    esac; \
    curl -fsSL "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-${MINIFORGE_VERSION}-Linux-${mf_arch}.sh" \
        -o /tmp/miniforge.sh; \
    bash /tmp/miniforge.sh -b -p /opt/conda; \
    rm /tmp/miniforge.sh; \
    /opt/conda/bin/conda clean --all -y
ENV PATH=/opt/conda/bin:$PATH

# Julia for parity.
ARG JULIA_VERSION=1.10.4
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64)  jl_dir=x64;     jl_arch=x86_64 ;; \
        arm64)  jl_dir=aarch64; jl_arch=aarch64 ;; \
        *)      echo "unsupported arch: $arch" >&2; exit 2 ;; \
    esac; \
    jl_minor="$(echo ${JULIA_VERSION} | cut -d. -f1-2)"; \
    curl -fsSL "https://julialang-s3.julialang.org/bin/linux/${jl_dir}/${jl_minor}/julia-${JULIA_VERSION}-linux-${jl_arch}.tar.gz" \
        -o /tmp/julia.tar.gz; \
    tar -xzf /tmp/julia.tar.gz -C /opt; \
    ln -s /opt/julia-${JULIA_VERSION}/bin/julia /usr/local/bin/julia; \
    rm /tmp/julia.tar.gz

# Agent user (UID 1001 same as agent-claude for cross-runtime parity).
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

# OpenAI Codex CLI: pinned. Bump via this Dockerfile.
# Task #118: was '@openai/codex-cli' (doesn't exist on npm at v0.1.x)
# with a `|| (placeholder)` fallthrough that SILENTLY shipped a stub
# image so users discovered the breakage only when invoking codex.
# Now: try the published name `@openai/codex` first; if THAT fails,
# the build FAILS LOUDLY (no stub). Users see the error at build time,
# not at agent-launch time inside a broken container.
FROM base AS codex-cli
ARG CODEX_CLI_VERSION=latest
RUN npm install -g @openai/codex@${CODEX_CLI_VERSION} \
    || (echo "FATAL: @openai/codex@${CODEX_CLI_VERSION} install failed." >&2 \
        && echo "  - Check npm view @openai/codex versions to confirm name+version" >&2 \
        && echo "  - Or pass --build-arg CODEX_CLI_VERSION=<known-good>" >&2 \
        && exit 4)

# Entrypoint wrapper that primes AGENT_HINTS into the system prompt.
COPY entrypoint_wrap.sh /usr/local/bin/agent-codex-entrypoint
RUN chmod +x /usr/local/bin/agent-codex-entrypoint

USER agent
WORKDIR /workspace

# Env-var routing for /packages (matches agent-claude).
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
    OPENAI_API_KEY_FILE=/home/agent/.openai/api_key

ENTRYPOINT ["/usr/local/bin/agent-codex-entrypoint"]
CMD []
