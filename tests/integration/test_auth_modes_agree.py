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
import re
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


def _is_click_command(node) -> bool:
    """True if this def is decorated as a click command/group.

    Its docstring is then the text `--help` prints, i.e. user-facing output
    rather than a note to the next editor.
    """
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name in {"command", "group"}:
            return True
    return False


def test_mode_list_hint_names_only_modes_that_can_start():
    """`mode_list_hint()` is what a hint tells the user to TYPE.

    Two shipped hints wrote the list by hand as `<isolated|shared|proxy>` —
    offering the one mode that refuses to start, and omitting the one for more
    than one session at a time. So the rendered hint must exclude every
    experimental mode and include every other one.
    """
    from botainer.auth_modes import mode_list_hint

    rendered = mode_list_hint()
    named = rendered.split("|")
    for mode in EXPERIMENTAL_AUTH_MODES:
        assert mode not in named, (
            f"{mode} is experimental — it cannot start — but the hint tells the "
            f"user to type it: {rendered!r}")
    assert named == [m for m in AUTH_MODES if m not in EXPERIMENTAL_AUTH_MODES], (
        f"the hint {rendered!r} is not AUTH_MODES minus the experimentals; a "
        f"hand-picked subset is the second source of truth this exists to kill")


def test_no_shipped_hint_hand_writes_the_command_it_tells_you_to_run():
    """The SECOND way the list drifts, and the one the Choice scan cannot see.

    `test_no_cli_surface_hand_writes_the_mode_list` looks at `click.Choice`
    literals. Three shipped strings recited the same list in PROSE instead —
    `policy set default_auth_mode <isolated|shared|proxy>`, the family-conflict
    refusal, and the comment `init` writes into the user's own config.yaml.
    They are neither a Choice nor an option, so both existing tests passed in
    silence while two of the three offered `proxy` and hid `broker`.

    What it looks for is the PIPE, not the words: `isolated|shared|broker` is
    the CLI's own spelling of "choose one of these", so a pipe-joined run of
    mode names in a string the user reads IS a second copy of the list.
    Rendering it from `mode_list_hint()` splits the literal, and the joined run
    no longer appears in any one constant.

    Deliberately NOT flagged, because it is not a list: prose naming ONE
    concrete command — `botainer auth use isolated` beside `botainer auth use
    broker` is two instructions, and forcing those through a helper would make
    the advice vaguer, not truer.

    A CLICK COMMAND'S DOCSTRING IS ITS `--help` BODY, so those ARE scanned.
    This test first exempted every docstring on the reasoning that a docstring
    is read by whoever edits the code; a refuting review pointed out that
    `botainer auth login --help` was printing `--mode=<shared|isolated|proxy>`
    straight out of one — the #128 defect, live, in the auth command group.
    Non-click docstrings stay exempt: `(shared | isolated)` in an internal
    helper is shorthand for "either mode" and no user ever sees it.

    WHAT THIS DOES NOT COVER, stated rather than left to be discovered: a list
    joined by `/` or `, ` instead of `|`; a list assembled at runtime; a list
    in a plugin manifest, a shipped doc or a hook under `plugins/`. It is a
    backup for the structural fix (one rendering function), not a fence around
    every way the list could be re-typed.
    """
    joined = re.compile(
        r"\b(" + "|".join(AUTH_MODES) + r")\b(?:\s*\|\s*\b(" + "|".join(AUTH_MODES)
        + r")\b)+")
    offenders = []
    help_bodies_scanned = 0
    for py in sorted((REPO / "botainer").rglob("*.py")):
        if py.name == "auth_modes.py":
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None) or []
            if not (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                continue
            if _is_click_command(node):
                help_bodies_scanned += 1
                continue  # this docstring IS the --help body — scan it
            docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings:
                continue
            found = joined.search(node.value)
            if found:
                offenders.append(
                    f"{py.relative_to(REPO)}:{node.lineno} -> {found.group(0)!r}")
    assert help_bodies_scanned > 50, (
        f"only {help_bodies_scanned} click docstrings were scanned as --help "
        f"bodies; the CLI has far more, so the decorator detection is broken "
        f"and this test is no longer looking at the help text at all")
    assert not offenders, (
        "a shipped string the user reads spells out the auth-mode list; render "
        "it with botainer.auth_modes.mode_list_hint() so there is one list:\n  "
        + "\n  ".join(offenders)
    )


def test_the_shared_summary_describes_the_topology_the_hooks_BUILD():
    """The shared one-liner said "copied into each project". It is a symlink.

    This matters beyond wording: a copy per project cannot be overwritten by
    one container, so the sentence made the true warning — an agent can
    overwrite the credential every shared project uses — read as impossible.

    Both sides are asserted, so this fails whichever one moves: the summary if
    it goes back to describing copies, and the hooks if they ever really do
    copy (at which point the summary is the thing to fix, not this test).
    """
    summary = AUTH_MODE_SUMMARY["shared"]
    assert "copie" not in summary and "copy" not in summary, (
        f"the shared summary describes a copy: {summary!r}. The per-project "
        f"credential is a symlink to one shared file.")
    assert "link" in summary, (
        f"the shared summary no longer says the projects are linked to one "
        f"file, which is the fact the overwrite warning depends on: {summary!r}")

    hooks = sorted((REPO / "plugins").glob("agent-*-shared/hooks/pre_session.py"))
    assert len(hooks) >= 2, (
        f"expected a shared pre_session hook per agent family, found {hooks} — "
        f"if the glob broke this test asserts nothing")
    for hook in hooks:
        src = hook.read_text()
        assert "os.symlink(expected_target" in src, (
            f"{hook.relative_to(REPO)} no longer symlinks the per-project "
            f"credential at the shared file, so AUTH_MODE_SUMMARY['shared'] "
            f"('linked into each project') is now the wrong description")


def _walk_commands(group, path=()):
    """Yield (dotted-name, command) for every command in the click tree."""
    import click
    yield ".".join(path) or "botainer", group
    if isinstance(group, click.Group):
        for name in group.list_commands(None):  # type: ignore[arg-type]
            sub = group.get_command(None, name)  # type: ignore[arg-type]
            if sub is not None:
                yield from _walk_commands(sub, path + (name,))


def test_every_auth_mode_option_is_a_choice_over_AUTH_MODES():
    """The SIBLING of the test above, and the half that was missing.

    That test scans the AST for a `click.Choice([...])` literal holding two or
    more mode names — it catches a SECOND source of truth. It is structurally
    incapable of catching NO source of truth: a bare

        @click.option("--auth-mode", default=None)

    contains no Call node to match, so it passes in silence.

    That is not hypothetical. `botainer hpc submit --auth-mode` shipped exactly
    like that. `_sbatch_token` accepts any word and
    `_apply_auth_mode_override_in_memory` treats an unrecognised mode as a
    NO-OP plus one stderr line, so one typo —

        $ botainer hpc submit ... --yes --auth-mode isolate
        [botainer] --auth-mode='isolate': no installed 'isolate' variant ...
        Submitted batch job 999001                              exit 0

    — submitted in the CONFIG's mode. On a cluster that is `shared`, i.e. the
    credential bound rw into an unattended job, while the user was asking for
    the opposite. The correctly-spelled value refused; the TYPO was the
    permissive outcome.

    So this walks the live command tree rather than the source. An option that
    is absent, renamed or retyped cannot hide from introspection the way it can
    hide from a grep for a literal.
    """
    import click

    from botainer.cli.main import cli

    offenders = []
    checked = 0
    for name, cmd in _walk_commands(cli):
        for param in cmd.params:
            if "--auth-mode" not in getattr(param, "opts", []):
                continue
            checked += 1
            if not isinstance(param.type, click.Choice):
                offenders.append(
                    f"{name} --auth-mode is {param.type!r}, not a Choice — an "
                    f"unrecognised mode becomes a silent no-op")
            elif tuple(param.type.choices) != tuple(AUTH_MODES):
                offenders.append(
                    f"{name} --auth-mode offers {tuple(param.type.choices)}, "
                    f"not AUTH_MODES {tuple(AUTH_MODES)}")

    assert checked, (
        "no command offers --auth-mode, so this test asserted nothing. Either "
        "the flag was renamed or the tree walk is broken — both are failures.")
    assert not offenders, (
        "an --auth-mode option that does not validate its value:\n  "
        + "\n  ".join(offenders))
