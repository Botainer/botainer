"""Pinned-version drift gate for agent plugins' image recipes.

Each agent plugin (agent-claude, agent-codex) ships TWO build paths:
  - `Dockerfile` for the docker runtime (laptop / dev)
  - `<name>.def` for apptainer (HPC — first-class peer per CLAUDE.md)

Both files pin the same upstream component versions (CLI, miniforge,
julia, agent UID/GID). They have to stay in sync or the docker image
and the .sif diverge — same image label, different contents.

Drift has bitten twice in two days:
  - commit 85f0ca1: Dockerfile bumped CLAUDE_CODE_VERSION;
    .def stayed pinned to a now-unpublished version → real-host Grace
    build failed with npm 'notarget'.
  - commit 4fb1f0f: same shape, different version.

Same principle-vs-prose failure mode as the umbrella-bind disaster:
the rule lives only in a comment ("mirrors the Dockerfile") and
nothing tests it. This test makes the rule mechanical.

Pattern: same as test_capability_surface_matches_inventory.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"

# Canonical pin names checked across recipes. Each agent's two recipes
# (Dockerfile + .def) must agree on every value, AND every pin in this
# set must appear in BOTH recipes if it appears in either — silent
# omission on one side is exactly the drift mode this test catches.
#
# Per-agent pins: only the CLI-version pin differs across agents (claude
# uses CLAUDE_CODE_VERSION, codex uses CODEX_CLI_VERSION). The rest are
# language-scaffold pins both kitchen-sink images share.
CANONICAL_PINS = {
    "agent-claude": {
        "CLAUDE_CODE_VERSION",
        "MINIFORGE_VERSION",
        "JULIA_VERSION",
        "AGENT_UID",
        "AGENT_GID",
    },
    "agent-codex": {
        "CODEX_CLI_VERSION",
        "MINIFORGE_VERSION",
        "JULIA_VERSION",
        "AGENT_UID",
        "AGENT_GID",
    },
}

# Match either:
#   ARG NAME=VALUE  (Dockerfile)
#   ARG NAME VALUE  (Dockerfile alternate)
# Strips quotes/whitespace.
_DOCKERFILE_ARG = re.compile(
    r"^\s*ARG\s+([A-Z_][A-Z0-9_]*)\s*=\s*(\S+)\s*$", re.MULTILINE
)

# Match the .def's %post export blocks:
#   export NAME=VALUE
# Strips quotes/whitespace. We only look at exports inside %post (where
# the .def captures its pins; the agent-claude .def uses `export FOO=…`
# in %post rather than %arguments).
_DEF_EXPORT = re.compile(
    r"^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(\S+)\s*$", re.MULTILINE
)


def _parse_dockerfile_args(path: Path) -> dict[str, str]:
    text = path.read_text()
    return {m.group(1): m.group(2).strip('"\'') for m in _DOCKERFILE_ARG.finditer(text)}


def _parse_def_exports(path: Path) -> dict[str, str]:
    text = path.read_text()
    return {m.group(1): m.group(2).strip('"\'') for m in _DEF_EXPORT.finditer(text)}


@pytest.mark.parametrize("agent", sorted(CANONICAL_PINS))
def test_image_recipes_agree_on_pins(agent: str) -> None:
    """Dockerfile and .def for the same agent agree on every canonical pin.

    Failure means a bump landed in one file and not the other — exactly
    the / failure shape. Fix by syncing both files
    in the same commit.
    """
    plugin_dir = PLUGINS / agent
    dockerfile = plugin_dir / "Dockerfile"
    def_file = plugin_dir / f"{agent}.def"

    assert dockerfile.exists(), (
        f"{agent}: Dockerfile missing at {dockerfile} — every shipped agent "
        f"plugin must have a docker build recipe."
    )
    assert def_file.exists(), (
        f"{agent}: {def_file.name} missing at {def_file} — HPC parity "
        f"(CLAUDE.md): apptainer is a first-class peer to docker, not a "
        f"doc afterthought. Author the .def or doc-remove this agent "
        f"from HPC-supported docs."
    )

    docker_pins = _parse_dockerfile_args(dockerfile)
    def_pins = _parse_def_exports(def_file)
    expected = CANONICAL_PINS[agent]

    missing_from_docker = expected - docker_pins.keys()
    missing_from_def = expected - def_pins.keys()

    # Allowlist: if a pin is INTENTIONALLY omitted (not just drift), the
    # recipe file MUST carry the literal sentinel `RECIPE-OMIT: <NAME>`
    # in a comment. That makes the omission a positive choice an
    # auditor can see, not an oversight. None at present; if a future
    # bump ships a slimmer recipe, add the sentinel + update this test.
    def_text = def_file.read_text()
    docker_text = dockerfile.read_text()
    missing_from_def = {
        name for name in missing_from_def
        if f"RECIPE-OMIT: {name}" not in def_text
    }
    missing_from_docker = {
        name for name in missing_from_docker
        if f"RECIPE-OMIT: {name}" not in docker_text
    }

    assert not missing_from_docker, (
        f"{agent}: Dockerfile is missing canonical pin(s) {sorted(missing_from_docker)}. "
        f"Either add `ARG NAME=value` to {dockerfile} or, if it's intentionally "
        f"omitted, add a `# RECIPE-OMIT: NAME — why` comment to the Dockerfile."
    )
    assert not missing_from_def, (
        f"{agent}: {def_file.name} is missing canonical pin(s) {sorted(missing_from_def)} "
        f"that the Dockerfile declares. Either add `export NAME=value` in the "
        f"%post block of {def_file} (mirroring the Dockerfile) or, if "
        f"intentionally omitted, add a `# RECIPE-OMIT: NAME — why` comment to "
        f"the .def. Drift in this direction is exactly the 2026-05-19 Grace "
        f"build failure (HPC parity rule, CLAUDE.md)."
    )

    # Every pin present on both sides must have the same value.
    mismatches: list[str] = []
    for name in sorted(expected):
        if name in docker_pins and name in def_pins:
            if docker_pins[name] != def_pins[name]:
                mismatches.append(
                    f"  {name}: Dockerfile={docker_pins[name]!r} "
                    f".def={def_pins[name]!r}"
                )
    assert not mismatches, (
        f"{agent}: Dockerfile and {def_file.name} pin different values:\n"
        + "\n".join(mismatches)
        + "\nBump both in the same commit. (CLAUDE.md HPC-parity rule: "
        "docker and apptainer are peers, not master+mirror.)"
    )


# ---------------------------------------------------------------------------
# Runtime env parity (added).
#
# The pin test above compares VERSION pins and caught nothing when
# agent-codex.def shipped with NONE of the eight /packages routing vars its own
# Dockerfile set — under a Dockerfile comment reading "matches agent-claude",
# which the .def did not. agent-claude.def had them all, so the gap was
# codex-on-apptainer only: a caged codex on a cluster installed pip/cargo/conda
# packages into the container's ephemeral layer instead of the persistent
# /packages bind, and lost them at session end.
#
# Versions were pinned across recipes; the environment the agent actually RUNS
# IN was not. HPC is the product (CLAUDE.md), so a var that exists only on
# docker is a parity break, not a detail. This test closes that half.
# ---------------------------------------------------------------------------

# Routing/credential vars that must be identical in both recipes for an agent.
# Shared by both kitchen-sink images; the credential var differs per agent
# because the CLIs read different ones.
_PACKAGE_ROUTES = {
    "PIP_TARGET", "PYTHONPATH", "NODE_PATH", "JULIA_DEPOT_PATH",
    "R_LIBS_USER", "CONDA_ENVS_PATH", "CARGO_HOME", "GOPATH",
}
RUNTIME_ENV = {
    "agent-claude": _PACKAGE_ROUTES | {"CLAUDE_CONFIG_DIR"},
    "agent-codex": _PACKAGE_ROUTES | {"OPENAI_API_KEY_FILE"},
}


def _parse_dockerfile_env(path: Path) -> dict[str, str]:
    """ENV assignments, honouring backslash continuations.

    `ENV A=1 \\\n    B=2` is one statement spanning lines, so a line-by-line
    regex silently sees only the first pair — which would make this test pass
    while missing seven of the eight vars it exists to check.
    """
    text = path.read_text().replace("\\\n", " ")
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ENV "):
            continue
        for token in stripped[4:].split():
            if "=" in token:
                key, _, value = token.partition("=")
                out[key.strip()] = value.strip().strip("\"'")
    return out


@pytest.mark.parametrize("agent", sorted(RUNTIME_ENV))
def test_image_recipes_agree_on_runtime_env(agent: str) -> None:
    """Both recipes give the agent the same routing + credential environment."""
    plugin_dir = PLUGINS / agent
    docker_env = _parse_dockerfile_env(plugin_dir / "Dockerfile")
    def_env = _parse_def_exports(plugin_dir / f"{agent}.def")
    expected = RUNTIME_ENV[agent]

    missing_docker = sorted(expected - docker_env.keys())
    missing_def = sorted(expected - def_env.keys())
    assert not missing_docker, (
        f"{agent}/Dockerfile does not set {missing_docker}; the docker session "
        f"would lose that routing.")
    assert not missing_def, (
        f"{agent}/{agent}.def does not export {missing_def}, but the Dockerfile "
        f"sets them — so this agent behaves differently ON HPC, which is the "
        f"product. A caged agent would install packages into the ephemeral "
        f"container layer instead of the persistent /packages bind and lose "
        f"them at session end.")

    disagree = {k: (docker_env[k], def_env[k]) for k in sorted(expected)
                if docker_env[k] != def_env[k]}
    assert not disagree, (
        f"{agent}: docker and apptainer disagree on {disagree} — the same "
        f"session would route to different paths depending on runtime.")


def test_both_agents_route_packages_identically() -> None:
    """The two agents must agree with EACH OTHER on /packages routing.

    They share one persistent /packages bind and one set of botainer-managed
    route vars (composition._BOTAINER_MANAGED_ROUTES). If codex routed pip
    somewhere claude did not, switching agents in one project — which
    `botainer start --agent` now makes a one-word change — would silently
    strand everything the other agent installed.
    """
    claude = _parse_dockerfile_env(PLUGINS / "agent-claude" / "Dockerfile")
    codex = _parse_dockerfile_env(PLUGINS / "agent-codex" / "Dockerfile")
    disagree = {k: (claude.get(k), codex.get(k)) for k in sorted(_PACKAGE_ROUTES)
                if claude.get(k) != codex.get(k)}
    assert not disagree, (
        f"agents disagree on package routing {disagree}; `--agent` switching "
        f"would strand packages the other agent installed.")
