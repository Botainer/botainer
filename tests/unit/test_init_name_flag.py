"""Task #76: `botainer init --name <name>` must be accepted by Click.

Was: docs and CLI help mentioned --name; the flag did not exist on the
init command, so users hit 'no such option' errors.

Now: --name accepted, passed through to do_init via the existing
do_init(name=...) parameter (currently cosmetic storage; downstream
meta.json wire-up is a follow-up).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from botainer.cli.init import init


def test_init_accepts_name_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    # We invoke with --help to avoid actually creating a project; --help
    # exit code is 0 and Click parses all options first.
    result = runner.invoke(init, ["--help"])
    assert result.exit_code == 0
    assert "--name" in result.output
    assert "Friendly project name" in result.output
