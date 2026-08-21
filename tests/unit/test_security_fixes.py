"""Tests for the security fixes from the multi-agent review.

These are intentionally separate from feature tests so the security
contracts are easy to audit and grep for. Each test references the
finding it locks down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.cli import nudge as nudge_cli
from botainer.core.refusal import Refused
from botainer.core.spec import PortForward
from botainer.state import session_record as sr

# ────────── CRITICAL 1: host_bind allowlist (sharp-edges + insecure-defaults) ──────────


def test_portforward_host_bind_accepts_loopback() -> None:
    """The three allowed loopback forms."""
    for bind in ("127.0.0.1", "::1", "localhost"):
        pf = PortForward(container_port=8888, host_port=8888, host_bind=bind)
        assert pf.host_bind == bind


def test_portforward_host_bind_refuses_zero_zero_zero_zero() -> None:
    with pytest.raises(Exception, match="loopback|not allowed"):
        PortForward(container_port=8888, host_port=8888, host_bind="0.0.0.0")


def test_portforward_host_bind_refuses_ipv6_wildcard() -> None:
    """`::` is IPv6 'all interfaces'; the old denylist missed it."""
    with pytest.raises(Exception, match="loopback|not allowed"):
        PortForward(container_port=8888, host_port=8888, host_bind="::")


def test_portforward_host_bind_refuses_empty_string() -> None:
    """Docker treats `-p :8080:80` as 0.0.0.0:8080:80."""
    with pytest.raises(Exception, match="loopback|not allowed"):
        PortForward(container_port=8888, host_port=8888, host_bind="")


def test_portforward_host_bind_refuses_zero_string() -> None:
    """Docker resolves "0" to 0.0.0.0."""
    with pytest.raises(Exception, match="loopback|not allowed"):
        PortForward(container_port=8888, host_port=8888, host_bind="0")


def test_portforward_host_bind_refuses_arbitrary_hostname() -> None:
    """Hostnames can resolve to routable NICs."""
    with pytest.raises(Exception, match="loopback|not allowed"):
        PortForward(
            container_port=8888, host_port=8888,
            host_bind="host.docker.internal",
        )


def test_portforward_host_bind_refuses_wildcard() -> None:
    with pytest.raises(Exception, match="loopback|not allowed"):
        PortForward(container_port=8888, host_port=8888, host_bind="*")


def test_resolve_port_forwards_allowlist_at_composition() -> None:
    """The composition layer enforces the same allowlist as the Pydantic
    validator (defense in depth)."""
    from botainer.core import composition
    from botainer.core import config as config_module

    cfg = config_module.ProjectConfig(
        agent="claude",
        plugins_enabled=["web-ports"],
        plugins={
            "web-ports": {
                "ports": [{"container": 8888, "host": 8888, "host_bind": "::"}]
            }
        },
    )
    with pytest.raises(Refused, match="loopback|not allowed"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


# ────────── HIGH 3: screen -X stuff -- separator ──────────
# Post-§A19: screen-outside-container, not in-container tmux. The `--`
# separator is between `screen -S <sid> -X stuff` and the user payload.


def _docker_rec():
    return sr.SessionRecord(
        session_id="ses12345abc", project_uuid="u", project_root="/p",
        runtime="docker", image="img", host="h", spec={},
        docker=sr.DockerHandle(container_id="cidabc123def4"),
        screen_session_id="botainer-ses12345abc",
    )


def test_send_stuff_argv_has_no_dash_dash_separator() -> None:
    """2026-07-09: the `--` was REMOVED — GNU screen's `stuff` command doesn't
    accept it and errored ('-X: stuff: ...'). The payload is the stuff operand,
    positioned right after `-X stuff`."""
    argv = nudge_cli._build_delivery_argv(_docker_rec(), ["hello\r"])
    assert "--" not in argv
    assert argv[:5] == ["screen", "-S", "botainer-ses12345abc", "-X", "stuff"]
    assert argv[-1] == "hello\r"


def test_leading_dash_payload_is_the_stuff_operand_not_a_flag() -> None:
    """A payload starting with `-` is safe WITHOUT `--`: it sits after `-X stuff`,
    already past screen's option parsing, so screen treats it as the stuff text,
    never a second flag."""
    argv = nudge_cli._build_delivery_argv(_docker_rec(), ["-X", "copy-pipe"])
    stuff_idx = argv.index("stuff")
    assert argv[stuff_idx + 1] == "-X"       # the payload, as the operand
    assert argv[stuff_idx + 2] == "copy-pipe"
    assert "--" not in argv


# ────────── HIGH 2: nudges-sent.jsonl mode 0600 ──────────


def test_audit_log_created_with_mode_0600(tmp_path: Path) -> None:
    """Defense: log file is mode 0600 even when umask is loose."""
    import os
    # Set a permissive umask so default open() would create 0666 & ~022 = 0644.
    old_umask = os.umask(0o022)
    try:
        nudge_cli._append_audit(tmp_path, "test message", None)
    finally:
        os.umask(old_umask)
    log_path = tmp_path / "nudges-sent.jsonl"
    assert log_path.exists()
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


# ────────── MEDIUM 7: session_id + container_id shape validation ──────────


def test_session_id_validator_refuses_path_traversal() -> None:
    assert not sr.is_valid_session_id("../etc/passwd")
    assert not sr.is_valid_session_id("..")


def test_session_id_validator_refuses_leading_dash() -> None:
    """Leading `-` would be argv-flag-interpreted."""
    assert not sr.is_valid_session_id("-rm-rf-x")


def test_session_id_validator_refuses_path_separator() -> None:
    assert not sr.is_valid_session_id("a/b/c12345")


def test_session_id_validator_refuses_shell_metachars() -> None:
    assert not sr.is_valid_session_id("a;b;c;1234")
    assert not sr.is_valid_session_id("a$b$1234567")
    assert not sr.is_valid_session_id("a`b`1234567")
    assert not sr.is_valid_session_id("a b 12345678")


def test_session_id_validator_refuses_too_short() -> None:
    assert not sr.is_valid_session_id("abc")  # 3 chars
    assert not sr.is_valid_session_id("abcdefg")  # 7 chars


def test_session_id_validator_refuses_empty_and_null() -> None:
    assert not sr.is_valid_session_id("")
    assert not sr.is_valid_session_id("\x00abc12345")


def test_session_id_validator_accepts_uuid_hex() -> None:
    """The form _new_session_id() actually produces."""
    assert sr.is_valid_session_id("abcdef0123456789")
    assert sr.is_valid_session_id("1234567890abcdef")


def test_container_id_validator_refuses_leading_dash() -> None:
    assert not sr.is_valid_container_id("-rm-rf-x-args")


def test_container_id_validator_refuses_path_separator() -> None:
    assert not sr.is_valid_container_id("abc/def/12345")


def test_container_id_validator_refuses_too_short() -> None:
    assert not sr.is_valid_container_id("abc123")  # 6 chars


def test_container_id_validator_accepts_docker_id() -> None:
    """64-char hex (full Docker container ID)."""
    assert sr.is_valid_container_id("a" * 64)
    # 12-char short form.
    assert sr.is_valid_container_id("abc123def456")


def test_from_dict_refuses_bad_session_id() -> None:
    """A tampered spec.json with a leading-dash session_id should refuse."""
    payload = {
        "schema_version": 1,
        "session_id": "-rm-rf",
        "project_uuid": "u",
        "project_root": "/p",
        "runtime": "docker",
        "image": "img",
        "host": "h",
    }
    with pytest.raises(ValueError, match="session_id"):
        sr.SessionRecord.from_dict(payload)


def test_from_dict_refuses_bad_container_id() -> None:
    payload = {
        "schema_version": 1,
        "session_id": "abcdef01234567",
        "project_uuid": "u",
        "project_root": "/p",
        "runtime": "docker",
        "image": "img",
        "host": "h",
        "runtime_handle": {
            "docker": {"container_id": "/etc/passwd"},
            "apptainer": None,
        },
    }
    with pytest.raises(ValueError, match="container_id"):
        sr.SessionRecord.from_dict(payload)


# ────────── MEDIUM 5: _quote_for_display vs shlex.quote ──────────


def test_scheduled_nudge_uses_shlex_quote_not_display_helper(tmp_path, monkeypatch) -> None:
    """The delayed-nudge scheduler builds a SHELL command string, so a hostile
    token in argv must not be able to break out of it.

    TEST-QUALITY AUDIT (B7): this used to assert
    `"shlex.quote" in inspect.getsource(_schedule_native)`. That passed on the
    DOCSTRING — nudge.py:617 reads "every token is shlex.quote'd" and
    getsource() includes docstrings — so changing the real call at :628 to
    `" ".join(argv)`, a shell-injection regression on a security-surface file,
    left this test GREEN. Mutation-verified by the audit. Assert the BEHAVIOUR:
    run the function with a hostile token and inspect the script it hands to sh.
    (The sibling test below already made exactly this move for attach.py.)
    """
    import shlex

    captured: dict[str, list[str]] = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(nudge_cli.subprocess, "Popen", _FakePopen)

    hostile = "; touch /tmp/pwned #"
    nudge_cli._schedule_native(
        ["botainer", "nudge", "--session", "abc", hostile], 5, cwd=tmp_path)

    assert captured["argv"][:2] == ["sh", "-c"]
    script = captured["argv"][2]
    # The hostile token must appear ONLY in quoted form...
    assert shlex.quote(hostile) in script
    # ...and must not be able to terminate the command it sits in. Re-parse the
    # delivered command and require the token to survive as ONE argument.
    delivered = script.split("[nudge] delivering $(date)\"; ", 1)[1]
    delivered = delivered.split("; echo \"[nudge] done", 1)[0]
    assert shlex.split(delivered)[-1] == hostile


# ────────── attach --: argv hardening ──────────


def test_attach_docker_argv_has_dash_dash_before_container_id() -> None:
    """Task #276: assert argv LIST shape, not source-text grep.

    Old: read attach.py text, look for a literal '"docker", "attach",
    "--", target.docker.container_id'. Cosmetic refactor breaks the
    test even though behavior is identical. Worse: if the `--` is
    removed but the literal happens to appear in a comment, test
    passes while injection is possible.

    New: import the helper, call it with a hostile container_id, assert
    the returned LIST has 'docker', 'attach', '--', then the id.
    """
    from botainer.cli.attach import _build_docker_attach_argv
    hostile_id = "--rm"  # would be parsed as a flag without --
    argv = _build_docker_attach_argv(hostile_id)
    assert argv == ["docker", "attach", "--", "--rm"]
    # The '--' must be at index 2, BEFORE the container_id at index 3.
    assert argv.index("--") < argv.index(hostile_id)
