"""`botainer hpc setup` storage guidance — drift gate.

Earlier versions printed `export MY_BOTAINER=$SCRATCH/.botainer` as a
"suggested next step". Site scratch can be purged, which can remove
credentials, project UUIDs, built .sif images and the installed plugin tree.
Persistent state must not be directed to purgeable storage (DN-036).

These tests pin the guidance the command now prints so the dangerous
advice can't sneak back in via a refactor."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from botainer.cli.hpc import hpc as hpc_group


def test_setup_does_not_advise_redirecting_state_to_scratch(
    monkeypatch, tmp_path: Path
) -> None:
    """The setup command MUST NOT print `MY_BOTAINER=$SCRATCH/...` or any
    other recommendation to put botainer state on scratch. State (credentials,
    project UUIDs, .sif images, plugins) is unrecoverable if wiped by the
    cluster's auto-purge."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.chdir(tmp_path)
    # Use the bundled yale-grace profile by name; that's what triggered the
    # old advice block (scratch_template with $ expansion).
    result = CliRunner().invoke(
        hpc_group,
        ["setup", "--profile", "grace", "--non-interactive"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # Hard-fail any sentinel that looks like the old dangerous advice.
    forbidden = [
        "export MY_BOTAINER=/scratch",
        "export MY_BOTAINER=$SCRATCH",
        # No site-specific paths here — the personal-info gate would catch
        # them. The two patterns above cover the genuine shapes any cluster's
        # advice could take.
        "Redirect botainer's\nstate dir to scratch",
        "Redirect botainer's state dir to scratch",
    ]
    for f in forbidden:
        assert f not in result.output, (
            f"setup output contains dangerous advice {f!r}; the pre-2026-06-16 "
            f"block directs persistent state into purgeable scratch storage. "
            f"Replace with the storage-layout block."
        )


def test_setup_prints_storage_layout_guidance(
    monkeypatch, tmp_path: Path
) -> None:
    """Setup explicitly names what stays on $HOME (state, credentials, .sif)
    vs what goes on per-session scratch. The first user a new HPC user hears
    this from should be the launcher, not a forum post after they've lost
    their credentials to a purge."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        hpc_group,
        ["setup", "--profile", "grace", "--non-interactive"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # Names the layout sections.
    assert "Storage layout" in result.output
    assert "keep on $HOME" in result.output
    # Names what's at risk if state goes on purged storage.
    assert "credentials" in result.output.lower()
    assert "unrecoverable" in result.output.lower()
    # Names per-session scratch as the right place for per-session work.
    assert "per-session" in result.output.lower()
