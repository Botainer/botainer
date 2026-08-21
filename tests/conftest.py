"""Shared pytest fixtures + test helpers.

The five-tier test plan (see DN-029):
- Tier 1: unit (pure logic) — runs everywhere
- Tier 2: snapshot / schema (textual) — runs everywhere
- Tier 3: mocked integration (adapter playback) — runs everywhere
- Tier 4: real Docker / Apptainer / Slurm — marker-skipped in this container
- Tier 5: hostile / property tests — runs everywhere
"""

from __future__ import annotations

from pathlib import Path

# A syntactically-valid placeholder image digest for tests. NOT all-zeros
# (that's the launcher-refuses placeholder); this one is "aaa..." so tests
# can compose specs without triggering the refuse-on-missing-image guard.
TEST_IMAGE_REF = "test-runtime/agent:0.1@sha256:" + ("a" * 64)


def append_image_to_config(project_root: Path, image_ref: str = TEST_IMAGE_REF) -> None:
    """Append a valid `image:` line to a freshly-initialized config.yaml.

    Per the refuse-on-missing-image guard, tests that compose sessions need
    a real image in their config. Use this helper after `write_initial_config`
    or after `bot1 init` in CLI tests.
    """
    cfg = project_root / ".botainer" / "config.yaml"
    if cfg.exists():
        cfg.write_text(cfg.read_text() + f"\nimage: {image_ref}\n")
