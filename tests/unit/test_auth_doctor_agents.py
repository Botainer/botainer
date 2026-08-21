"""`auth doctor --agent codex` must not say "nothing is logged in" when it is.

Reported by the user with two real codex credentials on disk:

    $ bot1 auth doctor --agent codex
    No codex credentials found anywhere under this root.
      Nothing is logged in. `botainer auth login` to start.

A confident false negative, in the one command whose own docstring says it
exists to "report the OBSERVED state instead of another theory". Two bugs
stacked:

  * the credential FILENAME and the OAuth block key were hardcoded to Claude's
    (`.credentials.json` / `claudeAiOauth`); codex uses auth.json with a
    `tokens` object;
  * the directory component was built as `agent-<name>`, so a bare
    `--agent codex` could not match either — both spellings failed, for
    different reasons.

`auth status`, in the SAME command group and on the same disk, found both files
— it resolves the filename per family. That was the model to copy, and copying
it is what this fixes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_CODEX = {"tokens": {"access_token": "a" * 40, "refresh_token": "r" * 40,
                     "account_id": "acct_1"}}
_CLAUDE = {"claudeAiOauth": {"accessToken": "a" * 64, "refreshToken": "r" * 64,
                             "expiresAt": 9_999_999_999_000}}


def _run(root: Path, *args) -> subprocess.CompletedProcess:
    code = ("import sys; sys.argv=['botainer']+%r\n"
            "from botainer.cli.main import main\n"
            "sys.exit(main())\n") % (list(args),)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO, capture_output=True, text=True,
        env={**os.environ, "MY_BOTAINER": str(root), "PYTHONPATH": str(_REPO)},
        timeout=120)


@pytest.fixture()
def codex_root(tmp_path) -> Path:
    root = tmp_path / "state-root"
    shared = root / "shared-auth" / "agent-codex"
    shared.mkdir(parents=True)
    (shared / "auth.json").write_text(json.dumps(_CODEX))
    proj = (root / "state" / str(uuid.uuid4()) / "data" / "agent-codex"
            / "profiles" / "default")
    proj.mkdir(parents=True)
    (proj / "auth.json").write_text(json.dumps(_CODEX))
    return root


@pytest.mark.parametrize("spelling", ["codex", "agent-codex"])
def test_codex_credentials_are_FOUND(codex_root, spelling) -> None:
    """THE regression. Both spellings, because both were broken."""
    out = _run(codex_root, "auth", "doctor", "--agent", spelling)
    assert "Credential holders (2)" in out.stdout, (
        f"--agent {spelling} did not find the two codex credentials on disk.\n"
        f"stdout:\n{out.stdout[:600]}")
    assert "Nothing is logged in" not in out.stdout, (
        "reported nothing logged in while two credentials were present — the "
        "original false negative")


def test_claude_still_works(tmp_path) -> None:
    """The fix generalises the lookup; it must not break the default agent."""
    root = tmp_path / "r"
    shared = root / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True)
    (shared / ".credentials.json").write_text(json.dumps(_CLAUDE))
    out = _run(root, "auth", "doctor")
    assert "Credential holders (1)" in out.stdout, out.stdout[:400]


def test_the_settled_rotation_question_is_not_reopened(tmp_path) -> None:
    """The command used to end by telling the user to go run the R1 experiment
    and settle whether tokens rotate — from a doc under private/, which the
    distribution does not contain. It WAS settled  (rotation AND
    invalidation, HTTP 400). Asking the user to measure a measured thing trains
    them to ignore the command's guidance."""
    root = tmp_path / "r"
    shared = root / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True)
    (shared / ".credentials.json").write_text(json.dumps(_CLAUDE))
    out = _run(root, "auth", "doctor")
    assert "private/" not in out.stdout, (
        "points at a path the distribution excludes")
    assert "R1 experiment" not in out.stdout
