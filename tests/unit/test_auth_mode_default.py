"""The default authentication mode has one source of truth.

The model's default and the policy template written by setup must agree. A model
default only applies when the saved policy omits that key; a conflicting template
would override it on every new setup. These tests assert agreement across those
paths rather than checking an isolated constant."""
from __future__ import annotations

import ast
import pathlib

from botainer.core.policy import SitePolicy
from botainer.state import dir as state_dir

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _template_default() -> str:
    """The `default_auth_mode` literal in the policy.yaml template setup writes.

    Read from the SOURCE by AST rather than by calling the writer, because the
    writer needs a state root and this is a question about what the code says,
    not what one run produced.
    """
    tree = ast.parse((_REPO / "botainer/state/dir.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "default_auth_mode"
                    and isinstance(v, ast.Constant)):
                return v.value
    raise AssertionError("no default_auth_mode literal in the policy template")


def test_the_template_and_the_class_default_agree() -> None:
    """THE regression guard. Two answers existed and the losing one was the
    one that shipped."""
    assert _template_default() == SitePolicy().default_auth_mode, (
        "the policy.yaml template setup writes disagrees with "
        "SitePolicy.default_auth_mode. The TEMPLATE wins for every user who has "
        "run `botainer setup`, so a change to the class default alone is "
        "invisible to them — that is exactly #210."
    )


def test_the_default_is_isolated() -> None:
    # Pin the chosen default separately from the agreement check above;
    # changing this value must not weaken the check that policy layers agree.
    assert SitePolicy().default_auth_mode == "isolated"


def test_no_module_hardcodes_a_mode_where_the_policy_should_decide() -> None:
    """`x.default_auth_mode or "shared"` was a THIRD copy, in auth.py.

    A literal fallback beside a policy read is the same defect one level down:
    it answers the question locally instead of asking the one source. Caught by
    shape — a BoolOp whose left side reads `.default_auth_mode` and whose right
    side is a bare string.
    """
    offenders = []
    for rel in ("botainer/cli/auth.py", "botainer/cli/setup.py",
                "botainer/core/config.py", "botainer/core/composition.py"):
        path = _REPO / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
                continue
            reads_policy = any(
                isinstance(v, ast.Attribute) and v.attr == "default_auth_mode"
                for v in node.values)
            literal = [v for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str)
                       and v.value]
            if reads_policy and literal:
                offenders.append(f"{rel}:{node.lineno}: falls back to "
                                 f"{literal[0].value!r}")
    assert not offenders, (
        "a hardcoded auth mode sits beside a policy read:\n  "
        + "\n  ".join(offenders)
        + "\n\nRead SitePolicy().default_auth_mode instead — a second literal is "
          "how #210 happened."
    )


def test_an_existing_root_keeps_the_mode_it_already_has(tmp_path, monkeypatch) -> None:
    """Changing the default must not relocate a live user's credentials.

    The merge path only ADDS keys the on-disk policy lacks. A root that already
    says `shared` keeps saying it — silently moving where an existing project's
    login comes from is the #122/#149 class of surprise, and the fact that the
    new default is safer does not make the move consensual.
    """
    import yaml

    root = tmp_path / "root"
    (root / "state").mkdir(parents=True)
    policy = root / "policy.yaml"
    policy.write_text("version: policy-v1\ndefault_auth_mode: shared\n")
    monkeypatch.setenv("MY_BOTAINER", str(root))

    state_dir.ensure_user_state_dir(create_if_missing=True)
    # The merge path: re-running setup on an existing root must not rewrite a
    # key the file already has.
    state_dir.write_default_policy(root, allow_tiers=["core"])

    assert yaml.safe_load(policy.read_text())["default_auth_mode"] == "shared"
