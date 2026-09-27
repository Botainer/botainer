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


def test_enable_known_name_proceeds(tmp_path, monkeypatch) -> None:
    """A known name gets past the existence check and enables.

    NOW NEEDS A REAL PROJECT ON DISK, and that is the point rather than an
    inconvenience. `enable` used to pass `Path.cwd()` straight to
    `lifecycle.enable`, so with that mocked the command never asked whether it
    was in a project at all — and running it from a subdirectory silently
    addressed the wrong place. It resolves the project with `find_project_root`
    now, so the fixture has to be a project. Refusing outside one is correct
    behaviour, not a regression: the unmocked path always refused, via
    `_require_config`; the mock simply hid which layer said so.

    `plugins_enabled` deliberately holds only `git`, so enabling an agent
    plugin cannot trip the family-exclusion check this test is not about.
    """
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "55555555-5555-4555-8555-555555555555\n")
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: docker\n"
        "profile: default\nnetwork:\n  mode: internet\n"
        "plugins_enabled:\n  - git\n")
    monkeypatch.chdir(proj)

    # `check_family_exclusion` IS PATCHED OUT HERE, and that is not laziness.
    # This test is about the EXISTENCE guard. The exclusion check is a separate
    # mechanism with its own tests in
    # tests/unit/test_plugin_enable_disable_tell_you_the_truth.py.
    #
    # It also must not run against this fixture. `list_installed` is mocked to
    # a MagicMock, so `load_manifest(inst.plugin_dir)` hands a MagicMock to the
    # YAML parser, which allocates until the kernel kills the process — 4.8 GB,
    # OOM, exit 137, and a pre-commit run that truncates at 70% with no failure
    # name. The mock was harmless until the production code grew a call it does
    # not satisfy, which is precisely the "a fixture must match a real install,
    # not the minimum that makes the code path run" rule: a MagicMock stands in
    # for an InstalledPlugin only until something actually uses one.
    fake = [MagicMock(name="agent-claude")]
    fake[0].name = "agent-claude"
    with patch("botainer.cli.plugin.lifecycle_module.list_installed", return_value=fake), \
         patch("botainer.cli.plugin.selection_module.check_family_exclusion"), \
         patch("botainer.cli.plugin.lifecycle_module.enable") as mock_enable:
        runner = CliRunner()
        result = runner.invoke(enable, ["agent-claude"])
        assert result.exit_code == 0, result.output
        mock_enable.assert_called_once()
        assert "Enabled 'agent-claude'" in result.output
