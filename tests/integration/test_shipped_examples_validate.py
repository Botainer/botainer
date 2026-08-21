"""Every shipped example YAML must round-trip through ProjectConfig.

Codex review workflow-UX HIGH #4 caught examples/hpc-slurm.yaml
failing `ProjectConfig.model_validate` (plugins.hpc-launcher was
null because all entries below were commented out). This test
prevents that class of regression for ALL bundled examples.

If you add a new file to examples/ that ISN'T a ProjectConfig
(e.g., a partial config snippet for docs), name it with a
`.partial.yaml` suffix; the glob below skips those.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from botainer.core.config import ProjectConfig

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
EXAMPLES = sorted(
    p for p in EXAMPLES_DIR.glob("*.yaml")
    if not p.name.endswith(".partial.yaml")
)


@pytest.mark.parametrize("example_path", EXAMPLES, ids=lambda p: p.name)
def test_shipped_example_validates(example_path: Path) -> None:
    """Each examples/*.yaml must parse as a ProjectConfig."""
    with example_path.open() as f:
        data = yaml.safe_load(f)
    assert data is not None, f"{example_path.name} is empty"
    ProjectConfig.model_validate(data)
