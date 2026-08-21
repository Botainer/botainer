"""The auth-mode list must be ONE list, and it must match the plugin manifests.

The defect this pins (#128): `botainer auth use` offered four modes and
`botainer start --auth-mode` offered three. The missing one was `broker` — the
mode that survives concurrent sessions and the one we recommend for more than
one session at a time. Both lists were hand-written `click.Choice([...])`
literals, individually defensible, with nothing comparing them.

Two assertions, because there are two ways to drift:

1. AUTH_MODES vs the manifests — composition dispatches on `auth_mode:` in the
   plugin manifests, so a mode that no manifest declares cannot work, and a mode
   some manifest declares but the CLI never offers is unreachable.
2. AUTH_MODES vs the CLI — a future editor writing a fresh literal at a new
   `click.Choice` site reintroduces exactly the original bug, so scan for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from botainer.auth_modes import (
    AUTH_MODE_SUMMARY,
    AUTH_MODES,
    EXPERIMENTAL_AUTH_MODES,
)

REPO = Path(__file__).resolve().parents[2]


def _declared_modes() -> set[str]:
    modes = set()
    for man in sorted((REPO / "plugins").glob("*/botainer-plugin.yaml")):
        data = yaml.safe_load(man.read_text()) or {}
        m = data.get("auth_mode")
        if m:
            modes.add(m)
    return modes


def test_auth_modes_matches_what_the_plugins_declare():
    declared = _declared_modes()
    assert declared, "no bundled plugin declares auth_mode — did the glob break?"
    missing_from_cli = declared - set(AUTH_MODES)
    assert not missing_from_cli, (
        f"plugin manifests declare auth_mode(s) {sorted(missing_from_cli)} that "
        f"AUTH_MODES does not offer, so no CLI surface can select them. This is "
        f"the #128 defect: broker existed as a plugin and worked, but "
        f"`start --auth-mode` never listed it. Add them to "
        f"botainer/auth_modes.py."
    )
    unbacked = set(AUTH_MODES) - declared
    assert not unbacked, (
        f"AUTH_MODES offers {sorted(unbacked)} but no bundled plugin declares "
        f"that auth_mode, so selecting it can only no-op or fail."
    )


def test_every_mode_has_a_summary_line():
    # A mode with no summary silently renders as a bare word in --help, which is
    # how `proxy` sat there for months looking like an ordinary option.
    assert set(AUTH_MODE_SUMMARY) == set(AUTH_MODES)
    for mode, text in AUTH_MODE_SUMMARY.items():
        assert text.strip(), f"{mode} has an empty summary"


def test_experimental_modes_are_a_subset():
    assert EXPERIMENTAL_AUTH_MODES <= set(AUTH_MODES)


def test_experimental_modes_say_so_in_help():
    from botainer.auth_modes import choice_help
    rendered = choice_help("Modes:")
    for mode in EXPERIMENTAL_AUTH_MODES:
        # The user must see it AT the choice, not only in a doc.
        idx = rendered.index(mode)
        assert "experimental" in rendered[idx:idx + 40].lower(), (
            f"{mode} is experimental but --help does not say so next to it"
        )


def test_no_cli_surface_hand_writes_the_mode_list():
    """Any `click.Choice([...])` literal that looks like the mode list is a
    second source of truth. Use AUTH_MODES instead."""
    offenders = []
    for py in sorted((REPO / "botainer").rglob("*.py")):
        if py.name == "auth_modes.py":
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "Choice"):
                continue
            for arg in node.args:
                if not isinstance(arg, (ast.List, ast.Tuple)):
                    continue
                vals = {e.value for e in arg.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                # Two or more real mode names in a hand-written literal is the
                # signature of a duplicated list. One is a coincidence
                # ("shared" appears in unrelated flags).
                if len(vals & set(AUTH_MODES)) >= 2:
                    offenders.append(
                        f"{py.relative_to(REPO)}:{node.lineno} -> {sorted(vals)}")
    assert not offenders, (
        "hand-written auth-mode list(s) found; import AUTH_MODES from "
        "botainer.auth_modes instead:\n  " + "\n  ".join(offenders)
    )
