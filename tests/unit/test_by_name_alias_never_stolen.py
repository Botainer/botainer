"""Two projects must never share a by-name alias.

The alias is `state/by-name/<name>-<short-uuid>` → `../<uuid>`. `<short-uuid>`
is 8 hex chars, so a collision needs two projects with the SAME sanitized name
AND the same 32-bit prefix — around one in 4.3 billion per pair. Small enough
that it was reasonable to pick 8.

But the old code, on finding the alias taken, called `unlink()` and pointed it
at itself. So the rare event was not "an error"; it was "one project silently
became unreachable by name". These tests pin the property rather than the odds,
because the plan is to INVERT the layout — make `<name>-<short>` the real
directory and the bare uuid a symlink to it — and under that layout a collision
means two projects sharing one state directory: same /packages, same credential
dir. That must not rest on a probability.
"""

from __future__ import annotations

import os
from pathlib import Path

from botainer.state.dir import StatePaths, _refresh_by_name_symlink

# Same 8-hex prefix, different uuids. Contrived on purpose: it is the case that
# would otherwise show up once in billions and be impossible to reproduce.
UUID_A = "deadbeef-1111-4111-8111-111111111111"
UUID_B = "deadbeef-2222-4222-8222-222222222222"


def _setup(tmp_path: Path) -> StatePaths:
    paths = StatePaths(root=tmp_path)
    for u in (UUID_A, UUID_B):
        (paths.state_dir / u).mkdir(parents=True, exist_ok=True)
    return paths


def _aliases(paths: StatePaths) -> dict[str, str]:
    byname = paths.state_dir / "by-name"
    if not byname.is_dir():
        return {}
    return {p.name: Path(os.readlink(p)).name for p in byname.iterdir()}


def test_colliding_short_uuid_does_not_steal_the_alias(tmp_path):
    paths = _setup(tmp_path)
    _refresh_by_name_symlink(paths, UUID_A, "analysis")
    _refresh_by_name_symlink(paths, UUID_B, "analysis")

    aliases = _aliases(paths)
    assert len(aliases) == 2, (
        f"expected an alias per project, got {aliases} — the second project "
        f"took the first one's name"
    )
    assert set(aliases.values()) == {UUID_A, UUID_B}


def test_alias_is_stable_across_repeated_refresh(tmp_path):
    # Called on every session start; must be idempotent, not accumulating.
    paths = _setup(tmp_path)
    for _ in range(3):
        _refresh_by_name_symlink(paths, UUID_A, "analysis")
        _refresh_by_name_symlink(paths, UUID_B, "analysis")
    assert len(_aliases(paths)) == 2


def test_an_existing_alias_is_never_moved(tmp_path):
    """Once a project owns an alias it keeps it, even as neighbours appear.

    This is the property that actually protects users: a path they have in a
    shell history or a script keeps meaning the same project. It is weaker than
    order-independence, and deliberately so — see _unique_link_path's
    LIMITATION note. Which of two COLLIDING projects gets the short form does
    depend on who registered first; what must never happen is an alias moving
    after the fact.
    """
    paths = _setup(tmp_path)
    _refresh_by_name_symlink(paths, UUID_A, "analysis")
    before = _aliases(paths)
    assert before == {"analysis-deadbeef": UUID_A}

    # A colliding project arrives, then both refresh repeatedly.
    _refresh_by_name_symlink(paths, UUID_B, "analysis")
    for _ in range(3):
        _refresh_by_name_symlink(paths, UUID_A, "analysis")
        _refresh_by_name_symlink(paths, UUID_B, "analysis")

    after = _aliases(paths)
    assert after["analysis-deadbeef"] == UUID_A, "the incumbent's alias moved"
    assert len(after) == 2


def test_non_colliding_projects_keep_the_short_form(tmp_path):
    # The collision path must not lengthen aliases for everyone else.
    paths = StatePaths(root=tmp_path)
    u = "0123abcd-1111-4111-8111-111111111111"
    (paths.state_dir / u).mkdir(parents=True, exist_ok=True)
    _refresh_by_name_symlink(paths, u, "analysis")
    assert list(_aliases(paths)) == ["analysis-0123abcd"]


def test_renaming_the_folder_moves_the_alias_instead_of_adding_one(tmp_path):
    """`project_name` is `project_root.name`, re-read on every compose.

    Creating without pruning meant a rename ADDED an alias and left the old one:
    `old-name-abcd1234` and `new-name-abcd1234` both live, both the same
    project, one more per rename. The alias must follow the folder.
    """
    paths = _setup(tmp_path)
    _refresh_by_name_symlink(paths, UUID_A, "old-name")
    _refresh_by_name_symlink(paths, UUID_A, "new-name")
    assert _aliases(paths) == {"new-name-deadbeef": UUID_A}

    _refresh_by_name_symlink(paths, UUID_A, "final-name")
    assert _aliases(paths) == {"final-name-deadbeef": UUID_A}


def test_pruning_never_removes_a_DIFFERENT_projects_alias(tmp_path):
    """The prune is scoped to one uuid; a colliding neighbour is untouched.

    This is the pairing that makes the two rules coherent: a project's own
    stale name goes (the user renamed it), another project's name never does
    (removing it would silently repoint their path at a stranger).
    """
    paths = _setup(tmp_path)
    _refresh_by_name_symlink(paths, UUID_A, "shared")
    _refresh_by_name_symlink(paths, UUID_B, "shared")
    aliases = _aliases(paths)
    assert len(aliases) == 2
    assert set(aliases.values()) == {UUID_A, UUID_B}

    # And a rename of one must not disturb the other.
    _refresh_by_name_symlink(paths, UUID_A, "renamed")
    after = _aliases(paths)
    assert set(after.values()) == {UUID_A, UUID_B}
    assert any(k.startswith("renamed-") for k in after)
    assert any(k.startswith("shared-") for k in after)
