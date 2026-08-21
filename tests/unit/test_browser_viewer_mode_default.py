"""The browser viewer's default mode is declared once and read in three places.

`plugins/browser/botainer-plugin.yaml` declares `viewer_mode.default`. Two
Python readers duplicate it:

    plugins/browser/hooks/start_viewer.py   the loud validator
    botainer/core/composition.py            picks the published port

They duplicate it because plugin hooks run standalone — no hook in this project
imports `botainer`, so a shared constant is not available. The manifest is
therefore the authority and these tests are what stop the copies drifting. That
is the same three-copy shape that let `botainer --version` print a number two
releases stale.

The default is `gateway`, and the direction matters. Under `legacy` the
container serves the noVNC page your browser executes; under `gateway` it
serves only an authenticated pixel stream and your own machine serves the page.
They are not equally safe, so the resolution is deliberately asymmetric:
**only an explicit `legacy` selects legacy.**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "plugins" / "browser" / "botainer-plugin.yaml"
HOOK = REPO / "plugins" / "browser" / "hooks" / "start_viewer.py"
COMPOSITION = REPO / "botainer" / "core" / "composition.py"


def _manifest_default() -> str:
    """The authority: `viewer_mode.default` from the plugin manifest."""
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            if sorted(node.get("enum", []) or []) == ["gateway", "legacy"]:
                return node.get("default")
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    default = walk(doc)
    assert default is not None, (
        "no viewer_mode enum with a default found in the browser manifest — "
        "did the schema move?"
    )
    return default


def test_the_manifest_default_is_gateway() -> None:
    assert _manifest_default() == "gateway", (
        "the browser viewer default is no longer gateway. legacy has the "
        "container serve the page your browser executes; making it the default "
        "again needs a deliberate decision, not a config edit."
    )


@pytest.mark.parametrize("source", [HOOK, COMPOSITION])
def test_each_reader_defaults_to_the_manifest_value(source: Path) -> None:
    expected = _manifest_default()
    body = source.read_text(encoding="utf-8")
    found = re.findall(r'\.get\(\s*"viewer_mode"\s*,\s*"([a-z]+)"\s*\)', body)
    assert found, (
        f"{source.relative_to(REPO)} no longer reads viewer_mode with a default "
        "— if the lookup changed shape, update this test to match."
    )
    for value in found:
        assert value == expected, (
            f"{source.relative_to(REPO)} defaults viewer_mode to {value!r}, but "
            f"the manifest declares {expected!r}. The manifest is the authority."
        )


def test_composition_resolves_toward_gateway() -> None:
    """Absent, wrong-typed and misspelled all resolve to the SAFER mode."""
    from botainer.core.composition import _browser_viewer_mode

    class _Cfg:
        def __init__(self, plugins):
            self.plugins = plugins

    assert _browser_viewer_mode(_Cfg({})) == "gateway", "absent should be gateway"
    assert _browser_viewer_mode(_Cfg({"browser": {}})) == "gateway", (
        "browser configured but no viewer_mode should be gateway")
    assert _browser_viewer_mode(_Cfg({"browser": "not-a-dict"})) == "gateway", (
        "a wrong-typed browser block must not buy the weaker posture")
    assert _browser_viewer_mode(_Cfg({"browser": {"viewer_mode": "gatway"}})) == "gateway", (
        "a MISSPELLING must not silently select legacy — that is fail-open")
    assert _browser_viewer_mode(_Cfg({"browser": {"viewer_mode": "legacy"}})) == "legacy", (
        "an explicit legacy must still be honoured")
    assert _browser_viewer_mode(_Cfg({"browser": {"viewer_mode": " LEGACY "}})) == "legacy", (
        "case and whitespace should not defeat an explicit choice")


def test_legacy_is_reachable_and_documented_as_deprecated() -> None:
    """Deprecating is not deleting. The escape hatch must still work, and the
    manifest must say what it is, so nobody re-adopts it by accident."""
    from botainer.core.composition import _browser_viewer_mode

    class _Cfg:
        plugins = {"browser": {"viewer_mode": "legacy"}}

    assert _browser_viewer_mode(_Cfg()) == "legacy"
    manifest = MANIFEST.read_text(encoding="utf-8").upper()
    assert "DEPRECATED" in manifest, (
        "the browser manifest no longer marks legacy as deprecated"
    )
