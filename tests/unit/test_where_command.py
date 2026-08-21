"""`botainer where` — find the big directories, and say which are safe to delete.

User question: "is there an easy way to tell where all the data
(especially large files) are stored? an easy way to move them?" Neither existed.

Deliberately NOT relocatable placement. That needs a config block, defaults
resolution, a move command, and a migration for the absolute `image:` paths in
existing configs. Seeing what is big and deleting it costs one read-only command
and, for someone who wants their quota back, is most of the value — because the
things that grow (packages/, scratch/, home/) are regenerable by construction.
The user's framing: "it should be relatively easy to find packages folders to
delete them manually."

The load-bearing property is the SAFE/UNSAFE split. Marking the credential
directory reclaimable would cost someone their login, so that direction is
tested explicitly rather than assumed.
"""
from __future__ import annotations

import uuid

import pytest
from click.testing import CliRunner

from botainer.cli.where import where


@pytest.fixture
def populated(tmp_path, monkeypatch):
    """A state root shaped like a real one, with measurable content."""
    root = tmp_path / "state-root"
    uid = str(uuid.uuid4())
    proj = root / "state" / uid
    for sub in ("packages/pip", "scratch", "home/.npm", "sessions", "data/agent-claude"):
        (proj / sub).mkdir(parents=True)
    # scratch is deliberately BIGGER than packages: the render loop visits
    # packages first, so only a real size sort can put scratch on top.
    (proj / "packages" / "pip" / "big.bin").write_bytes(b"x" * 100_000)
    (proj / "scratch" / "mid.bin").write_bytes(b"x" * 300_000)
    (proj / "data" / "agent-claude" / ".credentials.json").write_bytes(b"x" * 200)
    (root / "plugins").mkdir()
    (root / "policy.yaml").write_text("{}")
    monkeypatch.setenv("MY_BOTAINER", str(root))
    monkeypatch.setattr(
        "botainer.state.dir.list_projects",
        lambda: [type("E", (), {"uuid": uid, "last_path": str(tmp_path / "myproj"),
                                "path_exists": True, "paths": (),
                                "display_name": "myproj", "last_session_at": "",
                                "last_session_runtime": "", "sessions_dir_count": 0})()])
    return root, proj


def _run() -> str:
    res = CliRunner().invoke(where, [])
    assert res.exit_code == 0, res.output
    return res.output


def test_it_names_the_state_root_and_how_it_was_chosen(populated) -> None:
    """The original question. `MY_BOTAINER` vs default is the thing that
    silently bites — a shell missing the export gets a second, empty root."""
    root, _ = populated
    out = _run()
    assert str(root) in out
    assert "MY_BOTAINER" in out


def test_regenerable_dirs_are_marked_reclaimable_with_a_command(populated) -> None:
    _, proj = populated
    out = _run()
    assert "Reclaimable" in out
    assert f"rm -rf {proj / 'packages'}" in out, out


def test_the_credential_directory_is_never_offered_for_deletion(populated) -> None:
    """The direction that matters. Offering `rm -rf .../data` would cost the
    user their login — worse than the problem this command solves."""
    _, proj = populated
    out = _run()
    assert f"rm -rf {proj / 'data'}" not in out
    assert f"rm -rf {proj / 'sessions'}" not in out
    # ...and it says so rather than merely omitting them
    assert "do NOT delete" in out


def test_biggest_first(populated) -> None:
    """A reclaim list in arbitrary order makes the user read all of it."""
    _, proj = populated
    out = _run()
    body = out[out.index("Reclaimable"):]
    assert body.index(str(proj / "scratch")) < body.index(str(proj / "packages")), (
        "reclaim list is not sorted by size — scratch (300K) must precede "
        "packages (100K) despite packages being rendered first")


def test_it_deletes_nothing(populated) -> None:
    """Read-only by contract: it prints commands, it does not run them."""
    _, proj = populated
    _run()
    assert (proj / "packages" / "pip" / "big.bin").exists()
    assert (proj / "scratch" / "mid.bin").exists()
