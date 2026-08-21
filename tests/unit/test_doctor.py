"""Tests for `botainer doctor` preflight (Phase 1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.cli import doctor


def test_docker_disk_finding_warns_when_reclaimable_high() -> None:
    """`botainer doctor` must translate a full Docker disk into plain guidance so
    a user hits `docker system prune` instead of a raw ENOSPC. >= 3 GB reclaimable
    → warn; the remediation is carried through verbatim."""
    df = (
        "Images\t8.5GB\t4.2GB (49%)\n"
        "Containers\t20MB\t10MB (50%)\n"
        "Local Volumes\t100MB\t0B (0%)\n"
        "Build Cache\t2.1GB\t2.1GB"
    )
    rem = "REMEDIATION-MARKER"
    f = doctor._docker_disk_finding(df, rem)
    assert f.severity == "warn"          # 4.2 + 2.1 = 6.3 GB reclaimable
    assert f.check == "runtime.docker_disk"
    assert "prune" in f.detail
    assert f.remediation == rem


def test_docker_disk_finding_info_when_low() -> None:
    df = "Images\t1.2GB\t200MB (16%)\nBuild Cache\t50MB\t50MB"
    f = doctor._docker_disk_finding(df, "R")
    assert f.severity == "info"          # < 3 GB reclaimable
    assert "Images" in f.detail


def test_docker_disk_finding_tolerates_garbage() -> None:
    """Never crash doctor on unexpected df output."""
    f = doctor._docker_disk_finding("weird\noutput\nno tabs", "R")
    assert f.severity in {"info", "warn"}


def test_collect_findings_returns_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    findings = doctor.collect_findings(for_setup=False)
    assert isinstance(findings, list)
    assert all(isinstance(f, doctor.Finding) for f in findings)
    assert findings  # should have at least the state-dir + plugin checks


def test_collect_for_setup_adds_disk_and_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    setup_findings = doctor.collect_findings(for_setup=True)
    regular = doctor.collect_findings(for_setup=False)
    # setup should add more checks (disk, network)
    setup_checks = {f.check for f in setup_findings}
    regular_checks = {f.check for f in regular}
    assert "disk.free" in setup_checks or "network.docker_hub" in setup_checks
    # regular check shouldn't have those
    assert "disk.free" not in regular_checks


def test_finding_actionable_flag() -> None:
    err = doctor.Finding(severity="err", check="x", detail="y", remediation="fix")
    warn = doctor.Finding(severity="warn", check="x", detail="y")
    info = doctor.Finding(severity="info", check="x", detail="y")
    ok = doctor.Finding(severity="ok", check="x", detail="y")
    assert err.is_actionable()
    assert not warn.is_actionable()
    assert not info.is_actionable()
    assert not ok.is_actionable()


def test_screen_check_ok_when_screen_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§A19 + §A18: doctor records host.screen as `ok` when screen is found,
    so users on a fresh Mac (which ships /usr/bin/screen) get a green tick
    instead of a warning."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    real_which = doctor.shutil.which

    def _which(name):
        if name == "screen":
            return "/usr/bin/screen"
        return real_which(name)

    monkeypatch.setattr(doctor.shutil, "which", _which)
    findings = doctor.collect_findings(for_setup=False)
    screen = next((f for f in findings if f.check == "host.screen"), None)
    assert screen is not None
    assert screen.severity == "ok"
    assert screen.detail == "/usr/bin/screen"


def test_screen_check_warns_when_screen_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§A19 + §A18: doctor warns when screen is missing on PATH, because
    the `nudge` plugin's `botainer start` refusal at launch time is too
    late — doctor should flag this BEFORE the user hits it."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    real_which = doctor.shutil.which

    def _which(name):
        if name == "screen":
            return None
        return real_which(name)

    monkeypatch.setattr(doctor.shutil, "which", _which)
    findings = doctor.collect_findings(for_setup=False)
    screen = next((f for f in findings if f.check == "host.screen"), None)
    assert screen is not None
    assert screen.severity == "warn"
    # Remediation must tell the user how to install it on Mac/Linux/HPC.
    assert "apt install screen" in screen.remediation
    assert "/usr/bin/screen" in screen.remediation
    # And not be actionable — botainer doctor should still exit 0 if
    # everything else is fine; this is a soft warning.
    assert not screen.is_actionable()


def test_remediation_present_for_warnings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    findings = doctor.collect_findings(for_setup=False)
    # At least one warn finding should have a non-empty remediation
    warns = [f for f in findings if f.severity == "warn"]
    if warns:
        # Each warning with a real fix should explain how
        for w in warns:
            # state.exists warn should have a remediation
            if w.check == "state.exists":
                assert w.remediation


def test_no_runtime_warns_but_does_not_abort_setup(monkeypatch, tmp_path) -> None:
    """A missing container runtime must NOT abort `botainer setup`.

    This REVERSES the original audit-T6 rule ("blocking for setup"), and the
    reversal is the point rather than a relaxation:

    * Setup's own work — state dir, user policy, plugin install — needs no
      runtime. `test_no_runtime_on_hpc_login_node_downgrades_to_warn` below
      already asserts exactly that for the sbatch case. Erroring off an HPC
      login node meant the SAME missing runtime got opposite verdicts
      depending on whether `sbatch` happened to be on PATH.
    * Aborting blocked `botainer init` + `botainer inspect` — the
      look-before-you-run flow the README tells people to use to evaluate
      botainer BEFORE installing Docker. It left the user with nothing.

    What must still hold, and is asserted here: the user is told plainly that
    nothing will RUN. Downgrading the severity without keeping that warning
    WOULD be a real weakening.
    """
    import shutil as _sh
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    _rw = _sh.which
    # Neither docker nor apptainer present, and NOT an HPC login node.
    monkeypatch.setattr(
        _sh, "which",
        lambda n: None if n in ("docker", "apptainer", "singularity", "sbatch")
        else _rw(n),
    )
    for for_setup in (True, False):
        f = {x.check: x for x in doctor.collect_findings(for_setup=for_setup)}
        assert f["runtime.any"].severity == "warn", for_setup
        assert not f["runtime.any"].is_actionable(), for_setup
        # ...and it says what will not work.
        text = (f["runtime.any"].detail or "") + (f["runtime.any"].remediation or "")
        assert "start" in text and "refuse" in text, (
            "the warning must still tell the user that `botainer start` will "
            f"refuse without a runtime; got: {text!r}")


def test_no_runtime_on_hpc_login_node_downgrades_to_warn(monkeypatch, tmp_path) -> None:
    """Audit (usability MAJOR / cluster-ease B6): on an HPC LOGIN
    NODE — where apptainer is typically compute-node-only (Yale Grace + many
    others) — setup's runtime-presence check must NOT hard-abort. Setup's real
    work (state dir + policy + plugin install) needs no runtime; the runtime
    requirement surfaces cleanly at `hpc build` / `start` time instead. Signal
    for an HPC login node: sbatch on PATH but no apptainer/singularity.
    """
    import shutil as _sh
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    _rw = _sh.which
    # No container runtime, but sbatch IS present (login node).
    def _which(n):
        if n in ("docker", "apptainer", "singularity"):
            return None
        if n == "sbatch":
            return "/usr/bin/sbatch"
        return _rw(n)
    monkeypatch.setattr(_sh, "which", _which)
    setup_f = {f.check: f for f in doctor.collect_findings(for_setup=True)}
    assert setup_f["runtime.any"].severity == "warn"      # NOT err — setup proceeds
    assert not setup_f["runtime.any"].is_actionable()    # doesn't trigger sys.exit(2)
    assert "HPC login node" in setup_f["runtime.any"].detail or \
           "HPC login" in (setup_f["runtime.any"].remediation or "")


def test_docker_daemon_probe_reports_unreachable(monkeypatch, tmp_path) -> None:
    """Audit T6: docker on PATH but daemon down → a warn finding (not silent)."""
    import shutil as _sh
    import subprocess as _sp
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    _rw = _sh.which
    monkeypatch.setattr(
        _sh, "which",
        lambda n: "/usr/bin/docker" if n == "docker" else (None if n in ("apptainer", "singularity") else _rw(n)),
    )
    # `docker info` fails (daemon down).
    class _P:
        returncode = 1
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P())
    f = {x.check: x for x in doctor.collect_findings(for_setup=False)}
    assert f["runtime.docker_daemon"].severity == "warn"
    assert "daemon" in f["runtime.docker_daemon"].detail.lower()


def test_state_dir_on_scratch_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Internal design note DN-036: a state dir that resolves onto scratch/tmp
    triggers a doctor warning. Losses there are unrecoverable (credentials,
    project UUIDs, .sif images, installed plugins) because scratch + /tmp
    auto-purge. Catches users still living with the older
    `MY_BOTAINER=$SCRATCH/...` advice that `hpc setup` used to print."""
    scratchy = tmp_path / "scratch" / "user" / ".botainer"
    monkeypatch.setenv("MY_BOTAINER", str(scratchy))
    f = {x.check: x for x in doctor.collect_findings(for_setup=False)}
    assert "state.dir_on_purged_storage" in f, list(f)
    finding = f["state.dir_on_purged_storage"]
    assert finding.severity == "warn"
    assert "scratch" in finding.detail.lower()
    # Remediation mentions the corrective action (unset MY_BOTAINER).
    assert "MY_BOTAINER" in (finding.remediation or "")


def test_state_dir_on_home_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A normal $HOME-rooted state dir doesn't trigger the scratch warning."""
    normal = tmp_path / "userhome" / ".botainer"
    monkeypatch.setenv("MY_BOTAINER", str(normal))
    f = {x.check: x for x in doctor.collect_findings(for_setup=False)}
    assert "state.dir_on_purged_storage" not in f


def test_doctor_reports_selinux_enforcing(monkeypatch, tmp_path) -> None:
    """recon T-B: doctor surfaces SELinux Enforcing so the user understands
    the label=disable behavior + the setenforce fallback."""
    import shutil as _sh
    from pathlib import Path as _P
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    orig_which = _sh.which
    monkeypatch.setattr(_sh, "which", lambda n: "/usr/bin/docker" if n == "docker" else (None if n in ("apptainer", "singularity", "sbatch") else orig_which(n)))
    import subprocess as _sp
    class _P0:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P0())
    real_read = _P.read_text
    monkeypatch.setattr(_P, "read_text", lambda self, *a, **k: "1\n" if str(self) == "/sys/fs/selinux/enforce" else real_read(self, *a, **k))
    real_exists = _P.exists
    monkeypatch.setattr(_P, "exists", lambda self: True if str(self) == "/sys/fs/selinux/enforce" else real_exists(self))
    f = {x.check: x for x in doctor.collect_findings(for_setup=False)}
    assert "host.selinux" in f
    assert "Enforcing" in f["host.selinux"].detail


def test_doctor_reports_rootless_docker(monkeypatch, tmp_path) -> None:
    """recon T-B: doctor warns on rootless Docker (bind-ownership caveat)."""
    import shutil as _sh
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    orig_which = _sh.which
    monkeypatch.setattr(_sh, "which", lambda n: "/usr/bin/docker" if n == "docker" else (None if n in ("apptainer", "singularity", "sbatch") else orig_which(n)))
    import subprocess as _sp
    class _P0:
        returncode = 0
        stdout = "Server:\n Security Options:\n  rootless\n"
        stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P0())
    f = {x.check: x for x in doctor.collect_findings(for_setup=False)}
    assert "runtime.docker_rootless" in f
    assert f["runtime.docker_rootless"].severity == "warn"
