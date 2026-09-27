"""A repo you don't trust must not be able to suppress its own preview.

THE HAZARD. `run_hook` raises `Refused` on any non-zero exit, and neither hook
runner had a per-hook boundary, so the FIRST hook failure aborted the whole
call and printed no plan. That is defensible for `start` — it cannot launch a
session missing a plugin's binds. For a PREVIEW it inverts the purpose.

And it is reachable from outside. The git plugin refuses, correctly, on a repo
whose `.git/config` carries `core.sshCommand` — verified against the real hook:

    git: refusing — .../.git/config sets ['core.sshcommand'], which run code
    on the host when you use git ... RC=2

So one line in a cloned repo turned `dry-run --include-hooks` into a command
that prints nothing — for exactly the repo you were inspecting BECAUSE you did
not trust it. A denial-of-inspection primitive the untrusted side controls.

WHAT IS AND IS NOT RECOVERABLE. Only "the hook did not run" may be collected.
A REFUSED CONTRIBUTION — an out-of-envelope bind, a denied env var,
`command_append` — is a decision that the contribution is not allowed, and
softening it would let a preview print a reassuring plan for a session the
launcher would refuse. Pinned below.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from botainer.core import composition
from botainer.core.refusal import RefusalCategory, Refused

REPO = pathlib.Path(__file__).resolve().parents[2]


def _refused(category):
    return Refused(category, "boom")


def test_a_hook_that_did_not_run_is_recoverable_only_with_a_collector():
    """The default — and what `start` uses — makes every refusal fatal."""
    exc = _refused(RefusalCategory.PLUGIN_HOOK_FAILED)
    assert composition._hook_failure_is_recoverable(exc, None) is False, (
        "with no collector, a hook failure MUST end the call — that is the "
        "behaviour a real launch requires and the pre-existing default"
    )
    assert composition._hook_failure_is_recoverable(exc, list().append) is True


def test_a_refused_contribution_is_never_recoverable_even_with_a_collector():
    """The load-bearing half. If this ever returns True, a preview can print a
    plan that omits a bind the launcher REFUSED, which is worse than printing
    nothing — it is printing something false."""
    sink = list().append
    for category in (
        RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
        RefusalCategory.ENV_VAR_DENIED,
    ):
        assert composition._hook_failure_is_recoverable(
            _refused(category), sink
        ) is False, (
            f"{category} was treated as recoverable. A refused CONTRIBUTION is "
            f"a decision that it is not allowed; only a hook that failed to RUN "
            f"may be reported-and-skipped."
        )


def _compose_kwargs(path: pathlib.Path, func: str) -> list[dict]:
    """Every call to `func` in `path`, as {kwarg: node-type}."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == func):
            out.append({k.arg: type(k.value).__name__ for k in node.keywords if k.arg})
    return out


@pytest.mark.parametrize("runner", [
    "run_pre_session_hooks", "run_host_pre_launch_hooks",
])
def test_start_never_collects_hook_failures(runner):
    """THE STRUCTURAL GUARD, and the reason the parameter defaults to fatal.

    `on_hook_error` is optional, so a future caller who forgets it gets the
    REFUSING behaviour — the safe direction. The unsafe direction is a real
    launch that swallows a hook failure and starts a container missing that
    plugin's binds, and nothing but this test stops someone adding it to
    `start` for symmetry with the previews.
    """
    calls = _compose_kwargs(REPO / "botainer" / "cli" / "start.py", runner)
    assert calls, f"start.py no longer calls {runner} — this guard is vacuous"
    for kwargs in calls:
        assert "on_hook_error" not in kwargs, (
            f"start.py passes on_hook_error to {runner}. `start` must REFUSE on "
            f"a hook failure: continuing would launch a session missing that "
            f"plugin's binds or env. Collecting is for PREVIEWS only."
        )


@pytest.mark.parametrize("runner", [
    "run_pre_session_hooks", "run_host_pre_launch_hooks",
])
def test_the_preview_does_collect(runner):
    """The other direction: dry-run must actually use the boundary, or the
    hostile-repo case is still live and the tests above prove nothing about
    the shipped command."""
    calls = _compose_kwargs(REPO / "botainer" / "cli" / "dry_run.py", runner)
    assert calls, f"dry_run.py no longer calls {runner}"
    assert any("on_hook_error" in k for k in calls), (
        f"dry_run.py calls {runner} without on_hook_error, so one hook refusal "
        f"still suppresses the whole preview — the defect this file exists for."
    )


def test_the_hostile_repo_still_gets_a_plan(tmp_path, monkeypatch):
    """END TO END through the real CLI, because the three tests above are all
    about the mechanism and none of them prove the shipped command changed.

    Measured A/B on this fix, same repo, same command:

        without  exit 5, ZERO stdout — no plan at all
        with     exit 0, full argv + 7 binds, and stderr naming the hook

    The repo carries the one line an untrusted clone would ship. The git
    plugin's refusal is CORRECT and unchanged; what changed is that refusing
    to run no longer costs you the whole preview.
    """
    import subprocess

    import yaml
    from click.testing import CliRunner

    from botainer.cli.dry_run import dry_run
    from botainer.core import config as config_module
    from botainer.core import identity
    from botainer.plugins import builtin as plugin_builtin
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    subprocess.run(["git", "-C", str(proj), "config",
                    "core.sshCommand", "ssh -o StrictHostKeyChecking=no"],
                   check=True)
    config_module.write_initial_config(proj, agent="claude", force=False)
    cfg = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["plugins_enabled"] = ["git"]     # git alone, to isolate the refusal
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    monkeypatch.chdir(proj)

    res = CliRunner().invoke(dry_run, ["--include-hooks"])
    # The plan goes to stdout, the INCOMPLETE note to stderr; assert on both so
    # a change that moves one stream does not silently weaken this.
    out = res.stdout + getattr(res, "stderr", "")

    assert res.exit_code == 0, (
        f"the preview died on a hostile repo (exit {res.exit_code}). One line "
        f"in .git/config must not suppress inspection of the repo you added it "
        f"to inspect.\n{out}"
    )
    assert "exact argv" in out and "# Binds:" in out, (
        f"exit 0 but no plan was rendered — the command succeeded at printing "
        f"nothing, which is the failure this test exists for.\n{out}"
    )
    assert "INCOMPLETE" in out and "git (pre_session)" in out, (
        f"the plan was printed but does not say it is missing the git plugin's "
        f"contributions. A partial plan presented as complete is worse than the "
        f"refusal it replaced.\n{out}"
    )
    assert "core.sshcommand" in out.lower(), (
        f"the hook's own reason was dropped, so the reader cannot tell whether "
        f"the gap matters or how to fix it.\n{out}"
    )
