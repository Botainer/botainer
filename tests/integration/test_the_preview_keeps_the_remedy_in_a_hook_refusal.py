"""A preview that names a failed hook must keep the hook's REMEDY, not just its headline.

WHAT `dry-run --include-hooks` DID. It collects hooks that refused to RUN and
lists them, so a partial plan says what is missing from it — that boundary is
`test_preview_survives_hook_refusal.py`'s subject and it works. But the listing
rendered only the first line of each refusal.

MEASURED END TO END, on the shipped default-path failure rather than a
constructed one. `agent-claude-shared` on a project with no shared credential yet
exits 2 with three lines, and the preview printed:

    # INCOMPLETE: 1 hook(s) did not run, so any env or binds they contribute …
    #   agent-claude-shared (pre_session): … no shared credential at …/…
    # `start` would REFUSE on the same failure rather than launch a session …

`Run: botainer auth login --shared --agent claude` was dropped. `start` prints
it — the refusal handler renders the whole message — so the two commands
described one failure differently, and the surface that lost the fix was the one
you run BECAUSE you are trying to find out what is wrong.

THE WORKAROUND THAT HID IT. This was noticed while fixing an unrelated warning
and worked around there by moving that particular hook's remedy onto line 1.
That repairs one message and leaves the surface broken for every other
multi-line hook, of which the bundled plugins have many.

EVERY LINE STAYS `#`-PREFIXED, and that is asserted rather than assumed. The
block is rendered to be copy-pasteable, and the message text is plugin-authored:
an unprefixed continuation line would be a shell command rather than a comment.

BOTH DIRECTIONS. A single-line refusal must NOT sprout a continuation, or the
fix has traded one wrong rendering for another — and the single-line case is the
one the existing sibling test already depends on.
"""
from __future__ import annotations

import subprocess

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.dry_run import dry_run
from botainer.core import config as config_module
from botainer.core import identity
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir


@pytest.fixture
def preview(tmp_path, monkeypatch):
    """A real project, previewed through the real CLI with hooks run."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    config_module.write_initial_config(proj, agent="claude", force=False)

    def _run(plugin: str, *, git_config: tuple[str, str] | None = None):
        if git_config is not None:
            subprocess.run(["git", "-C", str(proj), "config", *git_config],
                           check=True)
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        d["plugins_enabled"] = [plugin]
        cfg.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
        identity.init_project(proj, agent="claude", force=True,
                              non_interactive=True)
        monkeypatch.chdir(proj)
        res = CliRunner().invoke(dry_run, ["--include-hooks"])
        return res, res.stdout + getattr(res, "stderr", "")

    return _run


def test_the_remedy_below_line_one_reaches_the_reader(preview):
    """THE DEFECT. Shared mode with no shared credential is the default path's
    first failure, and its second line is the command that fixes it."""
    res, out = preview("agent-claude-shared")

    assert res.exit_code == 0, f"the preview died instead of reporting:\n{out}"
    assert "INCOMPLETE" in out and "agent-claude-shared (pre_session)" in out, (
        f"the refusal was not reported at all, so this test is measuring "
        f"something other than what it claims:\n{out}")
    assert "botainer auth login --shared" in out, (
        f"the hook named a remedy on its second line and the preview dropped "
        f"it, so the reader is told a hook failed and not how to fix it:\n{out}")


def test_every_rendered_line_is_still_a_shell_comment(preview):
    """The property the block's format exists for.

    `print_dry_run` renders a copy-pasteable block. The refusal text is written
    by a PLUGIN, so a continuation line that lost its `#` would be pasted as a
    command rather than read as a comment.
    """
    _, out = preview("agent-claude-shared")

    loose = [ln for ln in out.splitlines()
             if ln.strip() and not ln.startswith("#") and not ln.startswith("  ")]
    assert not loose, (
        f"these lines are neither a `#` comment nor an indented argv line, so "
        f"pasting the block would execute them: {loose!r}")


def test_a_single_line_refusal_gains_no_continuation(preview):
    """OPPOSITE DIRECTION, and the case the sibling test depends on.

    The git plugin refuses a repo whose `.git/config` carries
    `core.sshCommand`, in one long line. It must render as exactly one line —
    a fix that appended an empty continuation to every refusal would pass the
    test above while making the common case worse.
    """
    _, out = preview("git", git_config=("core.sshCommand",
                                        "ssh -o StrictHostKeyChecking=no"))

    assert "core.sshcommand" in out.lower(), (
        f"the git refusal did not fire, so this proves nothing:\n{out}")
    body = out.split("INCOMPLETE", 1)[1]
    named = [ln for ln in body.splitlines() if ln.startswith("#   git (")]
    conts = [ln for ln in body.splitlines() if ln.startswith("#     ")]
    assert len(named) == 1, f"expected one named refusal line, got {named!r}"
    assert not conts, (
        f"a one-line refusal grew continuation lines: {conts!r}")
