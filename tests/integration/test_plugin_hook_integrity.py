"""Every hook a plugin declares must actually be runnable.

A declared hook is executed directly by the launcher. A missing file,
executable bit, shebang or script body can therefore break startup before
the agent runs. Check every declared hook rather than only the hook that
first exposed a problem.

Checked against the MANIFEST rather than a glob, because what matters is what
the launcher will try to run — a stray .py in hooks/ that nothing declares is
not a failure, and a declared script that does not exist very much is.
"""
from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_PLUGINS = _REPO / "plugins"


def _declared_hooks() -> list[tuple[str, str, Path]]:
    """(plugin, when, absolute script path) for every hook every plugin declares."""
    out: list[tuple[str, str, Path]] = []
    for manifest in sorted(_PLUGINS.glob("*/botainer-plugin.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text()) or {}
        except yaml.YAMLError as exc:                      # pragma: no cover
            pytest.fail(f"{manifest}: unparseable manifest: {exc}")
        for hook in data.get("hooks") or []:
            script = hook.get("script")
            if not script:
                continue
            out.append((manifest.parent.name, hook.get("when", "?"),
                        manifest.parent / script))
    return out


HOOKS = _declared_hooks()


def test_there_are_hooks_to_check() -> None:
    """Guard the guard: if the manifest schema changes shape, every test below
    would silently pass over an empty list and prove nothing."""
    assert len(HOOKS) > 10, f"only found {len(HOOKS)} declared hooks — parser broken?"


@pytest.mark.parametrize("plugin,when,script", HOOKS,
                         ids=[f"{p}:{w}" for p, w, _ in HOOKS])
def test_declared_hook_exists(plugin, when, script) -> None:
    assert script.is_file(), (
        f"{plugin} declares {when} -> {script.name}, which does not exist. "
        f"The launcher refuses at session start, after the user has already "
        f"configured everything.")


def _git_modes() -> dict[str, str] | None:
    """Mode bits as GIT records them, which is what reaches another machine.

    `None` when git cannot answer, which is not exotic: `pyproject.toml`'s
    sdist ships `tests/integration/**` and carries no `.git`; `git` may not be
    installed; and git exits 128 ("detected dubious ownership") whenever the
    checkout is owned by a different uid — the normal case when the suite runs
    inside a container over a bind-mounted clone, i.e. how this project is
    developed.

    This ran at MODULE level with `check=True`. OBSERVED on a copy of the tree
    with no `.git`, and again with `git` off PATH:

        !!! Interrupted: 1 error during collection !!!
        no tests collected, 1 error

    The WHOLE suite ran zero tests, and the message named a
    `CalledProcessError` rather than a missing prerequisite. A module-level
    probe that raises does not fail one file — it fails everything.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "-s", "plugins/"],
                             cwd=_REPO, capture_output=True, text=True,
                             check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    modes: dict[str, str] = {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]
    return modes


GIT_MODES = _git_modes()


@pytest.mark.parametrize("plugin,when,script", HOOKS,
                         ids=[f"{p}:{w}" for p, w, _ in HOOKS])
def test_declared_hook_is_executable_IN_GIT(plugin, when, script) -> None:
    """Hook executability is asserted on the Git index, not the filesystem.

    The launcher execs hook scripts directly, so a missing +x is a hard refusal
    at session start. It survives commit, review and the whole suite, because
    nothing else ever tries to run them.

    ASSERTED VIA GIT ON PURPOSE. The obvious check, `os.access(script, X_OK)`,
    is WORTHLESS in this dev container: the bind mount reports mode 644 and
    still answers True to X_OK. So a filesystem check passes here and fails on
    a cluster with normal executable-bit enforcement. The Git
    index mode is what a clone or `rsync -a` actually carries to the cluster,
    which makes it the thing worth asserting.
    """
    if GIT_MODES is None:
        pytest.skip(
            "git cannot report index modes for this tree (no .git, no git on "
            "PATH, or refused ownership). The FILESYSTEM mode is NOT a "
            "substitute and this test does not fall back to one: a bind mount "
            "in this project's own dev container reports 644 and still answers "
            "True to os.access(X_OK), which is precisely the blindness that "
            "let a non-executable hook ship. Run from a git checkout.")
    rel = str(script.relative_to(_REPO))
    mode = GIT_MODES.get(rel)
    assert mode is not None, f"{rel} is not tracked by git; it cannot deploy"
    assert mode == "100755", (
        f"{plugin}'s {when} hook is mode {mode} in git (needs 100755): {rel}\n"
        f"    fix: chmod +x {rel} && git update-index --chmod=+x {rel}\n"
        f"The launcher cannot execute a hook shipped without its executable bit.")


@pytest.mark.parametrize("plugin,when,script", HOOKS,
                         ids=[f"{p}:{w}" for p, w, _ in HOOKS])
def test_declared_hook_has_a_shebang(plugin, when, script) -> None:
    """Executed directly, so the kernel needs a shebang to pick an interpreter.

    Without one the exec either fails outright or — worse on some systems —
    the file is handed to /bin/sh, which will run a python script as shell
    until it hits something that happens to be a valid command.
    """
    first = script.read_bytes().split(b"\n", 1)[0]
    assert first.startswith(b"#!"), (
        f"{plugin}'s {when} hook has no shebang (starts {first[:40]!r}); it is "
        f"exec'd directly, not passed to an interpreter.")
    assert b"python" in first, (
        f"{plugin}'s {when} hook shebang is {first!r}; every hook here is "
        f"python.")


@pytest.mark.parametrize("plugin,when,script", HOOKS,
                         ids=[f"{p}:{w}" for p, w, _ in HOOKS])
def test_declared_hook_is_not_a_stub(plugin, when, script) -> None:
    """A file that exists, is executable, and does nothing passes every check
    above while failing in the only way that matters."""
    body = [ln for ln in script.read_text().splitlines()[1:]
            if ln.strip() and not ln.strip().startswith("#")]
    assert len(body) > 3, (
        f"{plugin}'s {when} hook has {len(body)} substantive lines — a stub?")


# ── the review gate must SEE every hook ──

# test_every_hook_file_is_on_the_security_surface moved to a maintainer-side
# suite that does not ship: it reads tools/dev/security-surface-files.txt,
# absent from every distribution, so here it made the EXPORTED tree fail its
# own suite.
