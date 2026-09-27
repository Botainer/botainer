"""What `start` can be told, the PREVIEW commands must be able to be told (#204).

`botainer dry-run` and `botainer inspect` exist for one reason: to show you what
`botainer start` would do before it does it. That promise fails silently if the
preview cannot be given the same inputs.

It was failing. `start` forwards six things to `compose_session`; the two
preview commands forwarded ONE and hardcoded two as constants:

    start.py     runtime_choice=<name>  identity_accept=<name>  fork=<name>
                 auth_mode_override=<name>  auth_profile_override=<expr>
                 agent_override=<name>
    dry_run.py   runtime_choice="auto"   identity_accept=False   agent_override=<name>
    inspect.py   runtime_choice="auto"   identity_accept=False   agent_override=<name>

A hardcoded constant is the dangerous shape, not the missing kwarg: in a diff it
LOOKS like the parameter is handled. `runtime_choice="auto"` reads as coverage
and is the opposite — docker and apptainer render completely different argv, so
the preview was structurally capable of showing a different session than the one
that runs. `auth_mode_override` decides which CREDENTIAL is bound.

WHY THIS IS A TEST AND NOT THREE EDITS. The three flags were the symptom. The
defect is that nothing connected the two sets, so the next option added to
`start` would have quietly re-opened the gap — which is how it opened in the
first place (`--agent` was added to `start` in #112 and reached the previews
much later, reported as "codex is messed up" because there was no way to look).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
START = REPO / "botainer" / "cli" / "start.py"
PREVIEWS = {
    "dry_run.py": REPO / "botainer" / "cli" / "dry_run.py",
    "inspect.py": REPO / "botainer" / "cli" / "inspect.py",
}

#: Kwargs a preview command must NOT forward, each with the reason. These are
#: not oversights, and recording them here is what stops a later reader
#: "completing" the set and giving a read-only command a write.
WRITES_SO_EXCLUDED = {
    "fork": (
        "--fork calls write_project_id(..., overwrite=True) — it MINTS A NEW "
        "PROJECT ID and rewrites .botainer/project-id. A command that promises "
        "to change nothing cannot offer it."
    ),
    "identity_accept": (
        "--accept-identity-change appends to path_history and writes meta.json. "
        "Same reason: it resolves an ambiguity by RECORDING an answer."
    ),
}


def _hoisted_dicts(tree: ast.AST) -> dict[str, dict[str, str]]:
    """`name` -> {kwarg: node type} for each `name = dict(k=v, …)` / `{...}`.

    A caller may hoist its compose arguments into one dict and splat it — and
    `start` now does, precisely so its launch compose and `--preflight`'s
    per-runtime composes cannot drift apart. The AST walk below has to follow
    that, or it reports ONE kwarg and every comparison in this file becomes
    vacuous. (It did: the row-57 commit made this file's own
    `test_the_extractor_is_looking_at_something` guard fire, which is what that
    guard is for.)
    """
    out: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "dict"):
            out[target.id] = {k.arg: type(k.value).__name__
                              for k in value.keywords if k.arg}
        elif isinstance(value, ast.Dict):
            out[target.id] = {
                k.value: type(v).__name__
                for k, v in zip(value.keys, value.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    return out


def _compose_kwargs(path: pathlib.Path) -> dict[str, str]:
    """kwarg name -> the AST node type of its value, for the compose_session call.

    The node type is the point. `Name` means it came from a click parameter, so
    the caller can influence it. `Constant` means it is nailed shut — which is
    what made the old gap invisible.

    `**hoisted` is followed to the dict it names, so hoisting is neither a way
    to hide a kwarg from this check nor a reason for the check to go blind.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hoisted = _hoisted_dicts(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compose_session"):
            found = {k.arg: type(k.value).__name__
                     for k in node.keywords if k.arg}
            for k in node.keywords:
                if k.arg is None and isinstance(k.value, ast.Name):
                    resolved = hoisted.get(k.value.id)
                    assert resolved, (
                        f"{path.name} splats **{k.value.id} into "
                        f"compose_session and this test cannot see what is in "
                        f"it — resolve it here rather than letting every "
                        f"assertion below pass on an empty set"
                    )
                    found.update(resolved)
            return found
    raise AssertionError(f"no compose_session(...) call found in {path.name}")


def _caller_supplied(path: pathlib.Path) -> set[str]:
    """Kwargs whose value comes from the caller, not from a literal."""
    return {k for k, kind in _compose_kwargs(path).items()
            if kind not in ("Constant",)}


@pytest.mark.parametrize("preview", sorted(PREVIEWS))
def test_the_preview_accepts_everything_start_forwards(preview):
    """The load-bearing assertion. Anything `start` can be told that changes
    the composed plan, a preview must be able to be told too — or be on the
    excluded list with a stated reason."""
    start_supplied = _caller_supplied(START)
    preview_supplied = _caller_supplied(PREVIEWS[preview])

    missing = sorted(start_supplied - preview_supplied - set(WRITES_SO_EXCLUDED))
    assert not missing, (
        f"{preview} cannot be told {missing}, which `start` forwards to the "
        f"composer.\n\n  A preview that cannot be given the same inputs as the "
        f"real thing is\n  capable of showing a different session than the one "
        f"that runs.\n\n  Add the option and forward it — or, if it WRITES, add "
        f"it to\n  WRITES_SO_EXCLUDED in this file with the reason."
    )


@pytest.mark.parametrize("preview", sorted(PREVIEWS))
def test_a_hardcoded_constant_is_not_counted_as_coverage(preview):
    """`runtime_choice="auto"` was the shape that hid this for months: present
    in the call, impossible to influence, and indistinguishable from real
    handling unless you look at the value."""
    kinds = _compose_kwargs(PREVIEWS[preview])
    nailed = {k for k, kind in kinds.items() if kind == "Constant"}
    unexplained = nailed - set(WRITES_SO_EXCLUDED)
    assert not unexplained, (
        f"{preview} passes {sorted(unexplained)} as a literal. If that is "
        f"deliberate, say why in WRITES_SO_EXCLUDED; if not, the preview "
        f"silently ignores what the caller asked for."
    )


def test_the_write_exclusions_are_real_and_not_a_dumping_ground():
    """An exclusion list is only honest if its entries are actually excluded
    AND actually write. Both halves checked: `start` really forwards them (so
    they are not stale), and each carries a reason."""
    start_supplied = _caller_supplied(START)
    for name, reason in WRITES_SO_EXCLUDED.items():
        assert name in start_supplied, (
            f"{name!r} is excluded from the previews but `start` no longer "
            f"forwards it — the entry is stale and should go"
        )
        assert len(reason) > 40, f"{name!r} is excluded without a real reason"
        for preview, path in PREVIEWS.items():
            assert name not in _caller_supplied(path), (
                f"{preview} now forwards {name!r}, which this file says WRITES:"
                f"\n  {reason}"
            )


def test_the_extractor_is_looking_at_something():
    """Every assertion above passes trivially if the AST walk finds nothing.
    Pin that `start` really does forward a non-trivial set."""
    supplied = _caller_supplied(START)
    assert len(supplied) >= 5, (
        f"only {len(supplied)} caller-supplied kwargs found in start.py's "
        f"compose_session call ({sorted(supplied)}) — the extractor has stopped "
        f"matching the code, so the comparisons above are vacuous"
    )
    assert "auth_mode_override" in supplied, "the credential-deciding one vanished"
