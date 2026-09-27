"""A login container must RUN its command, not hand it to the agent.

Without an entrypoint override, the shell command can reach the agent
option parser and fail:

    Running `codex login --device-auth` INSIDE the agent-codex container.
    Error parsing -c overrides: Invalid override (missing '='):
        umask 0077 && exec codex login --device-auth
    codex login exited 1

Every agent image ends its entrypoint with `exec <agent> "$@"`. So

    docker run <image> sh -c "umask 0077 && exec codex login"

does NOT run sh. It runs

    agent-codex-entrypoint sh -c "umask 0077 && exec codex login"
    -> exec codex sh -c "umask 0077 && exec codex login"

and the login command becomes ARGUMENTS TO THE AGENT. codex happens to have its
own `-c` flag (config override) and died loudly. Claude accepted the junk argv
and started anyway — same defect, quieter, and it had been shipping in both
claude login hooks the whole time without anyone noticing.

`apptainer exec` runs the command directly and ignores %runscript, so the HPC
path was correct and only docker was broken. That is the THIRD inverted-parity
bug found in one day (after the missing `--init`, #108) — the assumption that
HPC is the fragile side is itself the bug.

WHY THIS TEST IS SHAPED THIS WAY. Asserting `"--entrypoint" in argv` for the
four hooks that exist today would pass forever and teach nothing. The real
invariant is a RELATIONSHIP between two files: if the image declares an
ENTRYPOINT, the login argv must override it. So the test reads the ENTRYPOINT
out of each agent's Dockerfile and requires the override only when one exists —
which keeps working for an agent added next year with a different image.

None of the pre-existing login tests caught this. They asserted the command
STRING was right (`argv[-1].endswith("codex login")`) and it always was; what
was wrong was WHO would execute it. Presence is not effect.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PLUGINS = _REPO / "plugins"

# (login hook plugin, plugin dir holding the Dockerfile)
LOGIN_HOOKS = [
    ("agent-claude", "agent-claude"),
    ("agent-claude-shared", "agent-claude"),
    ("agent-codex", "agent-codex"),
    ("agent-codex-shared", "agent-codex"),
]


def _load(plugin: str):
    path = _PLUGINS / plugin / "hooks" / "login.py"
    spec = importlib.util.spec_from_file_location(f"_login_{plugin}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _image_entrypoint(image_dir: str) -> str | None:
    """The ENTRYPOINT the image declares, if any."""
    text = (_PLUGINS / image_dir / "Dockerfile").read_text()
    m = re.search(r"^\s*ENTRYPOINT\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _docker_argv(mod, plugin: str) -> list[str]:
    """Build each hook's docker argv, tolerating their differing signatures."""
    creds = Path("/S/creds")
    if plugin.startswith("agent-codex"):
        return mod.build_docker_argv("img", creds, 54545, 54549, True)
    return mod.build_docker_argv("img", creds, 54545, 54549)


@pytest.mark.parametrize("plugin,image_dir", LOGIN_HOOKS)
def test_login_overrides_the_image_entrypoint(plugin, image_dir) -> None:
    """THE regression guard.

    If the image declares an ENTRYPOINT, the login container must override it —
    otherwise the login command is passed to the agent as arguments instead of
    being run.
    """
    entrypoint = _image_entrypoint(image_dir)
    if entrypoint is None:
        pytest.skip(f"{image_dir} declares no ENTRYPOINT to override")

    argv = _docker_argv(_load(plugin), plugin)
    assert "--entrypoint" in argv, (
        f"{plugin} login does not pass --entrypoint, but {image_dir}/Dockerfile "
        f"declares ENTRYPOINT {entrypoint}. docker would run\n"
        f"    {entrypoint} sh -c '<login command>'\n"
        f"which hands the login command to the AGENT as arguments. This is the "
        f"bug where Codex parses shell arguments as configuration overrides.")


@pytest.mark.parametrize("plugin,image_dir", LOGIN_HOOKS)
def test_the_shell_is_the_program_not_an_argument(plugin, image_dir) -> None:
    """With `--entrypoint sh`, everything after the image is sh's OWN argv.

    Leaving the old positional "sh" in place would make it sh's $0 and the
    `-c` would be lost — a silent variant of the same bug, so the override and
    the argv tail have to move together.
    """
    argv = _docker_argv(_load(plugin), plugin)
    assert argv[argv.index("--entrypoint") + 1] == "sh"

    after_image = argv[argv.index("img") + 1:]
    assert after_image[0] == "-c", (
        f"{plugin}: first token after the image is {after_image[0]!r}; with "
        f"--entrypoint sh it must be -c, or the shell gets a stray $0")
    assert "sh" not in after_image, (
        f"{plugin}: 'sh' still passed positionally after the image ({after_image})")


@pytest.mark.parametrize("plugin,image_dir", LOGIN_HOOKS)
def test_apptainer_does_NOT_need_the_override(plugin, image_dir) -> None:
    """`apptainer exec` runs the given command and ignores %runscript, so the
    HPC path was always correct here.

    Pinned so nobody "fixes" apptainer to match docker: --entrypoint is not an
    apptainer flag and would simply fail. Recording WHICH runtime needed the
    change is the point — this was the third bug in one day where docker was
    the broken side, and the reflex to assume HPC is the fragile one is itself
    a source of bugs.
    """
    mod = _load(plugin)
    creds = Path("/S/creds")
    if plugin.startswith("agent-codex"):
        argv = mod.build_apptainer_argv("/u/apptainer", Path("/S/x.sif"), creds, True)
    else:
        argv = mod.build_apptainer_argv("/u/apptainer", Path("/S/x.sif"), creds)
    assert "--entrypoint" not in argv, "not an apptainer flag; exec takes the command"
    assert argv[-3:-1] == ["sh", "-c"], (
        f"apptainer exec must be given `sh -c <cmd>` directly, got {argv[-3:]}")
