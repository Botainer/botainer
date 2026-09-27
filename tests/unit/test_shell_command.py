"""`botainer shell` runs a diagnostic command with the same cage as `start`.

The command replaces the agent, which must not run. Configuration, binds and
environment remain those of the composed session. Hooks run by default because
credential and module-software mounts are often what needs inspection.

Unlike fixed `selftest` probes, a shell command can investigate a new runtime
question. These tests check command replacement and hook composition."""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SHELL = REPO / "botainer" / "cli" / "shell.py"
START = REPO / "botainer" / "cli" / "start.py"


def _compose_kwargs(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compose_session"):
            return {k.arg for k in node.keywords if k.arg}
    raise AssertionError(f"no compose_session(...) in {path.name}")


def test_the_shell_launches_through_the_ONE_launch_path():
    """Shell must delegate to the shared launch path.

    Rendering argv and calling subprocess directly omits nested bind
    mountpoint preparation. Docker Desktop can then reject a mountpoint
    beneath the null-bind anchor even though argv-only tests pass.
    Check the call structure to prevent the two launch paths diverging.
    """
    assert _compose_kwargs(SHELL), "shell.py does not call compose_session"

    # AST, not substring: the file EXPLAINS the defect in a comment, and a
    # string scan cannot tell a warning about `subprocess.run` from a call to
    # it. A check that forbids discussing its own subject is a check people
    # route around by deleting the explanation.
    tree = ast.parse(SHELL.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)

    assert "launch" in called, (
        "shell.py does not CALL composition.launch — anything else is a second "
        "launch path and WILL drift (it already did once)"
    )
    assert "attach" in called, (
        "shell.py launches but never attaches. With the `nudge` plugin enabled "
        "(the default) the docker adapter wraps the run in `screen -dmS` — a "
        "DETACHED screen — so without attach() the command runs and its output "
        "goes to a pty nobody reads. That shipped once: `shell -c ls` printed "
        "nothing at all. `start` does launch() THEN attach(); so must this."
    )
    # `render_argv` is allowed for PREVIEW only (--print-argv); what must not
    # happen is executing it ourselves. So the ban is on the run/exec calls.
    for forbidden in ("run", "Popen", "check_output", "check_call"):
        assert forbidden not in called, (
            f"shell.py calls {forbidden}(): it is building or running its own "
            f"container command instead of going through composition.launch(). "
            f"That skipped _prepare_nested_bind_placeholders once already and "
            f"broke the command on every macOS host."
        )


def test_the_agent_is_replaced_not_appended():
    """`entrypoint_wraps` REPLACES what runs. If the command were appended, the
    agent would still start and the shell would never be reached."""
    src = SHELL.read_text(encoding="utf-8")
    assert "entrypoint_wraps" in src and "model_copy" in src, (
        "shell.py no longer swaps entrypoint_wraps — the agent may still run"
    )


def test_hooks_run_by_default_and_are_torn_down():
    """The hook-contributed binds are usually the thing under investigation, so
    they must be present. And anything a hook STARTS must be stopped: a leaked
    token-bearing sidecar for a session that never launched is worse than a
    stray file. Same discipline selftest uses."""
    src = SHELL.read_text(encoding="utf-8")
    assert "run_pre_session_hooks" in src and "run_host_pre_launch_hooks" in src
    assert "run_post_session_hooks" in src, (
        "shell.py starts hooks and never tears them down"
    )
    assert "finally:" in src, "teardown must be in a finally, not the happy path"


def test_a_refusing_hook_does_not_cost_you_the_shell():
    """Being unable to look is exactly when you most need to. A hook that
    refuses (no credential yet, a guarded-mode git config) must degrade the
    cage and SAY SO, not abort the command."""
    src = SHELL.read_text(encoding="utf-8")
    assert "on_hook_error" in src, (
        "shell.py lets one hook refusal abort the diagnostic — the same defect "
        "fixed for dry-run, reintroduced one command over"
    )


def test_it_says_the_agent_is_not_running():
    """A prompt inside a container that looks like a session, without saying
    the agent is absent, invites the reader to conclude the wrong thing about
    what they are looking at."""
    src = SHELL.read_text(encoding="utf-8")
    assert "NOT running" in src, (
        "the banner must state that the agent is not running"
    )


def test_the_swapped_command_really_reaches_the_container_argv(tmp_path, monkeypatch):
    """THE OBSERVATION, not another source assertion.

    Composes a real session, applies the same `entrypoint_wraps` swap the
    command does, and renders through the REAL DockerAdapter — which is pure,
    so this needs no daemon. Asserts the command is what the container runs and
    the agent binary is not.

    NOT via the mock runtime: `MockAdapter.render_argv` emits only
    `mock-runtime --session <id> <image>` and drops the command entirely, so a
    test routed through it would pass while the feature was broken. Finding
    that out is why this test exists in this shape.
    """
    import yaml

    from botainer.adapters.docker import DockerAdapter
    from botainer.core import composition
    from botainer.core import config as config_module
    from botainer.core import identity
    from botainer.plugins import builtin as plugin_builtin
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    cfg = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["plugins_enabled"] = ["git"]
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    spec = composition.compose_session(proj, runtime_choice="mock",
                                       identity_accept=False)
    # The adapter refuses a spec whose runtime is not its own, so retarget it.
    # Composing under "mock" avoids needing a docker daemon to compose at all.
    spec = spec.model_copy(update={"runtime": "docker"})
    swapped = spec.model_copy(
        update={"entrypoint_wraps": (("/bin/bash", "-lc", "CAGE_PROBE_MARKER"),)})

    argv = DockerAdapter().render_argv(swapped)
    flat = " ".join(argv)

    assert "CAGE_PROBE_MARKER" in flat, (
        f"the swapped command never reached the container argv:\n{argv}"
    )
    # The agent must not be the thing executed. `claude` appears in the IMAGE
    # name, so check the executed tail rather than the whole line.
    tail = flat.split("botainer/agent-claude")[-1]
    assert "CAGE_PROBE_MARKER" in tail, (
        f"the command does not follow the image, so it is not what runs: {argv}"
    )

    # And the cage is otherwise the same one `start` would build.
    plain = DockerAdapter().render_argv(spec)
    def _binds(a):
        return sorted(x for x in a if x.startswith("type=bind"))
    assert _binds(argv) == _binds(plain), (
        "the shell cage has different binds from the session cage, so what you "
        "can reach here is not what the agent could reach"
    )


def test_the_cage_is_identical_with_nudge_stripped(tmp_path, monkeypatch):
    """`shell` drops `nudge` from the RUN spec so the adapter does not wrap the
    container in a detached `screen`. That is only legitimate if it changes
    nothing about the CAGE — otherwise you would be inspecting a different
    container from the one the agent gets, which is worse than not looking.

    Checkable, not assumed: `plugins/nudge/` has a manifest and a README and no
    hooks, so it contributes no bind and no env. This pins the consequence.
    """
    import yaml

    from botainer.adapters.docker import DockerAdapter
    from botainer.core import composition
    from botainer.core import config as config_module
    from botainer.core import identity
    from botainer.plugins import builtin as plugin_builtin
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    cfg = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["plugins_enabled"] = ["git", "nudge"]
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    spec = composition.compose_session(proj, runtime_choice="mock",
                                       identity_accept=False)
    spec = spec.model_copy(update={"runtime": "docker"})
    assert "nudge" in spec.plugins_enabled, "fixture did not enable nudge"

    stripped = spec.model_copy(update={
        "plugins_enabled": tuple(p for p in spec.plugins_enabled if p != "nudge"),
        "entrypoint_wraps": (("/bin/bash", "-lc", "true"),),
    })

    def binds(a):
        return sorted(x for x in a if x.startswith("type=bind"))

    def envs(a):
        return sorted(a[i + 1] for i, x in enumerate(a) if x == "-e")

    full = DockerAdapter().render_argv(spec)
    less = DockerAdapter().render_argv(stripped)

    assert binds(less) == binds(full), (
        "dropping nudge changed the BINDS, so `botainer shell` would show you "
        "a different cage from the one the agent gets"
    )
    assert envs(less) == envs(full), (
        "dropping nudge changed the ENV, so the cage is not the same one"
    )
