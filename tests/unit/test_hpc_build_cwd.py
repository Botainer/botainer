"""Regression: `botainer hpc build` must run `apptainer build` from the plugin
directory, because apptainer resolves `%files` SOURCE paths relative to the
build's WORKING DIRECTORY (not the .def's location). The bundled .defs copy a
relative sibling `entrypoint_wrap.sh`, so a build launched from the user's
project dir fails with "cannot stat 'entrypoint_wrap.sh'" (Grace host-test,
2026-07-02)."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from botainer.cli import hpc


def test_hpc_build_runs_from_plugin_dir(tmp_path, monkeypatch) -> None:
    pdir = tmp_path / "plugins" / "agent-claude"
    pdir.mkdir(parents=True)
    (pdir / "agent-claude.def").write_text(
        "Bootstrap: docker\nFrom: x\n%files\n    entrypoint_wrap.sh /usr/local/bin/e\n"
    )
    (pdir / "entrypoint_wrap.sh").write_text("#!/bin/sh\n")
    plugin = SimpleNamespace(name="agent-claude", plugin_dir=pdir)

    images = tmp_path / "images"
    images.mkdir()
    paths = SimpleNamespace(
        images_dir=images,
        apptainer_sif_path=lambda n: images / f"botainer-{n}.sif",
    )

    monkeypatch.setattr(hpc.shutil, "which", lambda b: "/usr/bin/apptainer")
    import botainer.plugins.lifecycle as life
    monkeypatch.setattr(life, "list_installed", lambda: [plugin])
    import botainer.state.dir as sd
    monkeypatch.setattr(sd, "ensure_user_state_dir", lambda create_if_missing=True: paths)
    monkeypatch.setattr(hpc.profile_module, "active_profile", lambda: None)

    captured: dict = {}

    def fake_call(cmd, env=None, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        # apptainer would create the .sif; emulate it so the success message's
        # stat() doesn't fail. The sif path is the second-to-last argv token.
        # Must exceed _MIN_PLAUSIBLE_SIF_BYTES: `hpc build` now REFUSES a
        # zero/tiny .sif, because an apptainer that exits 0 having produced
        # nothing used to print "✓ built (0 MiB)" and send the user downstream
        # to debug the wrong layer. 2 KiB was below that floor.
        from pathlib import Path
        Path(cmd[-2]).write_bytes(b"x" * (2 * 1024 * 1024))
        return 0

    monkeypatch.setattr(hpc.subprocess, "call", fake_call)

    res = CliRunner().invoke(hpc.build, ["agent-claude"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    # The critical assertion: build ran from the plugin dir (so the relative
    # %files entrypoint_wrap.sh resolves), and the .def/.sif args are absolute.
    assert captured["cwd"] == str(pdir), captured
    assert captured["cmd"][0:2] == ["apptainer", "build"]
    assert captured["cmd"][-1].endswith("/agent-claude.def")
    assert captured["cmd"][-1].startswith("/")  # absolute def path


def test_bundled_defs_files_sources_exist_relative_to_def() -> None:
    """The contract the cwd fix relies on: every bundled .def's `%files` SOURCE
    is a plain relative filename that EXISTS in the plugin dir (so a
    build-from-plugin-dir resolves it). Guards against a future .def referencing
    a moved/absent file."""
    import re
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    checked = 0
    for defp in (repo / "plugins").rglob("*.def"):
        text = defp.read_text()
        m = re.search(r"^%files\s*$(.*?)^%", text, re.S | re.M)
        if not m:
            continue
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            src = line.split()[0]
            assert not src.startswith("/"), f"{defp}: %files source {src!r} is absolute"
            assert (defp.parent / src).exists(), (
                f"{defp}: %files source {src!r} not found next to the .def"
            )
            checked += 1
    assert checked > 0  # sanity: we actually inspected some %files entries


def test_hpc_build_refuses_a_zero_byte_sif(tmp_path, monkeypatch) -> None:
    """A zero exit is not a built image.

    OBSERVED in a UX walk on a simulated login node: apptainer was on PATH,
    `apptainer build` exited 0 and produced nothing, and botainer printed

        ✓ built /…/botainer-agent-claude.sif (0 MiB)

    It had already computed `st_size`, got 0, and put a green tick beside it.
    `doctor --strict` — the command the guide sends you to before launching
    jobs — passed the same empty file.

    A green tick on an empty image is worse than a red X: the user spent the
    rest of their session debugging authentication, two layers above the actual
    failure. Check the ARTEFACT, not the exit code.
    """
    pdir = tmp_path / "plugins" / "agent-claude"
    pdir.mkdir(parents=True)
    (pdir / "agent-claude.def").write_text("Bootstrap: docker\nFrom: x\n")
    plugin = SimpleNamespace(name="agent-claude", plugin_dir=pdir)
    images = tmp_path / "images"
    images.mkdir()
    paths = SimpleNamespace(
        images_dir=images,
        apptainer_sif_path=lambda n: images / f"botainer-{n}.sif",
    )
    monkeypatch.setattr(hpc.shutil, "which", lambda b: "/usr/bin/apptainer")
    import botainer.plugins.lifecycle as life
    monkeypatch.setattr(life, "list_installed", lambda: [plugin])
    import botainer.state.dir as sd
    monkeypatch.setattr(sd, "ensure_user_state_dir", lambda create_if_missing=True: paths)
    monkeypatch.setattr(hpc.profile_module, "active_profile", lambda: None)

    def fake_call(cmd, env=None, cwd=None):
        from pathlib import Path as _P
        _P(cmd[-2]).write_bytes(b"")      # "succeeds", produces nothing
        return 0

    monkeypatch.setattr(hpc.subprocess, "call", fake_call)

    res = CliRunner().invoke(hpc.build, ["agent-claude"], catch_exceptions=False)
    assert res.exit_code != 0, (
        f"a zero-byte .sif was accepted as a built image:\n{res.output}")
    assert "0 bytes" in res.output or "no image was produced" in res.output, res.output
    assert "✓ built" not in res.output, (
        f"printed a success tick for an empty image:\n{res.output}")
