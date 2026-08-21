"""Unit tests for botainer/cli/nudge.py — argument parsing + helpers.

Excludes anything that talks to a real container/socket; that's covered
by the integration tests with real tmux.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from botainer.cli import nudge as nudge_cli
from botainer.state import session_record as sr

# ────────── argument validation ──────────


def test_no_text_no_keys_refuses_with_remediation() -> None:
    runner = CliRunner()
    result = runner.invoke(nudge_cli.nudge, [])
    assert result.exit_code == 2
    assert "no text or --keys to send" in result.output
    assert "hint:" in result.output


def test_text_and_keys_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(nudge_cli.nudge, ["hello", "--keys", "C-c"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_empty_text_refuses() -> None:
    runner = CliRunner()
    # An empty string still passes click's required check; we refuse explicitly.
    result = runner.invoke(nudge_cli.nudge, [""])
    assert result.exit_code == 2
    assert "empty text" in result.output


def test_multiword_text_joined_without_quotes(tmp_path: Path, monkeypatch) -> None:
    """F6: `nudge are you connected` (unquoted, multi-word) must NOT error with
    'Got unexpected extra arguments' — it joins to 'are you connected' and gets
    past arg parsing (here: refuses because we're outside a project)."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(nudge_cli.nudge, ["are", "you", "connected"])
    assert "unexpected extra arguments" not in result.output.lower(), result.output
    assert "not inside a botainer project" in result.output


def test_outside_project_refuses(tmp_path: Path, monkeypatch) -> None:
    """When run outside a botainer project, refuses with a clear message."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(nudge_cli.nudge, ["hello"])
    assert result.exit_code == 2
    assert "not inside a botainer project" in result.output


# ────────── --in delay parsing ──────────


def test_parse_delay_seconds_returns_seconds() -> None:
    # #68 nudge: native --in parses to SECONDS (no at(1) dependency).
    assert nudge_cli._parse_delay_seconds("30s") == 30
    assert nudge_cli._parse_delay_seconds("5m") == 300
    assert nudge_cli._parse_delay_seconds("2h") == 7200
    assert nudge_cli._parse_delay_seconds("1d") == 86400


def test_parse_delay_unparseable() -> None:
    assert nudge_cli._parse_delay_seconds("forever") is None
    assert nudge_cli._parse_delay_seconds("") is None
    assert nudge_cli._parse_delay_seconds("3y") is None
    assert nudge_cli._parse_delay_seconds("-5m") is None


# ────────── _hosts_likely_compatible ──────────


def test_hosts_compatible_same() -> None:
    assert nudge_cli._hosts_likely_compatible("host1", "host1") is True


def test_hosts_compatible_long_common_prefix() -> None:
    # HPC: login + compute share cluster prefix
    assert nudge_cli._hosts_likely_compatible("grace1", "grace2") is True


def test_hosts_incompatible_short_common_prefix() -> None:
    assert nudge_cli._hosts_likely_compatible("ab", "cd") is False
    assert nudge_cli._hosts_likely_compatible("foo", "bar") is False


# ────────── self-invocation argv ──────────


def test_self_invocation_text() -> None:
    """2026-07-10: re-invoke via the RUNNING interpreter + module (`sys.executable
    -m botainer.cli.main`), NOT a bare "botainer" — a detached `sh -c` can't see
    the user's aliases/wrappers, and this pins the scheduled send to the SAME
    botainer version that scheduled it."""
    import sys
    argv = nudge_cli._self_invocation_argv("continue", None, "abc123", False)
    assert argv == [
        sys.executable, "-m", "botainer.cli.main",
        "nudge", "--session", "abc123", "--quiet", "continue",
    ]
    assert argv[0] != "botainer"          # never a bare alias-dependent name


def test_self_invocation_keys() -> None:
    import sys
    argv = nudge_cli._self_invocation_argv(None, "C-c", "abc123", False)
    assert argv == [
        sys.executable, "-m", "botainer.cli.main",
        "nudge", "--session", "abc123", "--quiet", "--keys", "C-c",
    ]


def test_self_invocation_no_enter_flag() -> None:
    argv = nudge_cli._self_invocation_argv("hi", None, "sid", True)
    assert "--no-enter" in argv


# ────────── audit log ──────────


def test_audit_log_appends_jsonl(tmp_path: Path) -> None:
    nudge_cli._append_audit(tmp_path, "first nudge", None)
    nudge_cli._append_audit(tmp_path, None, "C-c")
    log = tmp_path / "nudges-sent.jsonl"
    assert log.exists()
    lines = log.read_text().strip().split("\n")
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    # Task #301: nudges-sent.jsonl no longer stores nudge text verbatim
    # (users paste API keys into prompts). Now records sha8 + length.
    import hashlib as _h
    assert a["text_sha8"] == _h.sha256("first nudge".encode()).hexdigest()[:8]
    assert a["text_len"] == len("first nudge")
    assert a["keys"] is None
    assert b.get("text_sha8") is None
    assert b["keys"] == "C-c"
    assert "ts" in a and "host" in a


def test_audit_log_failure_silent(tmp_path: Path) -> None:
    """If the log file can't be written (permission denied), don't propagate."""
    # Create a file at the path that has no write perms.
    log = tmp_path / "nudges-sent.jsonl"
    log.write_text("")
    log.chmod(0o400)
    try:
        # Should not raise.
        nudge_cli._append_audit(tmp_path, "hi", None)
    finally:
        log.chmod(0o600)


# ────────── _is_session_alive ──────────


def test_is_session_alive_mock_runtime_always_false() -> None:
    rec = sr.SessionRecord(
        session_id="s",
        project_uuid="u",
        project_root="/p",
        runtime="mock",
        image="img",
        host="h",
        spec={},
    )
    assert nudge_cli._is_session_alive(rec) is False


def test_is_session_alive_docker_no_container_id() -> None:
    rec = sr.SessionRecord(
        session_id="s",
        project_uuid="u",
        project_root="/p",
        runtime="docker",
        image="img",
        host="h",
        spec={},
    )
    assert nudge_cli._is_session_alive(rec) is False


def test_is_session_alive_apptainer_no_jobid_assumes_no() -> None:
    """Task #92: foreground apptainer (no Slurm) with no jobid recorded is
    treated as DEAD, not alive. The old 'assume yes' behavior left every
    exited foreground session showing 'running forever'. New behavior:
    no jobid → dead."""
    rec = sr.SessionRecord(
        session_id="s",
        project_uuid="u",
        project_root="/p",
        runtime="apptainer",
        image="img",
        host="h",
        spec={},
        apptainer=sr.ApptainerHandle(),
    )
    assert nudge_cli._is_session_alive(rec) is False


# ────────── _resolve_target_session ──────────


def test_resolve_target_session_no_records_returns_none(tmp_path: Path) -> None:
    assert nudge_cli._resolve_target_session(tmp_path, None) is None


def test_resolve_target_session_id_no_match(tmp_path: Path) -> None:
    sr.write(
        tmp_path / "real",
        sr.SessionRecord(
            session_id="abcdef0123456789",
            project_uuid="u",
            project_root="/p",
            runtime="docker",
            image="img",
            host="h",
            spec={},
        ),
    )
    assert nudge_cli._resolve_target_session(tmp_path, "xyz") is None


def test_resolve_target_session_id_prefix_match(tmp_path: Path) -> None:
    sr.write(
        tmp_path / "s1",
        sr.SessionRecord(
            session_id="abcdef0123456789",
            project_uuid="u",
            project_root="/p",
            runtime="docker",
            image="img",
            host="h",
            spec={},
        ),
    )
    out = nudge_cli._resolve_target_session(tmp_path, "abcdef")
    assert out is not None
    assert out.session_id == "abcdef0123456789"


# ────────── dry-run mode ──────────


def test_dry_run_does_not_invoke_subprocess(tmp_path: Path, monkeypatch) -> None:
    """Setting up minimal project state so dry-run reaches the printout."""
    # Build a fake project with state dir.
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".botainer").mkdir()
    (project_root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    state_root = tmp_path / "botainer-state"
    monkeypatch.setenv("MY_BOTAINER", str(state_root))
    monkeypatch.chdir(project_root)

    # Build a fake session record under the project's state dir.
    from botainer.core.identity import resolve_identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    rec = sr.SessionRecord(
        session_id="abcdef0123456789",
        project_uuid=uid,
        project_root=str(project_root),
        runtime="docker",
        image="ubuntu:24.04",
        host="dev-container",
        spec={},
        docker=sr.DockerHandle(container_id="cid12345fakefake"),
        screen_session_id="botainer-abcdef0123456789",
        started_at="2026-05-16T22:00:00Z",
    )
    session_dir = proj_paths.sessions_dir / rec.session_id
    sr.write(session_dir, rec)

    # Stub out _is_session_alive to return True so dry-run reaches its print.
    monkeypatch.setattr(nudge_cli, "_is_session_alive", lambda r: True)
    # Also stub host check to pass.
    monkeypatch.setattr(
        nudge_cli, "_hosts_likely_compatible", lambda a, b: True
    )

    runner = CliRunner()
    result = runner.invoke(
        nudge_cli.nudge, ["--dry-run", "continue"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    # §A19: screen runs on the HOST; the dry-run argv is the screen
    # invocation, not `docker exec`.
    assert "screen" in result.output
    assert "stuff" in result.output
    assert "continue" in result.output


# ────────── the live-Grace/local nudge failures ──────────

def _running_session(tmp_path, monkeypatch):
    """Set up a project + a docker session record + alive/host stubs, and chdir
    into it. Returns the project_root."""
    from botainer.core.identity import resolve_identity
    from botainer.state import dir as state_dir
    project_root = tmp_path / "proj"
    (project_root / ".botainer").mkdir(parents=True)
    (project_root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.chdir(project_root)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    rec = sr.SessionRecord(
        session_id="abcdef0123456789", project_uuid=uid,
        project_root=str(project_root), runtime="docker", image="ubuntu:24.04",
        host="dev-container", spec={},
        docker=sr.DockerHandle(container_id="cid12345fakefake"),
        screen_session_id="botainer-abcdef0123456789",
        started_at="2026-05-16T22:00:00Z")
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)
    monkeypatch.setattr(nudge_cli, "_is_session_alive", lambda r: True)
    monkeypatch.setattr(nudge_cli, "_hosts_likely_compatible", lambda a, b: True)
    return project_root


def test_in_bad_value_is_clean_refusal_not_crash(tmp_path, monkeypatch) -> None:
    _running_session(tmp_path, monkeypatch)
    r = CliRunner().invoke(nudge_cli.nudge, ["--in", "banana", "continue"],
                           catch_exceptions=True)
    assert r.exit_code == 2
    assert r.exception is None or isinstance(r.exception, SystemExit)  # no traceback
    assert "could not be parsed" in r.output


def test_in_schedules_natively_without_at(tmp_path, monkeypatch) -> None:
    # THE point of nudge: send at a specified time — must work with no at(1)/cron.
    _running_session(tmp_path, monkeypatch)
    calls = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            calls["argv"] = argv
            calls["kw"] = kw

    monkeypatch.setattr(nudge_cli.subprocess, "Popen", _FakePopen)
    r = CliRunner().invoke(nudge_cli.nudge, ["--in", "5m", "wrap up"],
                           catch_exceptions=False)
    assert r.exit_code == 0
    assert "scheduled nudge in 5m" in r.output
    # detached background sleep+redeliver; NOT at(1)
    assert calls["argv"][0] == "sh" and "sleep 300;" in calls["argv"][2]
    # re-invokes via the interpreter + module (alias/version-robust), NOT a bare
    # "botainer" name (fix).
    assert "botainer.cli.main" in calls["argv"][2] and "nudge" in calls["argv"][2]
    assert "wrap up" in calls["argv"][2]
    assert calls["kw"].get("start_new_session") is True


def test_stuff_payload_uses_cr_not_the_word_Enter() -> None:
    #: text nudges must submit via a real carriage return, NOT the tmux
    # key name "Enter" (screen has no named keys — it stuffed the letters "Enter").
    chunks = nudge_cli._prepare_chunks("continue", None, no_enter=False)
    assert chunks == [["continue\r"]]
    # --no-enter → no trailing CR
    assert nudge_cli._prepare_chunks("continue", None, no_enter=True) == [["continue"]]


def test_keys_translate_to_screen_byte_sequences() -> None:
    # --keys must send the raw bytes screen understands, not the tmux name.
    assert nudge_cli._prepare_chunks(None, "C-c", False) == [["\x03"]]   # Ctrl-C
    assert nudge_cli._prepare_chunks(None, "Escape", False) == [["\x1b"]]
    assert nudge_cli._prepare_chunks(None, "Up", False) == [["\x1b[A"]]


def test_delivery_argv_has_no_dash_dash_separator() -> None:
    # GNU screen's `stuff` does NOT accept `--` (it errored "-X: stuff: ...").
    from botainer.state import session_record as sr
    rec = sr.SessionRecord(
        session_id="abcdef0123456789", project_uuid="u",
        project_root="/p", runtime="docker", image="x", host="h", spec={},
        docker=sr.DockerHandle(container_id="c"),
        screen_session_id="botainer-abcdef0123456789",
        started_at="2026-01-01T00:00:00Z")
    argv = nudge_cli._build_delivery_argv(rec, ["continue\r"])
    assert "--" not in argv
    assert argv[:5] == ["screen", "-S", "botainer-abcdef0123456789", "-X", "stuff"]
    assert argv[-1] == "continue\r"


def test_delivery_no_screen_session_gives_enablement_message(tmp_path, monkeypatch) -> None:
    # The exact local failure: nudge not enabled → screen says "No screen session
    # found" → we must show the enablement fix, not the raw "-X: stuff" error.
    _running_session(tmp_path, monkeypatch)

    class _R:
        returncode = 1
        stderr = "No screen session found matching botainer-abcdef0123456789."
        stdout = ""

    monkeypatch.setattr(nudge_cli.subprocess, "run", lambda *a, **k: _R())
    r = CliRunner().invoke(nudge_cli.nudge, ["continue"], catch_exceptions=True)
    assert r.exit_code == 1
    assert r.exception is None or isinstance(r.exception, SystemExit)
    assert "not enabled" in r.output and "plugins_enabled" in r.output
    assert "No screen session" not in r.output  # raw error suppressed


def test_delivery_screen_not_installed_is_clean_message(tmp_path, monkeypatch) -> None:
    _running_session(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise FileNotFoundError("screen")

    monkeypatch.setattr(nudge_cli.subprocess, "run", _boom)
    r = CliRunner().invoke(nudge_cli.nudge, ["continue"], catch_exceptions=True)
    assert r.exit_code == 1
    assert r.exception is None or isinstance(r.exception, SystemExit)  # no traceback
    assert "screen" in r.output and "not installed" in r.output
