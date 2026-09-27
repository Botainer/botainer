"""The permission vocabulary: ONE table, per agent family.

WHAT THIS REPLACES. `agent_permissions` used to accept exactly two words,
`bypass` and `prompt`, hardcoded in four places that had to agree. Claude Code
has six modes of its own and codex has two orthogonal axes, so the two-word
vocabulary could express only the extremes — and `prompt` was misnamed, because
it does not force prompting, it declines to override.

THE SHAPE, which is the maintainer's (2026-09-21): keep `bypass`, add exactly
one new word `default` meaning "botainer injects nothing", and let people write
THE AGENTS' OWN MODE NAMES for everything else. Nothing is renamed. `prompt`
stays accepted forever as an alias of `default`, so no existing config breaks.

WHY THE TABLE LIVES HERE AND NOT IN `composition.py`. Four modules need the same
answer — compose (which argv), the policy cap (which tier), the launch banner
and `config show` (what to tell a human). When each carried its own copy they
drifted; the two-value vocabulary was duplicated across `config.py`,
`policy.py` and `composition.py`, and the banner asserted behaviour that
contradicted all three. One table, four readers.

────────────────────────────────────────────────────────────────────────────
MEASURED FACTS ABOUT CODEX'S SANDBOX. These decide the openai rows, and each
was measured by RUNNING the binary (codex-cli 0.145.0) in a docker dev
container on 2026-09-21, not read out of `--help`:

  codex ships TWO sandbox backends and they fail differently.

    bubblewrap (the DEFAULT)   needs to create an unprivileged user namespace
    landlock (opt-in, via      a kernel LSM; needs no namespace at all
      features.use_legacy_landlock=true)

                       bubblewrap      landlock
    read-only            fails          WORKS, and enforces
    workspace-write      fails          fails (codex's own limitation:
                                        "permission profiles requiring direct
                                        runtime enforcement are incompatible
                                        with --use-legacy-landlock")
    danger-full-access   works          works

  "fails" for bubblewrap is always the same:
    bwrap: No permissions to create a new namespace, likely because the kernel
    does not allow non-privileged user namespaces.

  read-only under landlock was checked for ENFORCEMENT, not just for starting:
  a write to a temp file returned "Permission denied" and the file was
  unchanged; a read of the same file succeeded. A sandbox that starts and
  contains nothing would be worse than none, so "it ran" was not accepted as
  the answer.

TWO CORRECTIONS TO EARLIER DRAFTS, recorded because both were told to the
maintainer as fact and both were wrong:

  1. "Codex's sandbox needs a user namespace and a container cannot" is TOO
     BROAD. Only the bubblewrap backend does. Pinning `danger-full-access` for
     `default` is therefore a CHOICE — see the openai `default` row — and not a
     physical constraint.
  2. "The agent images ship no bwrap, so that is a second independent reason to
     fail" is FALSE. The npm package bundles its own
     `codex-resources/bwrap` (529 KB), and with the system bwrap removed from
     PATH codex still runs the bundled one — proven by the error text changing.
     So image content is NOT a reason to fail; the kernel's userns refusal is
     the only one, and that is a property of the HOST and runtime.

WHAT IS STILL UNMEASURED: whether landlock is available under APPTAINER on an
HPC compute node. Landlock needs kernel >= 5.13 or a distro backport, and many
clusters run older kernels. A host-side probe in the project's dev tooling
# measures it on
the machine; nothing here should be believed about apptainer until it has run
there. Full derivation: internal design note DN-011.
────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Tiers ───────────────────────────────────────────────────────────────────
# A site policy (`agent.max_permissions`) needs to ORDER postures, and the mode
# list does not sort onto a line: claude's six modes and codex's two axes are
# not points on one scale. Where does `plan` (executes nothing) sit relative to
# `acceptEdits`? The question has no answer, so the cap does not ask it.
#
# It ranks TIERS, and the criterion is about what BOTAINER does, never about
# how much a model can be trusted:
#
#   TIER_AGENT_DECIDES (0)  the agent's own permission system is RUNNING.
#                           Whether you are asked, and about what, is the
#                           agent's business.
#   TIER_BOTAINER_OFF  (1)  botainer switches the agent's permission system
#                           OFF. Nothing will ask, by construction.
#
# This criterion was chosen after two worse ones were tried and mis-sorted their
# own table. "Never asks a human" puts `dontAsk` (which denies rather than asks)
# and `plan` (which executes nothing) in the dangerous tier, which is backwards.
# "Removes the human from the loop" would put `auto` and codex's `on-request`
# there — and `on-request` is codex's own default, so a capped site could not
# run codex as codex ships it.
#
# The line drawn here is crisp and needs no judgement about model behaviour:
# did WE turn the agent's asking off, or is it still on? Only four values turn
# it off, and they are the four that name themselves as doing so.
TIER_AGENT_DECIDES = 0
TIER_BOTAINER_OFF = 1

TIER_NAMES = {
    TIER_AGENT_DECIDES: "default",
    TIER_BOTAINER_OFF: "bypass",
}
# What a policy file may write for `agent.max_permissions`. `prompt` is kept
# because policies in the field already say it.
CAP_VALUES: dict[str, int] = {
    "default": TIER_AGENT_DECIDES,
    "prompt": TIER_AGENT_DECIDES,   # legacy spelling of the same ceiling
    "bypass": TIER_BOTAINER_OFF,
}


@dataclass(frozen=True)
class Mode:
    """One permission posture, for one agent family.

    `argv` is what trusted compose appends to the agent command line. It is a
    FIXED tuple selected from this table by name — never free text, and never a
    plugin `command_append`. A hostile config can pick a row; it cannot write
    one.
    """

    argv: tuple[str, ...]
    tier: int
    gloss: str


# ── The one new botainer word, plus its legacy spelling ─────────────────────
DEFAULT_ALIASES = ("prompt",)   # `prompt` has always meant "append nothing"
CANONICAL_DEFAULT = "default"


def canonical(value: str) -> str:
    """Fold the legacy spelling onto the word it has always meant."""
    return CANONICAL_DEFAULT if value in DEFAULT_ALIASES else value


# ── Claude Code ─────────────────────────────────────────────────────────────
# Modes and glosses are the BINARY's own, read out of Claude Code 2.1.220 with
# `--help` and `strings`. `--permission-mode` advertises six choices:
# acceptEdits, auto, bypassPermissions, manual, dontAsk, plan.
#
# `manual` is an alias for `default` — the binary says so: "Default permission
# mode when Claude Code needs access ('manual' is accepted as an alias for
# 'default')". Both are listed so either spelling works; they do the same thing.
#
# `bypass` maps to `--dangerously-skip-permissions` rather than
# `--permission-mode bypassPermissions`, and that is deliberate: the binary
# says bypassPermissions "requires allowDangerouslySkipPermissions", i.e. a
# SECOND flag. The single-flag spelling is what works today and what has been
# shipping; this change does not touch it. `bypassPermissions` is accepted as a
# name for the same posture so that reaching for claude's own word gets the
# working argv instead of a trap.
_ANTHROPIC: dict[str, Mode] = {
    "bypass": Mode(
        ("--dangerously-skip-permissions",), TIER_BOTAINER_OFF,
        "the agent acts without asking you to approve each action"),
    "bypassPermissions": Mode(
        ("--dangerously-skip-permissions",), TIER_BOTAINER_OFF,
        "the agent acts without asking you to approve each action"),
    CANONICAL_DEFAULT: Mode(
        (), TIER_AGENT_DECIDES,
        "botainer appends nothing; claude's own default applies"),
    "acceptEdits": Mode(
        ("--permission-mode", "acceptEdits"), TIER_AGENT_DECIDES,
        "auto-accepts file edits, asks about everything else"),
    "auto": Mode(
        ("--permission-mode", "auto"), TIER_AGENT_DECIDES,
        "a model classifier approves or denies each prompt"),
    "plan": Mode(
        ("--permission-mode", "plan"), TIER_AGENT_DECIDES,
        "plans only; executes no tools"),
    "manual": Mode(
        ("--permission-mode", "manual"), TIER_AGENT_DECIDES,
        "claude's stock behaviour: asks before dangerous operations"),
    "dontAsk": Mode(
        ("--permission-mode", "dontAsk"), TIER_AGENT_DECIDES,
        "never prompts; DENIES anything not pre-approved"),
}

# ── codex ───────────────────────────────────────────────────────────────────
# Two axes, `--sandbox` and `--ask-for-approval`, whose vocabularies are
# DISJOINT — no word is both — so one string routes to the right flag with no
# ambiguity.
#
# Every row pins the sandbox axis. See the module docstring for why: codex's own
# default (`workspace-write`) cannot run a single command in a container on
# either backend, so "append nothing" would hand you an agent that starts,
# warns once, and then fails every tool call. Alive but crippled.
#
# `read-only` is the one row that restores a real inner boundary, and it needs
# the landlock backend to start at all. Measured to ENFORCE, not merely to run.
_CODEX_FULL = ("--sandbox", "danger-full-access")
_OPENAI: dict[str, Mode] = {
    "bypass": Mode(
        _CODEX_FULL + ("--ask-for-approval", "never"), TIER_BOTAINER_OFF,
        "the agent acts without asking you to approve each action"),
    "never": Mode(
        _CODEX_FULL + ("--ask-for-approval", "never"), TIER_BOTAINER_OFF,
        "never asks; failures go straight back to the model"),
    "danger-full-access": Mode(
        _CODEX_FULL, TIER_BOTAINER_OFF,
        "no inner sandbox; the container is the only boundary"),
    CANONICAL_DEFAULT: Mode(
        _CODEX_FULL, TIER_AGENT_DECIDES,
        "botainer pins only the sandbox (it cannot start otherwise); "
        "codex decides whether to ask you"),
    "untrusted": Mode(
        _CODEX_FULL + ("--ask-for-approval", "untrusted"), TIER_AGENT_DECIDES,
        "runs only trusted commands unasked; escalates the rest to you"),
    "on-request": Mode(
        _CODEX_FULL + ("--ask-for-approval", "on-request"), TIER_AGENT_DECIDES,
        "the model decides when to ask you"),
    "read-only": Mode(
        ("--sandbox", "read-only",
         "-c", "features.use_legacy_landlock=true"), TIER_AGENT_DECIDES,
        "the agent cannot write anywhere; reads are allowed"),
}

# A mode the agent HAS, that botainer must refuse, with the measurement as the
# reason. Refusing beats accepting-and-breaking: a config naming this would
# otherwise produce the crippled agent described above.
#
# This is a FILTER, not a structural guarantee, and it is named as one: it holds
# only while the measurement holds. If a future codex makes workspace-write work
# without a user namespace, this row should be deleted and the mode added above
# — which is what `tests/integration/test_agent_permission_table_matches_cli.py`
# is for.
REFUSED: dict[str, dict[str, str]] = {
    "openai": {
        "workspace-write": (
            "codex cannot start its `workspace-write` sandbox inside a "
            "container. Measured on codex-cli 0.145.0: the bubblewrap backend "
            "needs to create an unprivileged user namespace, which a container "
            "cannot do, and the landlock backend refuses this mode outright "
            "(\"permission profiles requiring direct runtime enforcement are "
            "incompatible with --use-legacy-landlock\"). An agent started this "
            "way runs, warns once, and then fails every command it tries. Use "
            "`read-only` for a real inner boundary that does work, or "
            "`default` to let codex decide about asking while botainer pins "
            "the sandbox open."
        ),
    },
}

_TABLES: dict[str, dict[str, Mode]] = {
    "anthropic": _ANTHROPIC,
    "openai": _OPENAI,
}

# Every word any agent accepts, plus the botainer words. Parse-time validation
# uses this UNION and nothing narrower, because at parse time the agent is not
# known — `agent:` is a sibling field and `botainer start --agent codex` can
# override it afterwards. Narrowing per-agent happens at compose, where the
# answer is settled. A typo is still caught here; a cross-agent mistake is
# caught there, with a message naming what the chosen agent does accept.
ALL_KNOWN_VALUES: frozenset[str] = frozenset(
    set(DEFAULT_ALIASES)
    | {CANONICAL_DEFAULT}
    | {m for table in _TABLES.values() for m in table}
    | {m for fam in REFUSED.values() for m in fam}
)


def modes_for(family: str | None) -> dict[str, Mode]:
    """The vocabulary of one agent family, empty for a family we do not know."""
    return dict(_TABLES.get(family or "", {}))


def refusal_for(family: str | None, value: str) -> str | None:
    """Why this family must refuse `value`, or None if it need not."""
    return REFUSED.get(family or "", {}).get(value)


def tier_of(family: str | None, value: str) -> int | None:
    """Which ceiling `value` sits under, or None if this family has no such mode.

    Returning None is meaningful: the caller must refuse rather than guess. A
    `.get(value, SOMETHING)` default here would be the old `_PERM_RANK`
    behaviour, where an unranked value silently took a number.
    """
    mode = _TABLES.get(family or "", {}).get(canonical(value))
    return None if mode is None else mode.tier


def argv_for(family: str | None, value: str) -> tuple[str, ...] | None:
    """What compose appends, or None if this family has no such mode."""
    mode = _TABLES.get(family or "", {}).get(canonical(value))
    return None if mode is None else mode.argv


def gloss_for(family: str | None, value: str) -> str | None:
    """One line saying what this mode DOES, for a human-facing surface.

    Every gloss is the agent's own description of its own mode, not botainer's
    opinion of it, and states a FACT rather than a safety verdict — no "safe",
    "safer", "protects" or "secure" appears in this table: a one-liner cannot
    carry the caveats a security claim needs, so a verdict there misleads.
    """
    mode = _TABLES.get(family or "", {}).get(canonical(value))
    return None if mode is None else mode.gloss


def is_botainer_word(value: str) -> bool:
    """True for the words that mean the same thing for every agent."""
    return canonical(value) in {CANONICAL_DEFAULT, "bypass"}
