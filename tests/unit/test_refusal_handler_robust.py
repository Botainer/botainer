"""Task #279: @handle_refusals must not IndexError when Refused has no message.

The decorator previously did `exc.args[0]` unconditionally; a Refused
constructed as `Refused(category)` (no message) has args = () so the
decorator itself raised IndexError, defeating the entire CLI refusal
output contract.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from botainer.cli._refusal_handler import handle_refusals
from botainer.core.refusal import RefusalCategory, Refused


def test_refusal_handler_no_message() -> None:
    """Refused with no message must not IndexError."""

    @click.command()
    @handle_refusals
    def cmd() -> None:
        raise Refused(RefusalCategory.PLUGIN_HOOK_FAILED)

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 5  # DN-028 §9: plugin failure (AUDIT)
    assert "refused: plugin-hook-failed" in result.output
    # Should NOT contain a traceback
    assert "IndexError" not in result.output
    assert "Traceback" not in result.output


def test_refusal_handler_with_message() -> None:
    """Refused with a message includes it after the category."""

    @click.command()
    @handle_refusals
    def cmd() -> None:
        raise Refused(RefusalCategory.PLUGIN_HOOK_FAILED, "hook X failed: detail")

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 5  # DN-028 §9: plugin failure (AUDIT)
    assert "refused: plugin-hook-failed: hook X failed: detail" in result.output


@pytest.mark.parametrize("category,expected_code", [
    ("CAPABILITY_DENIED_BY_POLICY", 2),   # refused
    ("CONFIG_INVALID", 2),                # refused
    ("MOUNT_TARGET_DENIED", 2),           # refused
    ("RUNTIME_CANNOT_ENFORCE", 3),        # runtime/adapter
    ("PLUGIN_HOOK_FAILED", 5),            # plugin failure
    ("PLUGIN_CONTRIBUTION_MALFORMED", 5),
    ("HOST_HELPER_REQUIRES_CONSENT", 5),
])
def test_exit_codes_follow_design_08_scheme(category: str, expected_code: int) -> None:
    """AUDIT (LOW): @handle_refusals maps RefusalCategory to the
    DN-028 §9 exit-code scheme (2 refused / 3 runtime / 5 plugin) instead of
    exiting 4 for everything."""
    cat = getattr(RefusalCategory, category)

    @click.command()
    @handle_refusals
    def cmd() -> None:
        raise Refused(cat, "x")

    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == expected_code, (category, result.exit_code)
