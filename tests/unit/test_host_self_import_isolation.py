"""Real Python import negative controls for native host-child argv prefixes.

The source lists are the actual call-site expressions, not duplicated intended
commands. -S is a TEST-ONLY addition that hides installed packages: the protected
child must report the module unavailable, whereas removing -I runs the planted
temporary package. This proves exclusion without requiring or launching an
installed broker/CLI. Installed-child source identity is a separate integration
requirement; these tests do not manufacture an installation with PYTHONPATH.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SITES = (
    ("botainer/cli/hpc.py", 0),
    ("botainer/cli/hpc.py", 1),
    ("botainer/cli/start.py", 0),
    ("botainer/cli/nudge.py", 0),
    ("botainer/hpc/autodispatch.py", 0),
    ("plugins/agent-claude-broker/hooks/start_broker.py", 0),
    ("plugins/agent-codex-broker/hooks/start_broker.py", 0),
)
MODULES = {"botainer.cli.main", "botainer.broker.daemon_main"}


def _prefix(relative: str, occurrence: int) -> list[str]:
    found = []
    for node in ast.walk(ast.parse((REPO / relative).read_text())):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        first = node.elts[0]
        if not (isinstance(first, ast.Attribute) and first.attr == "executable"
                and isinstance(first.value, ast.Name) and first.value.id == "sys"):
            continue
        for index, item in enumerate(node.elts[1:], start=1):
            if isinstance(item, ast.Constant) and item.value in MODULES:
                # Only literal interpreter options/module are interpreted.
                options = [ast.literal_eval(arg) for arg in node.elts[1:index + 1]]
                assert all(isinstance(arg, str) for arg in options)
                found.append((node.lineno, [sys.executable, *options]))
                break
    found.sort()
    assert len(found) == (2 if relative == "botainer/cli/hpc.py" else 1)
    return found[occurrence][1]


def _plant(root: Path) -> None:
    for name in ("botainer", "botainer/cli", "botainer/broker"):
        part = root / name
        part.mkdir(parents=True, exist_ok=True)
        (part / "__init__.py").write_text("")
    script = (
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['IMPORT_SENTINEL']).write_text('project code ran')\n"
        "raise SystemExit(41)\n"
    )
    for name in ("botainer/cli/main.py", "botainer/broker/daemon_main.py"):
        (root / name).write_text(script)


@pytest.mark.parametrize("relative,occurrence", SITES)
@pytest.mark.parametrize("vector", ["cwd", "pythonpath"])
def test_native_child_excludes_project_imports(tmp_path, relative, occurrence, vector):
    prefix = _prefix(relative, occurrence)
    project, other, home = (tmp_path / name for name in ("project", "other", "home"))
    for directory in (project, other, home):
        directory.mkdir()
    _plant(project)
    marker = tmp_path / "marker"
    env = {"HOME": str(home), "PATH": os.defpath,
           "IMPORT_SENTINEL": str(marker), "PYTHONDONTWRITEBYTECODE": "1"}
    cwd = project if vector == "cwd" else other
    if vector == "pythonpath":
        env["PYTHONPATH"] = str(project)
    # An explicit negative control: the same call-site prefix without isolation
    # must actually execute the planted module. -B bounds test bytecode writes.
    unprotected = [arg for arg in prefix[1:] if arg not in {"-I", "-B"}]
    bad = subprocess.run([sys.executable, "-S", "-B", *unprotected, "--help"],
                         cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
    assert bad.returncode == 41, bad.stderr
    assert marker.read_text() == "project code ran"
    marker.unlink()
    good = subprocess.run([prefix[0], "-S", *prefix[1:], "--help"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
    assert good.returncode != 41, good.stderr
    assert not marker.exists(), "the native child imported project-controlled code"
    assert good.returncode == 1 and "No module named" in good.stderr
    assert "-I" in prefix and "-B" in prefix


def test_isolated_child_keeps_cwd_and_ordinary_environment(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("synthetic project config\n")
    prefix = _prefix("botainer/cli/nudge.py", 0)
    options = prefix[1:prefix.index("-m")]
    code = (
        "import json,os,pathlib,sys; print(json.dumps({"
        "'cwd':str(pathlib.Path.cwd()),'config':pathlib.Path('config.yaml').read_text(),"
        "'state':os.environ['MY_BOTAINER'],'isolated':sys.flags.isolated,"
        "'bytecode':sys.dont_write_bytecode}))"
    )
    result = subprocess.run([prefix[0], "-S", *options, "-c", code],
        cwd=tmp_path, env={"MY_BOTAINER": str(tmp_path / "state")},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert facts == {"cwd": str(tmp_path), "config": config.read_text(),
        "state": str(tmp_path / "state"), "isolated": 1, "bytecode": True}
