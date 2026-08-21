"""S3 (security audit): the host must refuse to execute a plugin hook
that lives inside a directory bound WRITABLE into the container.

THE INVARIANT: code the HOST executes must not live where the CAGED AGENT can
write. This is structural — it does not inspect the script for malice, it refuses
the location outright.

The concrete hole: in an EDITABLE install, list_installed() overlays the clone's
plugins/ and source WINS. When the project root IS the botainer clone (the
documented develop-botainer-inside-botainer workflow) that tree is bound rw at
/workspace, so a prompt-injected agent could rewrite
plugins/<x>/hooks/pre_session.py and the launcher would run it ON THE HOST as the
user at the next `botainer start`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.refusal import Refused
from botainer.plugins.hooks import refuse_agent_writable_hook, run_hook


def _make_hook(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\nprint('{}')\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def test_refuses_hook_inside_a_writable_bind_source(tmp_path: Path) -> None:
    project = tmp_path / "clone"            # bound rw at /workspace
    hook = _make_hook(project / "plugins" / "git" / "hooks" / "pre_session.py")
    with pytest.raises(Refused) as exc:
        refuse_agent_writable_hook(hook, [project], plugin_name="git")
    msg = str(exc.value)
    assert "REFUSING to run hook" in msg
    assert "bound WRITABLE" in msg
    assert "Fix:" in msg                     # must tell the user what to do


def test_allows_hook_outside_every_writable_bind(tmp_path: Path) -> None:
    """The ordinary user case: ~/.botainer/plugins/ is not inside any bind."""
    project = tmp_path / "myproject"
    project.mkdir()
    hook = _make_hook(tmp_path / "dot-botainer" / "plugins" / "git" / "hooks" / "pre_session.py")
    refuse_agent_writable_hook(hook, [project], plugin_name="git")   # must not raise


def test_symlink_cannot_dodge_the_check(tmp_path: Path) -> None:
    """A hook path that RESOLVES into the writable tree is still refused —
    otherwise the check would be a name filter rather than a containment rule."""
    project = tmp_path / "clone"
    real = _make_hook(project / "plugins" / "git" / "hooks" / "pre_session.py")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "pre_session.py"
    link.symlink_to(real)
    with pytest.raises(Refused):
        refuse_agent_writable_hook(link, [project], plugin_name="git")


def test_run_hook_enforces_it_at_the_execution_point(tmp_path: Path) -> None:
    """Not just the helper — the single place hooks are actually executed."""
    project = tmp_path / "clone"
    hook = _make_hook(project / "plugins" / "x" / "hooks" / "pre_session.py")
    with pytest.raises(Refused):
        run_hook(plugin_name="x", hook_when="pre_session", script_path=hook,
                 env={}, agent_writable_roots=[project])


def test_writable_roots_are_derived_from_the_mount_plan() -> None:
    """The roots must come from what is actually bound writable, not a guess."""
    import types

    from botainer.core.composition import _agent_writable_bind_sources

    spec = types.SimpleNamespace(mount_plan=types.SimpleNamespace(binds=[
        types.SimpleNamespace(source="/host/proj", mode="rw"),
        types.SimpleNamespace(source="/host/ro-thing", mode="ro"),
        types.SimpleNamespace(source="/host/sock", mode="unix-socket"),
    ]))
    got = {str(p) for p in _agent_writable_bind_sources(spec)}
    assert got == {"/host/proj", "/host/sock"}      # ro is NOT writable
