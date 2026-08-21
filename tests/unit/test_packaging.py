"""AUDIT (H8): the built wheel must ship the bundled plugin trees
and cluster profiles at the install-time locations the discovery code prefers.

Background: a bare `[tool.hatch.build.targets.wheel] include = ["plugins/**/*",
...]` list is SILENTLY IGNORED by hatchling because those paths live outside the
selected `botainer` package — the wheel shipped only `botainer/*`, so a normal
(non-editable) `pip install` (the default of both shipped installers) produced
find_builtin_plugins_root()==None / list_bundled()==[] and `botainer setup`
aborted with "Reinstall botainer". The fix maps the trees into the package via
`force-include`. These tests pin that mechanism (fast, no wheel build) so the
regression cannot silently return.
"""

from __future__ import annotations

import sys
import tomllib

import pytest
from pathlib import Path

# pyproject declares `requires-python = ">=3.10"` and the PACKAGE honours it —
# only this file needs `tomllib`, which arrived in 3.11. Skipping on 3.10 keeps
# the declared floor genuinely runnable instead of quietly requiring 3.11 of
# everyone. If the floor moves to 3.11, delete this.
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="tomllib is 3.11+; botainer itself supports 3.10",
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _wheel_target() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]


def test_wheel_force_includes_plugins_and_profiles() -> None:
    """force-include must map the bundled trees to the EXACT package-relative
    locations discovery prefers: botainer/plugins/builtin.find_builtin_plugins_root
    → <pkg>/_builtin_plugins, and state/cluster_profile._bundled_profiles_root
    → <pkg>/cluster_profiles."""
    wheel = _wheel_target()
    fi = wheel.get("force-include")
    assert fi, (
        "wheel target has no [tool.hatch.build.targets.wheel.force-include] — a "
        "bare `include` list does NOT ship paths outside the botainer package "
        "(H8): the wheel would have zero bundled plugins/profiles."
    )
    assert fi.get("plugins") == "botainer/_builtin_plugins", (
        f"plugins must force-include to botainer/_builtin_plugins, got {fi.get('plugins')!r}"
    )
    assert fi.get("cluster_profiles") == "botainer/cluster_profiles", (
        f"cluster_profiles must force-include to botainer/cluster_profiles, "
        f"got {fi.get('cluster_profiles')!r}"
    )


def test_force_include_sources_exist() -> None:
    """The force-include SOURCES must actually exist in the repo (a typo'd
    source silently ships nothing, reintroducing H8).

    Existence, not dir-ness: hatchling force-includes files too, and the
    licensing fix (§4bk) adds `THIRD-PARTY-LICENSES.md` here — a single file
    that must ride along with the MPL-2.0 code plugins/ carries. A typo'd
    filename is exactly as silent as a typo'd dirname, which is what this
    guards."""
    fi = _wheel_target()["force-include"]
    for src in fi:
        assert (REPO_ROOT / src).exists(), f"force-include source {src!r} does not exist"


def test_discovery_targets_match_force_include() -> None:
    """Guard against drift between the packaging map and the discovery code: if
    someone renames the install-time dir in either place, this fails. We assert
    the discovery code references the same package-relative names force-include
    writes to."""
    fi = _wheel_target()["force-include"]
    builtin_src = (REPO_ROOT / "botainer" / "plugins" / "builtin.py").read_text()
    profile_src = (REPO_ROOT / "botainer" / "state" / "cluster_profile.py").read_text()
    # force-include targets are "botainer/<name>"; discovery joins pkg_root / "<name>".
    assert '"_builtin_plugins"' in builtin_src
    assert fi["plugins"].endswith("/_builtin_plugins")
    assert '"cluster_profiles"' in profile_src
    assert fi["cluster_profiles"].endswith("/cluster_profiles")
