"""The canonical list of auth modes — ONE tuple, imported by every CLI surface.

WHY THIS FILE EXISTS. The mode list used to be written out by hand at each
`click.Choice(...)` site. They drifted: `botainer auth use` offered four modes
and `botainer start --auth-mode` offered three, silently omitting `broker` —
which is the mode that actually survives concurrent sessions and the one we
recommend for more than one session at a time. So the flag whose whole job is
picking an auth mode hid the best one, and nothing compared the two lists.

The source of truth is the plugin manifests: a mode EXISTS iff some installed
plugin declares `auth_mode: <name>`. That is what composition dispatches on
(`_apply_auth_mode_override_in_memory` builds family -> {mode: plugin} straight
from the manifests), so the manifests decide what actually works.

This tuple is a presentation-layer mirror of that fact, and mirrors drift.
`tests/integration/test_auth_modes_agree.py` is what keeps them in step: it reads
every bundled manifest and fails if the set of declared `auth_mode` values is
not exactly AUTH_MODES, and it fails if any CLI surface builds its own list.
Adding a plugin with a new mode therefore breaks a test until this file is
updated — which is the point.
"""

from __future__ import annotations

# Order is deliberate: this is the order the modes appear in `--help`, so it
# runs least-shared to most-shared rather than alphabetically.
AUTH_MODES: tuple[str, ...] = ("isolated", "shared", "broker", "proxy")

# Modes that are NOT ready for ordinary use. They stay in AUTH_MODES — removing
# a mode the config file may already name would turn a working (if warned)
# session into a hard refusal — but every surface that offers them must say so
# at the point of choice, not only in a doc.
#
# proxy: dead on arrival at v0.1.0 (#59) — no refresh-on-401, per-project
#        credentials only, OAuth files may not work, and refused outright on
#        HPC (see hpc-launcher/host_helper/submit.py `_refuse_proxy_on_hpc`).
EXPERIMENTAL_AUTH_MODES: frozenset[str] = frozenset({"proxy"})

# One-line description per mode, for `--help` and error messages. Keep these
# FACTUAL and comparative-verdict-free: state what the mode DOES with the
# credential; never rank them as "safe"/"safer"/"protects". A one-liner cannot
# carry the caveats a security claim needs, so a verdict here misleads — the
# nuance belongs in docs/CAPABILITY-SURFACE.md, which has room for it.
AUTH_MODE_SUMMARY: dict[str, str] = {
    "isolated": "this project logs in on its own; credential stays per-project",
    # NOT "copied into each project". The per-project credential file is a
    # SYMLINK to the one shared file (both shared hooks do
    # `os.symlink(expected_target, ...)`), and it is the same one file for every
    # auth profile too. "Copied" describes the opposite topology and makes the
    # overwrite warning elsewhere read as impossible.
    #
    # "what one session changes, the others get" rather than "a write in one is
    # a write for all": a refuting review drove a real agent-shaped write
    # (temp file + rename, which is what Claude Code does) and the rename
    # REPLACED the symlink with a regular file — the shared file was untouched
    # until the next launch's reconcile back-filled it. The effect is shared;
    # the timing is next-launch, and the back-fill is gated on the file's shape.
    "shared": "one host-wide login every project links to; what one session "
              "changes, the others get (one session at a time)",
    "broker": "the container never holds the credential; a host process answers "
              "for it (works with several sessions at once)",
    "proxy": "EXPERIMENTAL, incomplete — see `botainer auth use proxy`",
}


# WHAT A ONE-SHOT AXIS OVERRIDE ACTUALLY DOES, in one sentence, in one place.
#
# The mode and the profile are PATH COMPONENTS of the agent's config directory,
# so they select not just the credential but the transcripts, todos, the user's
# own subagents, settings.json and the MCP entries. Five shipped help strings
# described `--auth-profile`; two said "credential + history directory", one
# said only "Reads credentials from …", and two said neither — so the surface a
# user reads depended on which command they happened to type. The one that
# surprises people is the one this spells out: the session runs against a
# DIFFERENT directory, usually an empty one, NOTHING is carried into it, and the
# next run without the flag goes back — leaving whatever the agent wrote behind.
#
# That "nothing is carried" is deliberate, not an omission: the change detector
# reads the config FILE and never the composed spec, so a flag cannot reach it.
# Otherwise one `--auth-profile work` would carry history there and the next
# plain run would carry it back — two moves from one flag.
ONE_SHOT_AXIS_HELP = (
    "This session only; .botainer/config.yaml is NOT modified. A different "
    "PROFILE means a different directory for the agent's whole config — "
    "credential AND transcripts, todos, settings and MCP entries — so the "
    "session runs against that one as it stands (often empty) and nothing is "
    "carried into it. On the MODE axis only `broker` has its own directory: "
    "isolated and shared share one, so switching between those two keeps the "
    "history. Either way the next run without the flag goes back to what the "
    "config says. For a persistent change that DOES offer to carry the history, "
    "use `botainer auth use <mode>` or edit `profile:` in .botainer/config.yaml."
)


def mode_list_hint() -> str:
    """The mode list for an inline command hint: `isolated|shared|broker`.

    Experimental modes are left OUT, because a hint tells the user what to
    TYPE and a mode that refuses to start is not an offer. They stay in
    AUTH_MODES (a config file may already name one) and `choice_help` still
    renders them, tagged.

    This exists because three shipped strings wrote the list out by hand: the
    `policy set default_auth_mode <...>` hint, the family-conflict refusal, and
    the comment `init` writes into the user's own config.yaml. Two of the three
    offered `proxy`, which cannot start, and omitted `broker`, the mode for more
    than one session at a time — the #128 defect (one list per site, nothing
    comparing them) in prose instead of in `click.Choice`.
    """
    return "|".join(m for m in AUTH_MODES if m not in EXPERIMENTAL_AUTH_MODES)


def choice_help(prefix: str) -> str:
    """Render the mode list for a click `help=` string, flagging experimentals."""
    parts = []
    for m in AUTH_MODES:
        tag = " [experimental]" if m in EXPERIMENTAL_AUTH_MODES else ""
        parts.append(f"{m}{tag}: {AUTH_MODE_SUMMARY[m]}")
    return prefix + "  " + ";  ".join(parts) + "."
