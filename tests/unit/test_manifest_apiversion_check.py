"""Tasks #254 + #271: load_manifest enforces apiVersion + botainer_min_version.

Was: PluginManifest accepted any apiVersion / any botainer_min_version.
load_manifest didn't re-validate them.

Now: load_manifest refuses if apiVersion is not in the supported set,
or if botainer_min_version > BOTAINER_LAUNCHER_VERSION.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from botainer.core.refusal import Refused
from botainer.plugins.manifest import load_manifest


def _write(d: Path, content: str) -> None:
    (d / "botainer-plugin.yaml").write_text(content, encoding="utf-8")


def test_load_manifest_apiversion_unknown_refused(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v99
        name: test-plugin
        version: 0.1.0
        kind: agent
    """))
    with pytest.raises(Refused) as exc:
        load_manifest(tmp_path)
    assert "apiVersion" in str(exc.value)


def test_load_manifest_apiversion_supported_accepted(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        kind: agent
    """))
    m = load_manifest(tmp_path)
    assert m.apiVersion == "botainer-plugin-v1"


def test_load_manifest_botainer_min_version_too_high_refused(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        botainer_min_version: 99.0.0
        kind: agent
    """))
    with pytest.raises(Refused) as exc:
        load_manifest(tmp_path)
    assert "requires botainer" in str(exc.value)


def test_load_manifest_botainer_min_version_compatible_accepted(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        botainer_min_version: 0.1.0
        kind: agent
    """))
    m = load_manifest(tmp_path)
    assert m.botainer_min_version == "0.1.0"


# ───── AUDIT (MEDIUM): manifest field validators ─────


def test_runtimes_empty_refused(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        kind: agent
        runtimes: []
    """))
    with pytest.raises(Refused) as exc:
        load_manifest(tmp_path)
    assert "runtimes" in str(exc.value)


def test_runtimes_unknown_value_refused(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        kind: agent
        runtimes: [docker, podman]
    """))
    with pytest.raises(Refused):
        load_manifest(tmp_path)


def test_command_name_invalid_refused(tmp_path: Path) -> None:
    _write(tmp_path, dedent("""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        kind: agent
        commands:
          - name: "Bad Name!"
            script: hooks/x.py
    """))
    with pytest.raises(Refused):
        load_manifest(tmp_path)


@pytest.mark.parametrize("bad", ["../escape/Dockerfile", "/etc/passwd", "a/../../b"])
def test_imagedecl_path_traversal_refused(tmp_path: Path, bad: str) -> None:
    """AUDIT (MEDIUM): cli/image.py joins plugin_dir / dockerfile and
    its docstring claimed a '..' guard that didn't exist; the ImageDecl path
    validator now makes that true."""
    _write(tmp_path, dedent(f"""\
        apiVersion: botainer-plugin-v1
        name: test-plugin
        version: 0.1.0
        kind: agent
        image:
          source: dockerfile
          dockerfile: "{bad}"
    """))
    with pytest.raises(Refused):
        load_manifest(tmp_path)
