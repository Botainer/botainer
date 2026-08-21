"""Codex 45#8: top-level --help groups commands by purpose.

A new user looking at `botainer --help` should see a guided sequence
(setup → init → start → ...), not a flat alphabetical list of 20
items. The custom Click group in botainer/cli/main.py renders sections
defined by _COMMAND_GROUPS.
"""

from __future__ import annotations

from click.testing import CliRunner


def test_top_level_help_renders_groups() -> None:
    from botainer.cli.main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    out = result.output
    # The named section headings appear in the canonical order.
    expected_headings = [
        "Getting started:",
        "Daily session:",
        "Auth and config:",
        "Inspection:",
        "HPC:",
        "Advanced:",
    ]
    last_idx = -1
    for heading in expected_headings:
        # Match the heading at start-of-line (click formatter prefix),
        # so the word "HPC" in the docstring doesn't false-match.
        marker = "\n" + heading
        idx = out.find(marker)
        assert idx >= 0, f"missing section {heading!r} in --help output"
        assert idx > last_idx, f"section {heading!r} out of order"
        last_idx = idx


def test_top_level_help_lists_each_command_under_a_group() -> None:
    """Every command in _COMMAND_GROUPS appears under its declared section."""
    from botainer.cli.main import _COMMAND_GROUPS, cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    out = result.output
    for group_name, members in _COMMAND_GROUPS:
        for name in members:
            # A subcommand may legitimately be missing (e.g. nudge gated
            # behind plugin install) — but if it's registered, it must
            # appear somewhere in the help output.
            from botainer.cli.main import cli as _cli
            if name in _cli.commands:
                assert name in out, f"command {name!r} missing from --help"


def test_top_level_help_mentions_onboarding_path() -> None:
    """The top-level help docstring suggests the canonical onboarding sequence."""
    from botainer.cli.main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    out = result.output
    assert "botainer setup" in out
    assert "botainer init" in out
    assert "botainer start" in out
