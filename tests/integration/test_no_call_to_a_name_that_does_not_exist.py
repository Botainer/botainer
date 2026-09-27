"""A blanket `except Exception` turns a typo into silent dead code.

THE INCIDENT. Three call sites called `config.load_project_config`. That
attribute does not exist — the function is `load_config` — so each raised
`AttributeError` *before its argument was evaluated*, and each was caught by a
surrounding `except Exception` that fell back to a plausible value. Nothing
looked wrong anywhere.

The worst of the three was a security guard. `cli/hpc.py` used the result to
decide `_fam`, which was therefore always `None`, so
`confirm_no_other_shared_session` NEVER RAN on `hpc submit`. That function's own
docstring says it exists because the check "was written in start.py first and
not here, which put the guard on the laptop path and left the CLUSTER path —
the product's main path — unprotected". The fix for that sibling drift was
itself written with a typo, so the drift was never actually closed, and the test
guarding it asserted only that the CALL APPEARED IN THE SOURCE — which it did.

WHY THIS TEST AND NOT THREE. Fixing the three sites fixes three typos. The class
is "an attribute lookup on an imported module, inside a handler broad enough to
swallow AttributeError" — and every future instance is as invisible as these
were. A grep cannot see it (the name looks plausible), a type checker is not run
in the gate, and the fallback is by construction something that looks like an
answer. So: resolve every such call against the real module, at test time.

SCOPE, honestly stated. This resolves calls of the form `alias.attr(...)` where
`alias` came from `from botainer.x import y as alias`. It does NOT cover
`getattr`, deep attribute chains, or names resolved at runtime — so it is a
FILTER over the commonest shape, not a proof. Its value is that the three real
instances, and anything written the same way, cannot come back silently.
"""
from __future__ import annotations

import ast
import importlib
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """`from botainer.core import config as _cfgm` -> {'_cfgm': 'botainer.core.config'}."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module \
           and node.module.startswith("botainer") and node.level == 0:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("botainer"):
                    out[a.asname or a.name.split(".")[0]] = a.name
    return out


def _locally_assigned(tree: ast.AST) -> set[str]:
    """Names bound somewhere in the module, which therefore may SHADOW an alias.

    Without this the scan reports `plugin_hooks.append` in composition.py: an
    import alias of that name exists, and so does a local
    `plugin_hooks: list[HookSpec] = []` that shadows it. A list has `.append`;
    the module does not. Skipping shadowed names is what keeps this test from
    crying wolf — which this repo treats as a defect in its own right.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, (ast.AnnAssign,)) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def test_no_shipped_call_targets_a_module_attribute_that_does_not_exist():
    offenders: list[str] = []
    for path in sorted((REPO / "botainer").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                       # not ours to police here
            continue
        aliases = _module_aliases(tree)
        if not aliases:
            continue
        shadowed = _locally_assigned(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)):
                continue
            name = node.func.value.id
            if name not in aliases or name in shadowed:
                continue
            try:
                mod = importlib.import_module(aliases[name])
            except Exception:                     # noqa: BLE001 — not this test's job
                continue
            if not hasattr(mod, node.func.attr):
                offenders.append(
                    f"{path.relative_to(REPO)}:{node.lineno} "
                    f"{name}.{node.func.attr}(...) -> {aliases[name]} has no "
                    f"attribute {node.func.attr!r}")

    assert not offenders, (
        "a call names a module attribute that does not exist. Raised as "
        "AttributeError BEFORE the arguments are evaluated, and — if any "
        "enclosing handler catches Exception — swallowed into a plausible "
        "fallback, which is how a security guard came to never run:\n  "
        + "\n  ".join(offenders))
