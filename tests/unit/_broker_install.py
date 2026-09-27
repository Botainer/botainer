"""Verify which installed source a real broker subprocess will execute.

Real-daemon tests require the test checkout installed in their interpreter.
PYTHONPATH and a parent-only sys.path override cannot select an isolated child.
This check fails explicitly rather than exercising another installed version.
It creates/installs nothing; pure call-boundary tests need no installation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def checked_hook_python(repo: Path) -> str:
    expected = (repo / "botainer/__init__.py").resolve()
    probe = subprocess.run(
        [sys.executable, "-I", "-B", "-c",
         "from pathlib import Path; import botainer; print(Path(botainer.__file__).resolve())"],
        env={name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG")
             if name in os.environ},
        capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0 and probe.stdout.strip() == str(expected), (
        "Real broker subprocess tests require this checkout installed in the "
        "selected interpreter. The isolated child imported another source (or "
        "could not import Botainer). Use an existing correctly prepared test "
        "environment; PYTHONPATH is not an isolated-child installation. "
        f"Observed exit: {probe.returncode}; source: {probe.stdout.strip()!r}; "
        f"expected: {str(expected)!r}."
    )
    return sys.executable
