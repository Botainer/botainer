"""Task #235: plugin enable rejects typos with difflib suggestion.

Was: enable('age-claud') succeeded silently; nothing was actually
enabled (no installed plugin matched); start later complained about
the missing plugin with no hint why.

Now: check installed list first; if no match, suggest closest with
difflib; exit 4 (refused).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from botainer.cli.plugin import enable


def test_enable_typo_rejected_with_suggestion() -> None:
    fake = [MagicMock(name="agent-claude")]
    fake[0].name = "agent-claude"
    with patch("botainer.cli.plugin.lifecycle_module.list_installed", return_value=fake):
        runner = CliRunner()
        result = runner.invoke(enable, ["age-claud"])
        assert result.exit_code == 4
        assert "not installed" in result.output
        assert "Did you mean 'agent-claude'?" in result.output


def test_enable_unknown_no_close_match_lists_options() -> None:
    fake = [MagicMock(name="x"), MagicMock(name="y")]
    fake[0].name = "x"
    fake[1].name = "y"
    with patch("botainer.cli.plugin.lifecycle_module.list_installed", return_value=fake):
        runner = CliRunner()
        result = runner.invoke(enable, ["completely-different"])
        assert result.exit_code == 4
        assert "Installed plugins:" in result.output
        assert "  x" in result.output
        assert "  y" in result.output


def test_enable_known_name_proceeds() -> None:
    fake = [MagicMock(name="agent-claude")]
    fake[0].name = "agent-claude"
    with patch("botainer.cli.plugin.lifecycle_module.list_installed", return_value=fake), \
         patch("botainer.cli.plugin.lifecycle_module.enable") as mock_enable:
        runner = CliRunner()
        result = runner.invoke(enable, ["agent-claude"])
        assert result.exit_code == 0
        mock_enable.assert_called_once()
        assert "Enabled 'agent-claude'" in result.output
