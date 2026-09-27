"""The .sif filename convention must agree across all sites.

The .sif-naming follow-up (GA-blocker, HPC parity; internal design note
DN-036): `botainer hpc build`
once wrote `<plugin>.sif` while `botainer image build` wrote
`botainer-<plugin>.sif` and the hpc-launcher resolver expected the
prefixed form. The drift meant a freshly-built .sif sat at a path the
launcher never looked at, so rebuilding did not resolve the launch failure.

The two botainer-side writers now route through one helper
(`StatePaths.apptainer_sif_path`). The hpc-launcher host_helper is
deliberately standalone (no botainer import) and keeps its own copy of
the convention, so it CAN'T share the helper — this test is the bridge
that asserts the two stay in lockstep.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_sifcheck", HPC_HOST_HELPER / "_common.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_sifcheck"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_helper_uses_botainer_prefix(tmp_path: Path) -> None:
    from botainer.state.dir import StatePaths

    paths = StatePaths(root=tmp_path)
    sif = paths.apptainer_sif_path("agent-claude")
    assert sif == tmp_path / "images" / "botainer-agent-claude.sif"
    assert sif.name == "botainer-agent-claude.sif"
    # The unprefixed form is exactly the bug; assert it's NOT produced.
    assert sif.name != "agent-claude.sif"


def test_resolver_conventional_path_matches_helper(tmp_path: Path) -> None:
    """The hpc-launcher resolver's conventional fallback path (which it
    uses when installed.lock has no recorded digest) must point at the
    same basename the botainer-side writers produce, for each agent."""
    from botainer.state.dir import StatePaths

    common = _load_common()
    paths = StatePaths(root=tmp_path)
    state_root = tmp_path

    for agent in ("claude", "codex"):
        plugin_name = f"agent-{agent}"
        # botainer-side: what `image build` / `hpc build` write.
        written = paths.apptainer_sif_path(plugin_name)
        # plugin-side: what the resolver looks for (conventional path,
        # no installed.lock entry present in tmp_path → falls through to
        # the conventional branch).
        resolved = Path(
            common._resolve_apptainer_image({}, tmp_path, state_root, agent)
        )
        assert resolved.name == written.name, (
            f"agent-{agent}: hpc-launcher resolver expects {resolved.name!r} "
            f"but the builders write {written.name!r} — the .sif naming has "
            f"drifted again. Both must be botainer-agent-{agent}.sif."
        )
        assert resolved == written, (
            f"agent-{agent}: full path mismatch resolver={resolved} "
            f"writer={written}"
        )
