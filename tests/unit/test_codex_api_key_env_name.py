"""codex reads CODEX_API_KEY, not OPENAI_API_KEY — and the wrap must export it.

MEASURED against codex-cli 0.145.0 (2026-08-27), in the dev container:

  - `CODEX_API_KEY` set   → codex sends the key (the server rejects the value,
    which is the point: the header went).
  - `OPENAI_API_KEY` set  → codex behaves exactly as if NO credential were
    present. The variable is not read.

The binary's own message is "run `codex login` or set CODEX_API_KEY". Its only
mentions of `OPENAI_API_KEY` are help text suggesting you PIPE it into the login
command — `printenv OPENAI_API_KEY | codex login --with-api-key` — i.e. it is an
INPUT to `login`, never an ambient variable codex consults.

WHAT THAT COST: every mount / isolated / shared codex session read the mounted
key file and exported it into a variable codex ignores. The credential was
present, correct, and unused. Nothing said so, because from botainer's side the
delivery looked complete — file mounted, variable set, session started.

This test drives the ACTUAL shell the entrypoint runs, not a Python
reimplementation of it, because the defect lived in the shell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WRAP = Path(__file__).resolve().parents[2] / "plugins" / "agent-codex" / "entrypoint_wrap.sh"


def _run_key_block(key_file: Path) -> dict[str, str]:
    """Execute the wrap's key-loading block and report the resulting env.

    The wrap ends by exec'ing codex, which does not exist here, so the block is
    extracted rather than the whole script run. It is extracted BY MARKER from
    the real file — not copied into this test — so a future edit to the block is
    what runs, and this test cannot silently drift from the shipped script.
    """
    text = WRAP.read_text(encoding="utf-8")
    lines = text.splitlines()
    first = next(i for i, ln in enumerate(lines)
                 if ln.startswith('if [ -r "${OPENAI_API_KEY_FILE:'))
    # END ON A LINE THAT IS EXACTLY `fi`, NOT ON THE SUBSTRING "fi".
    # This searched for `text.index("fi", start)`, which matches inside any
    # word containing those two letters — and the block's own comments say
    # "env-file". So the extraction cut off mid-comment, handed `sh` an
    # unterminated `if`, and all three tests failed with a syntax error that
    # said nothing about the real cause. An extractor that can silently take
    # the wrong slice is the same shape as a check that cannot fail: it looks
    # like it is testing the shipped script and is not.
    last = next(i for i, ln in enumerate(lines[first:], start=first)
                if ln.strip() == "fi")
    block = "\n".join(lines[first:last + 1])
    out = subprocess.run(
        ["sh", "-c", f'OPENAI_API_KEY_FILE="{key_file}"\n{block}\n'
                     'echo "CODEX_API_KEY=$CODEX_API_KEY"\n'
                     'echo "OPENAI_API_KEY=$OPENAI_API_KEY"\n'],
        capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"},
    )
    return dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )


def test_the_wrap_exports_the_variable_codex_actually_reads(tmp_path) -> None:
    kf = tmp_path / "api_key"
    kf.write_text("sk-testvalue")

    env = _run_key_block(kf)

    assert env["CODEX_API_KEY"] == "sk-testvalue", (
        "the mounted key must reach CODEX_API_KEY — codex 0.145.0 ignores "
        "OPENAI_API_KEY, so exporting only that hands the credential to a "
        "variable nothing reads and the session starts unauthenticated with "
        "no error"
    )


def test_openai_api_key_is_also_set_for_everything_else_in_the_image(tmp_path) -> None:
    """Kept deliberately, not left over.

    The user mounted a key; other tools in the image — SDKs, notebooks, scripts
    — read OPENAI_API_KEY by convention. Dropping it would fix codex and quietly
    break those. Costs nothing to set both.
    """
    kf = tmp_path / "api_key"
    kf.write_text("sk-testvalue")

    assert _run_key_block(kf)["OPENAI_API_KEY"] == "sk-testvalue"


def test_no_key_file_means_no_variables(tmp_path) -> None:
    """Broker mode mounts no key file, and the sentinel it provisioned must
    survive. If this block set anything unconditionally it would clobber it."""
    env = _run_key_block(tmp_path / "does-not-exist")
    assert env["CODEX_API_KEY"] == ""
    assert env["OPENAI_API_KEY"] == ""
