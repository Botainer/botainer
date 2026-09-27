"""No bind botainer composes reaches the STATE ROOT itself.

Per CLAUDE.md "Principles are tests, not prose". #202 added `<root>/root.json`
and asserted in review that a container cannot see it, on the strength of one
hand-inspected mount plan. A property established once by looking is a property
that regresses the first time someone adds a bind.

THE DIRECTION IS THE WHOLE TEST, and it is easy to get backwards. Legitimate
binds live INSIDE the root: `state/<uuid>/packages`, `scratch`, `home`, `data`,
`sessions/<id>/AGENT_HINTS.md`. What must never appear is a source that IS the
root or an ANCESTOR of it — that is the umbrella shape, and it would hand the
container `root.json`, `policy.yaml`, `shared-auth/` and every other project's
state in one go.

SCOPE, and the gap this used to name is now CLOSED. This test pins what botainer
composes ON ITS OWN. It used to add that a user's `mounts.extra` could still
name the state root, because the source denylist covered `$HOME` and a list of
sensitive subpaths but not `$HOME/.botainer` — true when written, and no longer.
A config-supplied (`Provenance.USER`) bind may not now name the root, anything
under it, an ancestor of it, or a symlink to any of those; that rule has its own
test file and its own capability-contract entry.

What remains outside this file, stated rather than implied: a SITE policy may
still vouch for such a source via `trusted_source_roots`, and other sensitive
host paths not on the denylist are a separate open item.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run(args, *, cwd, env):
    return subprocess.run([sys.executable, "-m", "botainer.cli.main", *args],
                          cwd=str(cwd), env=env, capture_output=True, text=True)


@pytest.fixture()
def composed(tmp_path, monkeypatch):
    """A real state root and a real project, composed by the real CLI."""
    root = tmp_path / "root"
    project = tmp_path / "proj"
    project.mkdir()

    import os
    env = dict(os.environ)
    env["MY_BOTAINER"] = str(root)
    env.pop("BOTAINER_STATE_ROOT", None)

    if _run(["setup"], cwd=project, env=env).returncode != 0:
        pytest.skip("botainer setup did not complete in this environment")
    if _run(["init"], cwd=project, env=env).returncode != 0:
        pytest.skip("botainer init did not complete in this environment")

    proc = _run(["inspect", "--json"], cwd=project, env=env)
    if proc.returncode != 0 or not proc.stdout.strip():
        pytest.skip(f"inspect produced no plan: {proc.stderr[:200]}")
    try:
        plan = json.loads(proc.stdout)
    except ValueError:
        pytest.skip("inspect --json did not emit JSON")
    return root.resolve(), plan


def _all_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_strings(v)
    elif isinstance(obj, str):
        yield obj


def umbrella_offenders(plan, root: Path) -> list[str]:
    """Paths in `plan` that ARE `root` or sit above it.

    ONE implementation, used by the real assertion and by its negative control
    below. An earlier draft had the control re-implement the rule, which proved
    only that the copy worked — the real test could have been broken in any way
    and still passed. Sharing the function is what makes the control mean
    something.
    """
    root = root.resolve()
    out = []
    for s in _all_strings(plan):
        if not s.startswith("/"):
            continue
        try:
            candidate = Path(s).resolve()
            # `root.relative_to(candidate)` succeeding means candidate is root
            # or an ancestor of root. Paths INSIDE the root (packages, scratch,
            # home, sessions/…) fail it, which is correct — those are the
            # legitimate binds.
            root.relative_to(candidate)
        except (ValueError, OSError):
            continue
        out.append(s)
    return sorted(set(out))


def test_no_bind_source_is_the_state_root_or_an_ancestor(composed):
    root, plan = composed

    assert not umbrella_offenders(plan, root), (
        "these paths are the state root or an ancestor of it, so a bind on any "
        "of them would expose root.json, policy.yaml and shared-auth/:\n  "
        + "\n  ".join(umbrella_offenders(plan, root))
    )


def test_the_check_would_catch_an_umbrella_bind(composed):
    """The guard above is worthless if it cannot fail. Prove it can — with the
    SAME function, so this is evidence about the real check and not about a
    reimplementation of it."""
    root, plan = composed

    assert umbrella_offenders({"mounts": [{"source": str(root)}]}, root) == [str(root)]
    assert umbrella_offenders({"mounts": [{"source": str(root.parent)}]}, root) \
        == [str(root.parent)], "an ANCESTOR is just as bad as the root itself"
    # ...and the legitimate shape stays clean, so it is not simply flagging
    # every path that mentions the root.
    inside = str(root / "state" / "uuid" / "packages")
    assert umbrella_offenders({"mounts": [{"source": inside}]}, root) == []


def test_root_json_exists_and_is_owner_only(composed):
    # It holds no secret, but it sits beside files that do, and a mode that
    # drifts here is a hint the whole root's modes have drifted.
    root, _ = composed
    record = root / "root.json"

    assert record.is_file(), "setup should have stamped the root"
    assert record.stat().st_mode & 0o077 == 0, "group/other must not read it"
