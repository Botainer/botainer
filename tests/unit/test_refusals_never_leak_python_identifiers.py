"""No user-facing refusal may print a Python identifier at the user.

FOUND BY RUNNING IT (2026-08-31), while checking whether a user could install an
MCP-server plugin:

    $ botainer plugin add <dir>
    refused: RefusalCategory.PLUGIN_TIER_NOT_ALLOWED: plugin 'lean-mcp' ...

Every other refusal prints kebab-case. `botainer/cli/plugin.py` formatted the
ENUM MEMBER where the shared handler (`_refusal_handler.py:64`) formats
`.value`. `RefusalCategory` is `(str, Enum)`, which used to make those the same
string — on Python 3.11+ it does not, and this repo runs 3.14. So the defect
arrived with an interpreter upgrade, in a call site that had been correct when
written. Nothing would have caught it: it is not a crash, no test asserted the
message, and the code reads fine.

WHY A TEST AND NOT JUST THE ONE-CHARACTER FIX. There is exactly one such call
site today, which is precisely when a guard is cheap. Without it the next
hand-rolled refusal print reintroduces the class, silently, for the same reason
this one did. (CLAUDE.md, "structure over rules": the rule backs up the gap that
`@handle_refusals` does not cover — a command that catches its own exception and
formats the message itself.)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "botainer" / "cli"

# The names whose str() is a Python identifier rather than the value.
_ENUMS = {"RefusalCategory"}


def _formats_a_bare_enum(node: ast.AST) -> list[str]:
    """f-string interpolations of `<something>.category` with no `.value`.

    AST, not a substring search: a comment or a docstring mentioning
    `{exc.category}` must not trip this, and the assertion-shape gate is right
    that grepping source proves nothing.
    """
    bad = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.JoinedStr):
            continue
        for part in sub.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            v = part.value
            # `{exc.category}` — an Attribute ending in `category`, NOT
            # `{exc.category.value}` (whose outermost attr is `value`).
            if isinstance(v, ast.Attribute) and v.attr == "category":
                bad.append(ast.unparse(sub))
    return bad


@pytest.mark.parametrize(
    "path", sorted(CLI.rglob("*.py")), ids=lambda p: p.name)
def test_no_cli_module_formats_a_bare_refusal_category(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = _formats_a_bare_enum(tree)
    assert not offenders, (
        f"{path.name} interpolates a RefusalCategory MEMBER, which renders as "
        f"'RefusalCategory.SOME_NAME' on Python 3.11+. Use `.value` (or route "
        f"the command through @handle_refusals, which already does). "
        f"Offending f-string(s): {offenders}"
    )


def test_the_enum_really_does_render_as_an_identifier() -> None:
    """Pins the interpreter behaviour the test above exists for.

    If a future Python makes `str(member)` return the value again, this fails
    and the guard above can be reconsidered rather than cargo-culted.
    """
    from botainer.core.refusal import RefusalCategory

    member = RefusalCategory.PLUGIN_TIER_NOT_ALLOWED
    assert str(member) == "RefusalCategory.PLUGIN_TIER_NOT_ALLOWED"
    assert member.value == "plugin-tier-not-allowed"
