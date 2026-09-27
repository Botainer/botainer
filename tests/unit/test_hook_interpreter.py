"""Regression: python plugin hooks run with botainer's OWN interpreter
(sys.executable), not their `#!/usr/bin/env python3` shebang.

A system Python selected by a shebang may lack dependencies such as yaml.
Using sys.executable keeps hook imports aligned with the launcher environment."""

from __future__ import annotations

import os
import sys

from botainer.plugins import hooks


def test_python_hook_runs_with_sys_executable(tmp_path, monkeypatch) -> None:
    hook = tmp_path / "h.py"
    hook.write_text("#!/usr/bin/env python3\nprint('{}')\n")
    hook.chmod(0o755)

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return R()

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks.run_hook(plugin_name="p", hook_when="pre_session", script_path=hook,
                   env={}, agent_writable_roots=[])
    assert captured["cmd"][0] == sys.executable, captured["cmd"]
    assert captured["cmd"][1] == str(hook)


def test_non_python_hook_runs_via_shebang(tmp_path, monkeypatch) -> None:
    hook = tmp_path / "h.sh"
    hook.write_text("#!/bin/sh\necho '{}'\n")
    hook.chmod(0o755)

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return R()

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks.run_hook(plugin_name="p", hook_when="pre_session", script_path=hook,
                   env={}, agent_writable_roots=[])
    assert captured["cmd"] == [str(hook)]  # shebang path for non-.py
