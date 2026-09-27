"""Execute the in-container probe (PROBE_SH) via LOCAL /bin/sh against tmp
fixtures. The dev container is Linux with /bin/sh + /proc, so the probe's own
logic is exercised for real here (write-refused-vs-writable, CapEff parse,
env-leak, ssh-dir, framing, always-exit-0) — the only thing that can't be
tested here is the real container's mount/cap state, which is skipif-gated
elsewhere. A probe that asserts "write refused" passes whether the errno is
EACCES (chmod 555 here) or EROFS (real RO bind), so local fidelity is high.

Design + shell authored via verified Fable-5 subagents (wf_fecc8dc3-aa7).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from botainer.preflight.parse import parse_selftest_output
from botainer.preflight.probe_script import PROBE_SH

US = "\x1f"


def _run(args: list[str], env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    env.pop("LD_PRELOAD", None)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        ["sh", "-c", PROBE_SH, "botainer-probe", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 0, f"probe must always exit 0; got {r.returncode}\n{r.stderr}"
    checks, mounts = parse_selftest_output(r.stdout)
    return {"by": {c.check.value: c for c in checks}, "mounts": mounts, "raw": r.stdout}


def _status(tmp: Path, capeff: str = "0000000000000000", nnp: str | None = "1") -> str:
    p = tmp / "status"
    body = f"CapEff:\t{capeff}\n"
    if nnp is not None:
        body += f"NoNewPrivs:\t{nnp}\n"
    p.write_text(body)
    return str(p)


def test_write_rw_pass_and_ro_fail(tmp_path: Path) -> None:
    rw = tmp_path / "rw"; rw.mkdir()
    ro = tmp_path / "ro"; ro.mkdir(); os.chmod(ro, 0o555)
    out = _run([
        f"workspace-rw{US}write_rw{US}{rw}",
        f"data-ro{US}write_ro{US}{ro}",
    ], {"BOTAINER_PROBE_STATUS": _status(tmp_path)})
    assert out["by"]["workspace-rw"].result == "pass"
    assert out["by"]["data-ro"].result == "pass"         # RO dir refuses write
    # A dir labeled RO but actually writable → FAIL.
    out2 = _run([f"data-ro{US}write_ro{US}{rw}"])
    assert out2["by"]["data-ro"].result == "fail"
    # No sentinel/probe leaves a stray sentinel file behind.
    assert not list(rw.glob(".botainer-selftest*"))


def test_write_ro_missing_target_fails(tmp_path: Path) -> None:
    out = _run([f"secret-ro{US}write_ro{US}{tmp_path}/nope"])
    assert out["by"]["secret-ro"].result == "fail"
    assert "missing" in out["by"]["secret-ro"].detail


def test_capeff_pass_fail_skip(tmp_path: Path) -> None:
    ok = _run([f"caps-dropped{US}capeff{US}"], {"BOTAINER_PROBE_STATUS": _status(tmp_path, capeff="0000000000000000")})
    assert ok["by"]["caps-dropped"].result == "pass"
    bad = _run([f"caps-dropped{US}capeff{US}"], {"BOTAINER_PROBE_STATUS": _status(tmp_path, capeff="00000000a80425fb")})
    assert bad["by"]["caps-dropped"].result == "fail"
    (tmp_path / "empty").write_text("NoNewPrivs:\t1\n")
    skip = _run([f"caps-dropped{US}capeff{US}"], {"BOTAINER_PROBE_STATUS": str(tmp_path / "empty")})
    assert skip["by"]["caps-dropped"].result == "skip"


def test_nonewprivs_pass_fail_skip(tmp_path: Path) -> None:
    assert _run([f"no-new-privs{US}nonewprivs{US}"], {"BOTAINER_PROBE_STATUS": _status(tmp_path, nnp="1")})["by"]["no-new-privs"].result == "pass"
    assert _run([f"no-new-privs{US}nonewprivs{US}"], {"BOTAINER_PROBE_STATUS": _status(tmp_path, nnp="0")})["by"]["no-new-privs"].result == "fail"
    (tmp_path / "noc").write_text("CapEff:\t0\n")
    assert _run([f"no-new-privs{US}nonewprivs{US}"], {"BOTAINER_PROBE_STATUS": str(tmp_path / "noc")})["by"]["no-new-privs"].result == "skip"


def test_sshdir_detects_key_file(tmp_path: Path) -> None:
    home = tmp_path / "home"; (home / ".ssh").mkdir(parents=True)
    clean = _run([f"negative-ssh-home{US}sshdir{US}"], {"HOME": str(home)})
    assert clean["by"]["negative-ssh-home"].result == "pass"
    (home / ".ssh" / "id_rsa").write_text("KEY")
    leaked = _run([f"negative-ssh-home{US}sshdir{US}"], {"HOME": str(home)})
    assert leaked["by"]["negative-ssh-home"].result == "fail"
    # Never leak the filename/contents — only a count-ish reason.
    assert "id_rsa" not in leaked["by"]["negative-ssh-home"].detail
    assert "KEY" not in leaked["raw"]


def test_env_unset_flags_injection_vars(tmp_path: Path) -> None:
    ok = _run([f"negative-env-injection{US}env_unset{US}"])
    assert ok["by"]["negative-env-injection"].result == "pass"
    bad = _run([f"negative-env-injection{US}env_unset{US}"], {"BASH_ENV": "/tmp/x", "LD_PRELOAD": "/tmp/e.so"})
    assert bad["by"]["negative-env-injection"].result == "fail"
    assert "BASH_ENV" in bad["by"]["negative-env-injection"].detail


def test_detail_is_sanitized(tmp_path: Path) -> None:
    """A target path with a double-quote must not break the JSON framing."""
    d = tmp_path / 'we"ird'; d.mkdir()
    out = _run([f'data-ro{US}write_ro{US}{d}'])   # writable → fail, detail references path
    # parse succeeded (no JSON break) and the quote was stripped from detail.
    assert '"' not in out["by"]["data-ro"].detail


def test_always_frames_output_and_mounts(tmp_path: Path) -> None:
    # Exercise the emitted mount frame without depending on the host having /proc.
    mount_text = "tmpfs / tmpfs rw 0 0\n/dev/mock /data ext4 ro 0 0"
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mount_text + "\n")
    out = _run([f"caps-dropped{US}capeff{US}"], {
        "BOTAINER_PROBE_STATUS": _status(tmp_path),
        "BOTAINER_PROBE_MOUNTS": str(mounts_file),
    })
    assert out["by"]["caps-dropped"].result == "pass"
    assert out["mounts"] == mount_text
