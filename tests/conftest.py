"""Shared pytest fixtures + test helpers.

The five-tier test plan (see DN-029):
- Tier 1: unit (pure logic) — runs everywhere
- Tier 2: snapshot / schema (textual) — runs everywhere
- Tier 3: mocked integration (adapter playback) — runs everywhere
- Tier 4: real Docker / Apptainer / Slurm — marker-skipped in this container
- Tier 5: hostile / property tests — runs everywhere
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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


# ── The ambient git environment never reaches a test's own repo ─────────────
#
# WHY THIS MATTERS MOST: A HARD GATE FAILS OPEN. (#224)
#
# Git hands its hooks a GIT_INDEX_FILE, and for a partial commit
# (`git commit -o <path>`) that is a TEMPORARY index holding HEAD plus the named
# paths. Tests that build a throwaway repo and shell out to git inherit it — and
# some of them run `git add -A`, so they do not merely READ the pending-commit
# index, they OVERWRITE it. Entry count through one directory of tests, measured:
# 1104 → 1104 → 1104 → 1 → 2.
#
# Two pre-commit checks run AFTER pytest and read that wreckage:
#
#     STAGED="$(git diff --cached --name-only)"     # under `set -uo pipefail`
#     [[ -z "$STAGED" ]] && exit 0                  # ...no -e, so this is reached
#
# The substitution fails, STAGED is empty, and a `run_hard` gate reports "no
# plugin trees changed" and exits 0 — checking nothing, silently. That is worse
# than the noisy symptom that led here (a gate refusing a commit over unrelated
# tests): a gate that fails CLOSED is annoying, one that fails OPEN is believed.
# The scripts have their own rc check now; this fixture removes the cause.
#
# A wrong commit is also reachable: when the staged blobs already exist in the
# repo's object database, git builds the tree successfully — a partial commit
# was observed returning 0 and DELETING the file it was told to commit.
#
# STRUCTURE, NOT A RULE. Eight test files execute git and none scrubbed
# anything. Fixing them one at a time is a filter that holds only while the next
# author remembers, and nobody ever had. Scrubbing once covers every test
# present and future.
#
# THIS IS A SCRUB, NOT A HERMETIC SEAL, and the difference is worth stating.
# It does not isolate tests from a hostile `~/.gitconfig` (a global
# `commit.gpgsign=true` still breaks a git-invoking test), and a fixture cannot
# run before COLLECTION — a module that calls git at import time to compute a
# `skipif` would still see the ambient value and silently skip. Hence the
# module-level pop below as well as the fixture: the pop covers import and
# collection, the fixture covers the test body and restores afterwards.
#
# NOT scrubbed: PATH, and GIT_EXEC_PATH. The claim that git cannot find its
# subcommands without GIT_EXEC_PATH is FALSE — it is unset in a normal shell and
# git derives it. It is left alone because it is not part of this failure class,
# not because it is inert: git PREPENDS it to PATH for everything it spawns, so
# a hostile value would matter for a different reason than the vars below, which
# redirect git at a different repository.
_GIT_ENV_THAT_REDIRECTS_GIT = (
    "GIT_INDEX_FILE",      # observed: the actual cause
    "GIT_DIR",             # clobbers the victim's index
    "GIT_WORK_TREE",       # fills the test's own repo with the victim's files
    "GIT_COMMON_DIR",      # makes `git init` fail outright
    "GIT_OBJECT_DIRECTORY",  # ditto
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",  # can hide a repo from a subdirectory
    "GIT_CONFIG",          # breaks any test that runs `git config`
    "GIT_PREFIX",          # precautionary: no failure mode demonstrated for it
)

# Import time, so collection-time git calls are covered too. The fixture below
# cannot reach them: by the time fixtures run, `skipif` has already been decided.
for _name in _GIT_ENV_THAT_REDIRECTS_GIT:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _no_ambient_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the git environment clean for the duration of every test.

    Redundant with the module-level pop above for anything inherited from the
    parent, and NOT redundant for anything a fixture or an earlier test sets:
    monkeypatch restores after each test, so one test cannot leak into the next.
    """
    for name in _GIT_ENV_THAT_REDIRECTS_GIT:
        monkeypatch.delenv(name, raising=False)
