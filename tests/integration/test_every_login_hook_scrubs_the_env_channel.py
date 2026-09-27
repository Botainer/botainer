"""Every login hook that launches apptainer must scrub the env channel.

THE CHANNEL. `APPTAINERENV_FOO=bar` in the host environment becomes `FOO=bar`
INSIDE the container — that is what the prefix is for, and it survives
`--cleanenv`, which strips ordinary variables and honours these by design. So a
poisoned login environment on a shared node reaches into the container unless
the launching process removes them first. `APPTAINERENV_LD_PRELOAD` is the sharp
version: arbitrary code in the container's processes.

THIS IS ONE DECISION WITH SIX SITES. Four had it — the session adapter, the HPC
submit path, `_sbatch_env` for child jobs, and the two CLAUDE login hooks. The
two CODEX login hooks did not: both build an `apptainer exec` argv and then run
`subprocess.run(cmd, check=False)` with NO `env=` at all, inheriting whatever
the login shell had. Their Claude siblings call `build_apptainer_subenv()` two
lines earlier.

That is the sibling-drift class on a path that WRITES A CREDENTIAL, which is
why it is a test and not a comment. The shape of the guard matters too: it does
not grep for the prefixes, because a module can mention them and still not use
them. It CALLS each hook's env builder with a poisoned environment and looks at
what comes back — presence is not effect.

WHY A TEST AND NOT A STRUCTURE. Plugins are separate trees loaded by path; only
two of eleven hooks import from `botainer` at all, so a shared helper would be a
convention someone must remember rather than a property. Until hooks have a
required envelope for this, the honest mechanism is a check that enumerates them
from disk — so a NEW agent plugin is covered before anyone remembers it exists.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"

# What must never survive into the subprocess env.
#
# THE PROPERTY IS ABOUT VALUES, NOT KEYS, and getting that wrong the first time
# nearly produced a false finding against the CLAUDE hooks. They strip every
# `APPTAINERENV_*`/`SINGULARITYENV_*` and then deliberately re-add
# `*ENV_CLAUDE_CONFIG_DIR=/out` — their own explicit propagation, which is the
# documented design and the reason `--cleanenv` is used at all. A key-presence
# assertion flagged that as a leak, and "fixing" it would have removed the
# propagation the login needs.
#
# So: the HOST must not be able to choose a value. A hook setting its own is
# exactly what should happen. `CLAUDE_CONFIG_DIR` is in the poison set on
# purpose — it is the one an attacker would most want to set, and the check
# still passes because the hook overwrites it.
POISON = {
    "APPTAINERENV_LD_PRELOAD": "/tmp/evil.so",
    "APPTAINERENV_PATH": "/tmp/evil/bin",
    "SINGULARITYENV_LD_PRELOAD": "/tmp/evil.so",
    "SINGULARITYENV_CLAUDE_CONFIG_DIR": "/tmp/evil",
    "APPTAINERENV_CLAUDE_CONFIG_DIR": "/tmp/evil",
}
# And what must survive, so the scrub is not just "return {}" — a guard that
# passes against an empty dict has proved nothing about a working login.
KEEP = {"HOME": "/home/someone", "PATH": "/usr/bin:/bin", "TERM": "xterm"}


def _login_hooks_that_launch_apptainer() -> list[Path]:
    """Every agent plugin login hook that builds an `apptainer exec` argv.

    Enumerated from disk rather than listed here: a list would be correct on
    the day it was written, which is how the codex pair came to be missing in
    the first place.
    """
    found = []
    for hook in sorted(PLUGINS.glob("agent-*/hooks/login.py")):
        if "def build_apptainer_argv" in hook.read_text(encoding="utf-8"):
            found.append(hook)
    return found


def _load(hook: Path):
    spec = importlib.util.spec_from_file_location(
        f"_hook_{hook.parent.parent.name.replace('-', '_')}", hook)
    assert spec and spec.loader, f"cannot load {hook}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_enumeration_finds_all_four_agents() -> None:
    """ANTI-VACUOUS. If the glob matched nothing, every test below would pass
    by iterating an empty list — the failure mode that makes a guard scenery."""
    hooks = _login_hooks_that_launch_apptainer()
    names = sorted(h.parent.parent.name for h in hooks)
    assert len(names) >= 4, (
        f"expected at least the four bundled agent login hooks, found {names}")
    assert any("codex" in n for n in names), f"no codex hook found: {names}"
    assert any("claude" in n for n in names), f"no claude hook found: {names}"


@pytest.mark.parametrize(
    "hook", _login_hooks_that_launch_apptainer(),
    ids=lambda p: p.parent.parent.name)
def test_login_hook_exposes_a_scrubbed_env_builder(hook: Path) -> None:
    """The function must EXIST. Its absence is how the codex pair ran
    `subprocess.run(cmd)` with no `env=` at all."""
    mod = _load(hook)
    assert hasattr(mod, "build_apptainer_subenv"), (
        f"{hook.parent.parent.name} builds an apptainer argv but has no "
        f"build_apptainer_subenv(); its sibling does. Without it the login "
        f"inherits APPTAINERENV_* from the shell, and those cross --cleanenv "
        f"into a container that is about to hold a credential."
    )


@pytest.mark.parametrize(
    "hook", _login_hooks_that_launch_apptainer(),
    ids=lambda p: p.parent.parent.name)
def test_the_env_channel_does_not_survive(hook: Path) -> None:
    """THE EFFECT, not the presence. Poison in, nothing poisonous out."""
    mod = _load(hook)
    builder = getattr(mod, "build_apptainer_subenv", None)
    if builder is None:
        pytest.skip("covered by test_login_hook_exposes_a_scrubbed_env_builder")

    out = builder({**KEEP, **POISON})

    leaked = sorted(k for k, v in POISON.items() if out.get(k) == v)
    assert not leaked, (
        f"{hook.parent.parent.name} passes the HOST'S OWN VALUE for {leaked} "
        f"through to apptainer. These cross --cleanenv by design, so they land "
        f"inside a login container that is about to hold a credential. (A hook "
        f"setting its own value for one of these keys is fine and expected — "
        f"what must not survive is a value the environment chose.)"
    )


@pytest.mark.parametrize(
    "hook", _login_hooks_that_launch_apptainer(),
    ids=lambda p: p.parent.parent.name)
def test_the_scrub_does_not_empty_the_environment(hook: Path) -> None:
    """THE OPPOSITE DIRECTION. A builder that returned `{}` would pass the test
    above and break every login — no PATH, no HOME. It is also exactly what a
    careless fix looks like."""
    mod = _load(hook)
    builder = getattr(mod, "build_apptainer_subenv", None)
    if builder is None:
        pytest.skip("covered by test_login_hook_exposes_a_scrubbed_env_builder")

    out = builder({**KEEP, **POISON})

    for k, v in KEEP.items():
        assert out.get(k) == v, (
            f"{hook.parent.parent.name} dropped {k} from the subprocess env; "
            f"the scrub is meant to remove an injection channel, not to "
            f"rebuild the environment from nothing."
        )
