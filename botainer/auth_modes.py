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
    "shared": "one host-wide login, copied into each project (one session at a time)",
    "broker": "the container never holds the credential; a host process answers "
              "for it (works with several sessions at once)",
    "proxy": "EXPERIMENTAL, incomplete — see `botainer auth use proxy`",
}


def choice_help(prefix: str) -> str:
    """Render the mode list for a click `help=` string, flagging experimentals."""
    parts = []
    for m in AUTH_MODES:
        tag = " [experimental]" if m in EXPERIMENTAL_AUTH_MODES else ""
        parts.append(f"{m}{tag}: {AUTH_MODE_SUMMARY[m]}")
    return prefix + "  " + ";  ".join(parts) + "."
