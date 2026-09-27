"""Every `botainer ...` command we print must actually exist.

Audit sweep G,, found nine shipped strings naming commands that do
not exist. Two were worse than dead text: `botainer hpc check-profile` and
`botainer hpc discover` were PLANNED (tasks #99, #98), never built, and the
prose shipped as though they were real —

  * `hpc info` printed check-profile whenever a profile was not
    hardware-verified, i.e. for essentially all 50 shipped cluster profiles;
  * `job_profile_gen` WROTE BOTH INTO THE USER'S OWN .botainer/config.yaml,
    where they sit looking official long after the session ends;
  * eight more occurrences sat in seven shipped cluster_profiles/*.yaml headers.

Running one gives `Error: No such command 'check-profile'. Did you mean
'job-profiles'?` — so the user's reward for following our instruction is a
typo-suggestion for a command they did not type.

The rest were ordinary rot: `plugin install` (real: `plugin add`), `plugin
remove` (real: `plugin disable`), `config show` (real: `config explain`),
`image rebuild` (real: `image build --no-cache`), `botainer browser gateway`
(real: `botainer plugin browser gateway`), and `bot1 ...` — a local shell
alias that is not provided by a standard installation.

WHY AN AST PASS AND NOT A GREP. One of the nine was invisible to a line scan:

    "plugins can be installed later with `botainer plugin "
    "install <path>`."

Python concatenates adjacent literals, so the command name exists only after
concatenation. A per-line regex sees `botainer plugin ` and `install <path>`
and matches neither. Walking the AST and reading each string CONSTANT gets the
joined value, which is what the user is shown.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Shipped trees only — private dirs may reference anything.
_SCAN = ("botainer/**/*.py", "plugins/**/*.py", "plugins/**/*.md",
         "cluster_profiles/*.yaml", "examples/*.yaml", "docs/*.md",
         "tools/pkg/*.sh", "*.md")

# Files that exist in the repo but are NOT distributed. Their references are
# correct for a maintainer and irrelevant to a user, so scanning them would
# report failures nobody should act on.
_NOT_SHIPPED = {"CLAUDE.md", "AGENTS.md", "docs/TESTING-0.1.0.md",
                "docs/HPC-IMPLEMENTATION-PLAN.md", "docs/REVIEW-PROTOCOL.md"}

# A command named in order to say it does NOT exist is accurate, not stale.
# TROUBLESHOOTING.md does this deliberately ("no `botainer packages clean` yet;
# manual rm -rf works today"), and turning that into a failure would push the
# author toward silence instead of candour.
_DISCLAIMED = ("does not exist", "doesn't exist", "not exist", "no such",
               "planned, not built", "never built", "TODO", "yet;", "yet.",
               "not implemented")

# A BACKTICK is required before `botainer`. Without it the pattern matches
# ordinary prose — "botainer protects", "botainer stores data", "botainer cd
# into your project" — and the test drowns in false positives that teach people
# to disable it. Every real instruction in this codebase is written `like this`,
# so requiring the backtick costs no coverage and removes the noise entirely.
# Two levels of subcommand is enough for this CLI's depth.
_CMD_RE = re.compile(r"`botainer\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?")

# Words that follow `botainer` in prose without naming a subcommand.
_PROSE = {"is", "in", "on", "at", "to", "and", "or", "the", "a", "an", "as",
          "can", "will", "would", "does", "did", "runs", "run", "state",
          "project", "session", "sessions", "plugin", "plugins", "itself",
          "cannot", "never", "always", "must", "should", "owns", "writes",
          "reads", "uses", "needs", "knows", "sees", "keeps", "has", "have",
          "was", "were", "may", "might", "core", "v0", "v1", "you", "your",
          "we", "it", "its", "this", "that", "these", "those", "for", "from",
          "with", "without", "by", "of", "not", "no", "only", "also", "then",
          "when", "while", "before", "after", "during", "under", "over"}


def _real_commands() -> set[tuple[str, ...]]:
    """The actual click tree, read from the CLI itself rather than a hand list."""
    import json
    code = (
        "import json\n"
        "from botainer.cli import main as m\n"
        "root = m.cli\n"
        "out = []\n"
        "def walk(cmd, path):\n"
        "    out.append(tuple(path))\n"
        "    for n, sub in getattr(cmd, 'commands', {}).items():\n"
        "        walk(sub, path + [n])\n"
        "for n, sub in getattr(root, 'commands', {}).items():\n"
        "    walk(sub, [n])\n"
        "print(json.dumps([list(p) for p in out]))\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=_REPO,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip(), (
        f"could not enumerate the click tree — this test is worthless if it "
        f"skips, so it fails instead:\n{r.stderr[-500:]}")
    return {tuple(p) for p in json.loads(r.stdout)}


def _strings_of(path: Path) -> list[str]:
    """Every string a reader could see: AST constants for .py, raw text else."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix != ".py":
        return [text]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [text]
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _candidates() -> list[tuple[str, tuple[str, ...]]]:
    seen: set[Path] = set()
    out: list[tuple[str, tuple[str, ...]]] = []
    for pattern in _SCAN:
        for path in sorted(_REPO.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            rel = str(path.relative_to(_REPO))
            if rel in _NOT_SHIPPED or rel.startswith("docs/PROTOCOLS/"):
                continue
            for blob in _strings_of(path):
                # PER MENTION, NOT PER FILE. _strings_of returns a non-Python
                # file as ONE blob, so `any(d in blob ...)` exempted the entire
                # document from a single explicit disclaimer somewhere in it.
                # Measured: 19 of 80 shipped non-.py files were
                # wholly exempt — including tools/pkg/install-hpc.sh, which was
                # carrying two live defects at the time, and 15 cluster
                # profiles, the exact class this test's docstring cites as
                # having held eight of the original nine bad commands.
                #
                # Worse, "planned, not built" — the phrase introduced when the
                # nonexistent commands were replaced — is itself a marker, so
                # writing the disclaimer DISABLED the check for that
                # file. A guard you switch off by being candid is not a guard.
                for _line in blob.splitlines():
                    if any(d in _line for d in _DISCLAIMED):
                        continue
                    for m in _CMD_RE.finditer(_line):
                        first, second = m.group(1), m.group(2)
                        if first in _PROSE:
                            continue
                        out.append((rel, (first,) if not second or second in _PROSE
                                    else (first, second)))
    return out


def test_every_botainer_command_we_print_exists() -> None:
    """THE guard. A command we tell the user to run must resolve."""
    real = _real_commands()
    tops = {p[0] for p in real}
    bad: dict[str, set[tuple[str, ...]]] = {}
    for rel, cand in _candidates():
        if cand[0] not in tops:
            bad.setdefault(rel, set()).add(cand)
        elif len(cand) == 2 and cand not in real and (cand[0],) in real:
            # Only flag a two-word form when the parent HAS subcommands —
            # otherwise the second word is an argument, not a subcommand.
            if any(len(p) == 2 and p[0] == cand[0] for p in real):
                bad.setdefault(rel, set()).add(cand)
    assert not bad, (
        "shipped text names commands that do not exist:\n" + "\n".join(
            f"  {rel}: " + ", ".join("botainer " + " ".join(c)
                                     for c in sorted(v))
            for rel, v in sorted(bad.items())))


def test_no_shipped_CODE_tells_a_user_to_run_bot1() -> None:
    """`bot1` is a local shell alias some installs use for the v0.1 binary. A
    released user has no such command, so every occurrence is an instruction
    that cannot be followed.

    SCOPED TO SHIPPED CODE (botainer/, plugins/) DELIBERATELY. The docs also
    carry `bot1`, but there it is load-bearing for the parallel-install workflow
    those guides describe (`bot1` = v0.1 alongside an existing v0.0.x) — fixing
    them means rewriting that workflow, not renaming a token. Tracked
    separately; scoping the guard rather than deleting it keeps the code half
    protected today instead of trading it for a doc rewrite tonight.
    """
    bad = []
    for pattern in ("botainer/**/*.py", "plugins/**/*.py", "plugins/**/*.md"):
        for path in sorted(_REPO.glob(pattern)):
            if not path.is_file():
                continue
            for blob in _strings_of(path):
                for m in re.finditer(r"`bot1\s+[a-z]", blob):
                    bad.append(f"{path.relative_to(_REPO)}: {blob[max(0,m.start()-30):m.start()+40]!r}")
    assert not bad, "shipped text tells the user to run `bot1`:\n  " + "\n  ".join(bad)
