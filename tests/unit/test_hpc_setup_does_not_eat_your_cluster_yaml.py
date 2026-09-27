"""`hpc setup` must not destroy a cluster.yaml you edited by hand. (#227)

Writing a bundled profile over an existing hand-edited cluster.yaml can
discard account settings and comments. The fixture uses the same state-root
layout as setup so the writer reaches the file being protected.

THE GUARD LIVES IN THE WRITER, not in the command. `hpc setup` already carries
a comment arguing exactly this for its trust label: "a chokepoint cannot be
forgotten by the next person who adds a fourth way to choose a profile." The
same holds for clobbering, and `write_user_profile` is the one place the profile
is persisted — so a caller that forgets is not expressible.

IT REFUSES RATHER THAN PROMPTING. This module has no terminal and more than one
caller; a prompt here would hang a non-interactive run and put UI in a state
module. Refusing names the file and the flag, which is the pattern `init`
already uses, and leaves the decision where it belongs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from botainer.core.refusal import Refused
from botainer.state import cluster_profile as cp

# Synthetic YAML used to check preservation of user edits and comments.
HAND_EDITED = """# MY hand-edited cluster config. An hour with the sysadmin.
id: us-yale-grace
account: my_real_pi_group          # the only place this is recorded
"""


@pytest.fixture
def state_root(tmp_path, monkeypatch) -> Path:
    """A state root at the DEFAULT location relative to HOME.

    Deliberately `HOME/.botainer`, not an unrelated directory: pointing
    MY_BOTAINER somewhere else is what made this look unreproducible the first
    time.
    """
    home = tmp_path / "home"
    root = home / ".botainer"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_BOTAINER", str(root))
    return root


def _a_profile():
    """A real bundled profile — the content only has to differ from the file.

    NO `pytest.skip` FALLBACK, deliberately. The first version of this helper
    guessed at a loader name that does not exist and skipped when it failed, so
    all six tests reported green while asserting nothing. A fixture that cannot
    produce its subject is a broken test, not an inapplicable one, and must say
    so loudly.
    """
    bundled = cp.list_bundled()
    assert bundled, (
        "no bundled cluster profiles found; this test cannot exercise the "
        "writer and must not pretend otherwise")
    return bundled[0]


def test_it_REFUSES_rather_than_overwriting_your_edits(state_root) -> None:
    """The defect: this used to succeed and print a green tick."""
    target = state_root / "cluster.yaml"
    target.write_text(HAND_EDITED)

    with pytest.raises(Refused) as exc:
        cp.write_user_profile(_a_profile())

    assert "already exists" in str(exc.value), str(exc.value)
    assert target.read_text() == HAND_EDITED, (
        "the file was modified despite the refusal")


def test_the_refusal_names_the_FILE_and_the_WAY_FORWARD(state_root) -> None:
    """A refusal a user cannot act on just moves the problem.

    Asserted separately so a bare "refused" cannot pass.
    """
    target = state_root / "cluster.yaml"
    target.write_text(HAND_EDITED)
    with pytest.raises(Refused) as exc:
        cp.write_user_profile(_a_profile())
    msg = str(exc.value)
    assert str(target) in msg, f"never named the file: {msg}"
    assert "--force" in msg, f"never said how to proceed: {msg}"


def test_force_writes_AND_keeps_the_old_file(state_root) -> None:
    """`--force` means "I meant it", not "throw it away"."""
    target = state_root / "cluster.yaml"
    target.write_text(HAND_EDITED)

    cp.write_user_profile(_a_profile(), force=True)

    backup = getattr(cp.write_user_profile, "last_backup", None)
    assert backup is not None and Path(backup).exists(), (
        "forced overwrite left no backup")
    assert "my_real_pi_group" in Path(backup).read_text(), (
        "the backup does not contain what was overwritten")
    assert yaml.safe_load(target.read_text())["version"] == "cluster-profile-v1"


def test_a_second_force_does_not_clobber_the_first_backup(state_root) -> None:
    """Otherwise the second --force destroys the copy the first one made.

    Same data loss, one level removed — and the level nobody checks.
    """
    target = state_root / "cluster.yaml"
    target.write_text(HAND_EDITED)
    cp.write_user_profile(_a_profile(), force=True)
    first = Path(getattr(cp.write_user_profile, "last_backup"))

    target.write_text("id: something-else\n")
    cp.write_user_profile(_a_profile(), force=True)
    second = Path(getattr(cp.write_user_profile, "last_backup"))

    assert first != second, "the second backup reused the first path"
    assert "my_real_pi_group" in first.read_text(), (
        "the first backup was overwritten by the second --force")


def test_writing_the_SAME_content_is_a_silent_no_op(state_root) -> None:
    """Re-running setup with the same answers must not refuse.

    Without this, the guard fires on the ordinary repeat-the-command path and
    becomes noise — which is how a warning gets trained away.
    """
    prof = _a_profile()
    cp.write_user_profile(prof)                 # first write, no file yet
    cp.write_user_profile(prof)                 # identical: must not raise
    assert getattr(cp.write_user_profile, "last_backup", None) is None, (
        "an identical rewrite made a backup, so it was treated as a clobber")


def test_a_fresh_install_still_just_works(state_root) -> None:
    """No file yet is the common case and must stay frictionless."""
    cp.write_user_profile(_a_profile())
    assert (state_root / "cluster.yaml").exists()
