"""Tests for the proxy hardening (sharp-edges HIGH 4 + HIGH 5)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROXY_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "agent-claude-proxy" / "proxy.py"
)
START_HOOK = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "agent-claude-proxy" / "hooks" / "start_proxy.py"
)


def _spawn_proxy(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Spawn the proxy directly and capture its early-exit error."""
    base_env = {
        "BOTAINER_PROXY_SOCKET_PATH": "/tmp/test-proxy.sock",
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "eph",
        "BOTAINER_PROXY_REAL_CREDS_PATH": "/tmp/test-creds.json",
        "BOTAINER_PROXY_AUDIT_LOG": "/tmp/test-audit.jsonl",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    base_env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(PROXY_SCRIPT)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=5,
    )


# ────────── HIGH 5: upstream hostname allowlist ──────────


def test_proxy_refuses_arbitrary_upstream_host() -> None:
    """An attacker-controlled upstream URL is refused at proxy startup."""
    result = _spawn_proxy({
        "BOTAINER_PROXY_UPSTREAM": "https://attacker.example.com",
    })
    assert result.returncode != 0
    assert "allowlist" in result.stderr.lower()
    assert "attacker.example.com" in result.stderr


def _refusal(upstream: str) -> subprocess.CompletedProcess[str]:
    """Spawn the proxy and insist it REFUSED, rather than merely exited.

    `_spawn_proxy` has a 5s timeout. If the proxy ACCEPTS an upstream it binds
    its socket and serves forever, so the timeout expires and the caller gets a
    `TimeoutExpired` traceback that says nothing about what went wrong. Turn
    that into the sentence a reader needs: the proxy started, and it started
    pointed at this host.
    """
    try:
        result = _spawn_proxy({"BOTAINER_PROXY_UPSTREAM": upstream})
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - regression
        # DO NOT read every timeout as acceptance. Refusal takes ~60-90 ms
        # against a 5 s budget, so a timeout is almost always a real accept —
        # but "the proxy forwards your credential to attackers" is far too
        # alarming a sentence to print at someone because a loaded machine
        # descheduled a subprocess. The proxy announces itself when it binds;
        # require that announcement before making the claim.
        err = exc.stderr or b""
        if isinstance(err, str):
            err = err.encode()
        if b"listening on" in err:
            raise AssertionError(
                f"the proxy ACCEPTED upstream {upstream!r} — it bound its "
                f"socket and began serving instead of refusing. A running "
                f"proxy forwards the real credential to whatever upstream it "
                f"was given."
            ) from None
        raise AssertionError(
            f"the proxy neither refused nor announced itself within the "
            f"timeout for upstream {upstream!r}. This is NOT evidence that it "
            f"accepted — it never got far enough to say. stderr: {err!r}"
        ) from None
    assert result.returncode != 0, (
        f"the proxy exited 0 for upstream {upstream!r}")
    return result


# WHY THESE TWO ASSERT THE MESSAGE AND USE `https://`, measured not assumed.
#
# They used to be `http://…` plus a bare `assert result.returncode != 0`, and
# neither half tested what the names claim. Deleting the allowlist membership
# check entirely (`if host not in self._ALLOWED_UPSTREAM_HOSTS:` → `if False:`)
# and re-running the ORIGINAL fixtures:
#
#   http://169.254.169.254/…   still exits 1 — the *scheme* check takes over,
#                              "http:// is only allowed for loopback testing"
#   http://internal.corp/admin same
#   https://169.254.169.254/…  "[proxy] listening on … upstream https://169.…"
#   https://internal.corp/admin same
#
# Note the ordering, because I asserted it backwards first and this file's own
# test caught me. There are TWO scheme-related checks and the allowlist sits
# between them: a scheme WHITELIST first (`file:///etc/passwd` → "must be http
# or https", never reaching the allowlist), then the allowlist, then the
# http-only-for-loopback guard. So for `http://` the allowlist does answer
# first — but "the allowlist is checked before the scheme" flatly is wrong, and
# a reviewer caught me writing it that way. The masking is only visible under
# mutation — remove the allowlist and the scheme guard silently takes the http
# cases, so an `http://` fixture passes either way and can never show that the
# allowlist is load-bearing. The `https://` form has no second guard behind it:
# if the allowlist goes, the proxy starts up pointed at the metadata service.
#
# A non-zero exit cannot tell a refusal from a crash either — an AttributeError
# from a deleted constant also exits non-zero. Assert the refusal the proxy
# actually makes, and name the host, so the test fails when the guard it is
# named after is the one that was removed.
#
# `test_proxy_refuses_arbitrary_upstream_host` above already did both. These
# two were its drifted siblings.


@pytest.mark.parametrize("upstream,host", [
    ("https://169.254.169.254/latest/meta-data/", "169.254.169.254"),
    ("https://[fd00:ec2::254]/latest/meta-data/", "fd00:ec2::254"),
    # KEEP THE http:// ROW. Moving these fixtures to https:// silently DROPPED
    # a property the weak originals did have: that an http non-allowlisted
    # upstream is refused. Measured — scope the allowlist to https and delete
    # the loopback guard (the shape an "allow an insecure mirror" escape hatch
    # would take) and without this row the whole file goes green on a proxy
    # that accepts http://169.254.169.254/latest/meta-data/. Costs nothing: the
    # unmutated proxy refuses it with the same "not in allowlist".
    ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
])
def test_proxy_refuses_metadata_service_upstream(upstream: str, host: str) -> None:
    """The cloud instance-metadata service is refused BY THE ALLOWLIST.

    This is the classic SSRF target: reaching it from inside a network yields
    instance credentials. The proxy holds a real API key, so an upstream it
    will forward to is a credential-exfiltration channel.
    """
    result = _refusal(upstream)

    assert "not in allowlist" in result.stderr, (
        f"refused, but not by the upstream allowlist this test is named for:\n"
        f"{result.stderr}")
    assert host in result.stderr, (
        f"the refusal does not name the host it rejected: {result.stderr}")


def test_proxy_refuses_internal_network() -> None:
    """An internal/RFC1918-style host is refused BY THE ALLOWLIST."""
    result = _refusal("https://internal.corp/admin")

    assert "not in allowlist" in result.stderr, result.stderr
    assert "internal.corp" in result.stderr, result.stderr


def _allowed_hosts() -> frozenset[str]:
    """The shipped allowlist, read from the shipped file.

    Imported rather than re-typed: a copy in the test would drift, and a test
    that pins its own copy pins nothing. Importing is safe — `main()` is behind
    an `if __name__ == "__main__"` guard.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_proxy_under_test", PROXY_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ProxyConfig._ALLOWED_UPSTREAM_HOSTS


def test_the_allowlist_CONTENTS_are_pinned() -> None:
    """Three hostile literals do not pin a list that anyone can extend.

    A reviewer asked for a mutation that re-opens SSRF in a different spelling
    and found this one: adding `evil.example.net` to the frozenset left every
    test in this file green, because each one names a host it expects to be
    REFUSED and none says what may be ALLOWED. Widening an allowlist is the
    likeliest way this guard actually dies — a plausible-looking one-line diff
    in a hurry — and it was the one case nothing watched.

    Changing this set is a security decision. Changing it here too is the
    point: it should not be possible to do quietly.
    """
    assert _allowed_hosts() == frozenset({
        "api.anthropic.com",
        "127.0.0.1",
        "localhost",
        "::1",
    }), (
        "the upstream allowlist changed. Every host here is somewhere the "
        "proxy will forward a real API credential. If the change is "
        "deliberate, say so in the commit message and update this test in the "
        "same commit."
    )


@pytest.mark.parametrize("variant", [
    "{h}.attacker.test",   # suffix confusion: defeats `startswith`
    "attacker-{h}",        # prefix confusion: defeats `endswith`
    "x.{h}.attacker.test",  # both at once: defeats a naive `in`
])
def test_a_host_that_merely_CONTAINS_an_allowed_name_is_refused(variant: str) -> None:
    """Pins the MATCHING RULE, not just the membership list.

    The same reviewer found two more mutations that passed every test here:

        if not any(a in host for a in ALLOWED):          # substring
        if not any(host.startswith(a) for a in ALLOWED)  # prefix

    Either one accepts `https://api.anthropic.com.attacker.test/` and
    `https://localhost.attacker.test/` — domains an attacker simply registers.
    The shipped code does exact membership (`host not in ALLOWED`), which is
    correct; nothing asserted that it stays correct.

    Derived FROM the allowlist rather than hardcoded, so a host added there is
    automatically probed for the same confusions instead of being exempt from
    them by omission.
    """
    for allowed in sorted(_allowed_hosts()):
        if ":" in allowed:      # IPv6 literal — bracket rules differ, skip
            continue
        hostile = variant.format(h=allowed)
        result = _refusal(f"https://{hostile}/v1/messages")
        assert "not in allowlist" in result.stderr, (
            f"{hostile!r} was not refused by the allowlist. A host that merely "
            f"contains {allowed!r} is a DIFFERENT host, owned by whoever "
            f"registered it — matching must be exact.\n{result.stderr}")


def test_the_https_fixture_is_refused_BY_NAME_not_by_scheme() -> None:
    """What this measures, stated honestly, because its first name oversold it.

    It was called `..._rests_on_the_ALLOWLIST_ALONE`, and it CANNOT establish
    that. A reviewer inserted a second guard right behind the allowlist and the
    whole file stayed green: "alone" is a counterfactual about REMOVING the
    allowlist, and only a mutation can observe a counterfactual. A test that
    reads the unmutated proxy's output can never see it.

    What it does establish, which is still worth pinning: the https fixtures
    above are refused by the HOST ALLOWLIST and not by the http-loopback guard.
    That is what makes them meaningful as SSRF tests rather than as accidental
    scheme tests. The claim that the allowlist is the only guard behind them is
    carried by the mutation recorded in the comment block above, and by nothing
    in this function.
    """
    result = _refusal("https://169.254.169.254/latest/meta-data/")

    assert "not in allowlist" in result.stderr, result.stderr
    assert "loopback" not in result.stderr, (
        f"the http-loopback guard answered instead of the allowlist, so this "
        f"fixture is testing the scheme rule and not SSRF:\n{result.stderr}")


def test_proxy_refuses_plain_http_to_anthropic() -> None:
    """http:// (vs https://) is refused for the production API."""
    result = _spawn_proxy({
        "BOTAINER_PROXY_UPSTREAM": "http://api.anthropic.com",
    })
    assert result.returncode != 0
    assert "https" in result.stderr.lower() or "loopback" in result.stderr.lower()


def _spawn_proxy_alive(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.Popen[bytes]:
    """Spawn the proxy in background. Caller must terminate it."""
    base_env = {
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "proxy.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "eph",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(tmp_path / "creds.json"),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    base_env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)],
        env=base_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_proxy(proc: subprocess.Popen[bytes]) -> None:
    """Robust proxy teardown: SIGTERM then SIGKILL after short wait."""
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def test_proxy_accepts_https_anthropic(tmp_path: Path) -> None:
    """Default upstream (https://api.anthropic.com) passes validation.
    Proxy starts and runs; we kill it to verify it actually started."""
    proc = _spawn_proxy_alive(
        {"BOTAINER_PROXY_UPSTREAM": "https://api.anthropic.com"},
        tmp_path,
    )
    import time
    for _ in range(20):
        if (tmp_path / "proxy.sock").exists():
            break
        time.sleep(0.05)
    try:
        assert (tmp_path / "proxy.sock").exists(), "proxy didn't start listening"
    finally:
        _stop_proxy(proc)


def test_proxy_accepts_http_loopback(tmp_path: Path) -> None:
    """http://127.0.0.1 is allowed for testing."""
    proc = _spawn_proxy_alive(
        {"BOTAINER_PROXY_UPSTREAM": "http://127.0.0.1:9999"},
        tmp_path,
    )
    import time
    for _ in range(20):
        if (tmp_path / "proxy.sock").exists():
            break
        time.sleep(0.05)
    try:
        assert (tmp_path / "proxy.sock").exists()
    finally:
        _stop_proxy(proc)


# ────────── HIGH 4: start_proxy.py doesn't pass through stale shell vars ──────────


def test_start_proxy_does_not_inherit_stale_upstream_env(
    tmp_path: Path,
) -> None:
    """If the user has BOTAINER_PROXY_UPSTREAM set in their shell, the
    hook ignores it and uses the plugin config's value.

    Without this defense, a malicious wrapper could redirect upstream
    by setting an env var before invoking botainer.
    """
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))

    creds = tmp_path / "creds.json"
    creds.write_text('{"api_key": "test"}')
    (tmp_path / "proj").mkdir()

    # Set a malicious BOTAINER_PROXY_UPSTREAM in the env we pass.
    # The hook should ignore it (build env from scratch).
    result = subprocess.run(
        [sys.executable, str(START_HOOK)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            "BOTAINER_TESTING": "1",
            "BOTAINER_PROXY_CREDS_PATH_OVERRIDE": str(creds),
            # ATTACK: stale shell var trying to redirect upstream.
            "BOTAINER_PROXY_UPSTREAM": "https://attacker.example.com",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    # If the hook passed BOTAINER_PROXY_UPSTREAM through, the proxy would
    # have started successfully with the malicious upstream. Now the hook
    # builds env from scratch and the proxy starts with the default
    # (https://api.anthropic.com). The proxy itself accepts the default,
    # so the hook succeeds (rc=0).
    assert result.returncode == 0, result.stderr
    # Stop the spawned proxy.
    import json as _json
    import signal as _sig
    payload = _json.loads(result.stdout)
    try:
        os.kill(payload["proxy_pid"], _sig.SIGTERM)
    except ProcessLookupError:
        pass


def test_start_proxy_refuses_creds_override_without_testing_flag(
    tmp_path: Path,
) -> None:
    """BOTAINER_PROXY_CREDS_PATH_OVERRIDE only takes effect with
    BOTAINER_TESTING=1 (defense against stale env redirecting creds)."""
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))

    bogus_creds = tmp_path / "bogus.json"
    bogus_creds.write_text('{"api_key": "bogus"}')
    (tmp_path / "proj").mkdir()

    # No BOTAINER_TESTING=1 → override ignored → real creds path used →
    # which doesn't exist → hook refuses with "credentials file not found".
    # BOTAINER_PROXY_EXPERIMENTAL=1 bypasses the T0-3 not-functional guard so
    # this test still exercises the creds-override gating (the guard fires
    # first otherwise, and we'd be asserting the wrong refusal).
    result = subprocess.run(
        [sys.executable, str(START_HOOK)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            "BOTAINER_PROXY_CREDS_PATH_OVERRIDE": str(bogus_creds),
            "BOTAINER_PROXY_EXPERIMENTAL": "1",  # bypass the T0-3 guard only
            # NOT setting BOTAINER_TESTING=1
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "credentials file not found" in result.stderr


def _commands_named_but_absent(text: str) -> list[str]:
    """Every backticked remedy in `text` that a user cannot actually run.

    Two ways to fail, and the FIRST one is the one that shipped:

    1. A BARE FLAG. The refusal said "Use `--mode=shared` … or
       `--mode=isolated`" with no command attached. `--mode` does exist — on
       `auth login` and `hpc submit` — so "is this flag real?" answers yes and
       misses it entirely; what the reader does is put it on the command that
       just failed, and `botainer start --mode=shared` answers "Error: No such
       option". A remedy has to name the command, which is this project's own
       rule about never handing someone a command without saying where it runs.
    2. A `botainer …` command or flag the live click tree does not have,
       resolved against the tree rather than a fixture, so a rename shows up
       here without anyone remembering to update a list.

    The first version of this helper only did (2), and only for strings
    starting with "botainer" — so it was proven against a form the hook never
    had and was blind to the one it did. The loop tzar caught that at
    checkpoint 10 by restoring the real pre-fix string and watching the suite
    stay green.
    """
    import re

    import click

    from botainer.cli.main import cli

    missing: list[str] = []
    for quoted in re.findall(r"`([^`]+)`", text):
        tokens = quoted.split()
        if not tokens:
            continue
        if tokens[0].startswith("-"):
            missing.append(
                f"{quoted!r}: a bare flag is not something a user can run — "
                f"name the command it belongs to")
            continue
        if tokens[0] != "botainer":
            continue
        cmd = cli
        rest = tokens[1:]
        while rest and isinstance(cmd, click.Group):
            sub = cmd.get_command(None, rest[0])  # type: ignore[arg-type]
            if sub is None:
                break
            cmd, rest = sub, rest[1:]
        if cmd is cli:
            missing.append(f"{quoted!r}: names no botainer command")
            continue
        # `secondary_opts` too: click stores the second half of a
        # `--shared/--isolated` pair there, so reading `opts` alone
        # REFUSED a correct remedy naming `auth login --isolated`.
        opts = {o for p in cmd.params
                for o in getattr(p, "opts", []) + getattr(p, "secondary_opts", [])}
        for tok in rest:
            if tok.startswith("-") and tok.split("=")[0] not in opts:
                missing.append(
                    f"{quoted!r}: {tok.split('=')[0]} is not an option of "
                    f"`{cmd.name}`")
    return missing


def test_start_proxy_refuses_as_not_functional_by_default(tmp_path: Path) -> None:
    """T0-3 / #59: without an escape hatch, the proxy hook fails fast with an
    explicit 'NOT FUNCTIONAL at v0.1.0' refusal BEFORE spawning anything.

    Rationale: the proxy's ephemeral ANTHROPIC_API_KEY is refused by the
    launcher's credential-leak guard, so a proxy session can't start on any
    runtime. The hook must refuse legibly here rather than spawn the proxy and
    die later at composition.py's leak check with a confusing message.
    """
    from botainer.core.spec import SessionSpec
    from botainer.state import session_record as sr

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "ses12345abc"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="ses12345abc",
        project_uuid="u",
        project_root=str(tmp_path / "proj"),
        state_dir=str(state_dir),
        runtime="docker",
        image="img",
    )
    sr.write(session_dir, sr.from_spec(spec))
    (tmp_path / "proj").mkdir()

    result = subprocess.run(
        [sys.executable, str(START_HOOK)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BOTAINER_SESSION_RECORD_PATH": str(session_dir / "spec.json"),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
            "BOTAINER_PLUGIN": "agent-claude-proxy",
            "BOTAINER_HOOK_WHEN": "pre_session",
            # NEITHER BOTAINER_TESTING nor BOTAINER_PROXY_EXPERIMENTAL set.
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "NOT FUNCTIONAL" in result.stderr
    # Fails fast BEFORE emitting any contribution (no proxy spawned, so no
    # proxy_pid to leak or clean up).
    assert result.stdout.strip() == ""
    # ...and the way OUT that it names must exist. This refusal is the ONLY
    # thing a proxy-configured user sees — the same remedy in the consent
    # summary never renders, because this hook refuses first. It spent months
    # naming `--mode=shared`, which is not an option of any command: the user
    # was told to run something that answers "Error: No such option".
    assert not _commands_named_but_absent(result.stderr), (
        f"the refusal names a command or flag the CLI does not have: "
        f"{_commands_named_but_absent(result.stderr)}")


# ────────── CREDENTIAL-PROXY-INVESTIGATION findings ──────────


def test_proxy_refuses_world_readable_credential_file(tmp_path: Path) -> None:
    """Investigation finding #1: file mode 0644 must be refused.

    A buggy write left the file world-readable; previously the proxy
    silently read it and forwarded the key. Now: refuse loudly.
    """
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    # Reload the proxy module to pick up our changes.
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "bad-mode.json"
    creds.write_text('{"api_key": "x"}')
    os.chmod(creds, 0o644)

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    })
    cfg = proxy_mod.ProxyConfig()
    with pytest.raises(RuntimeError, match="mode|broader than 0600"):
        cfg.load_real_credential()


def test_proxy_accepts_mode_0600_credential_file(tmp_path: Path) -> None:
    """Investigation finding #1: mode 0600 (and 0400) must be accepted."""
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "good-mode.json"
    creds.write_text('{"api_key": "secret-xyz"}')
    os.chmod(creds, 0o600)

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    })
    cfg = proxy_mod.ProxyConfig()
    # load_real_credential returns (kind, value) tuple after the
    # OAuth READ refactor (PROXY-REFRESH-INVESTIGATION.md Gap 2).
    assert cfg.load_real_credential() == ("api_key", "secret-xyz")


def test_audit_log_hash_chain_verifies_clean(tmp_path: Path) -> None:
    """Investigation finding #2: hash chain detects tampering."""
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "creds.json"
    creds.write_text('{"api_key": "x"}')
    os.chmod(creds, 0o600)
    audit = tmp_path / "audit.jsonl"

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit),
    })
    cfg = proxy_mod.ProxyConfig()
    # Write three audit entries.
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=429)
    ok, detail = proxy_mod.verify_audit_chain(audit)
    assert ok, detail


def test_audit_log_hash_chain_detects_truncation(tmp_path: Path) -> None:
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    creds = tmp_path / "creds.json"
    creds.write_text('{"api_key": "x"}')
    os.chmod(creds, 0o600)
    audit = tmp_path / "audit.jsonl"

    os.environ.update({
        "BOTAINER_PROXY_SOCKET_PATH": str(tmp_path / "s.sock"),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "e",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit),
    })
    cfg = proxy_mod.ProxyConfig()
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    proxy_mod._audit(cfg, kind="forwarded", method="POST", path="/v1/messages", status=200)
    # Attacker truncates the first entry.
    lines = audit.read_bytes().splitlines(keepends=True)
    audit.write_bytes(lines[1])  # only the second line
    ok, detail = proxy_mod.verify_audit_chain(audit)
    assert not ok
    assert "prev_hash mismatch" in detail


def test_audit_log_verify_empty_log_ok(tmp_path: Path) -> None:
    import sys as _sys
    sys.path.insert(0, str(PROXY_SCRIPT.parent))
    if "proxy" in _sys.modules:
        del _sys.modules["proxy"]
    proxy_mod = __import__("proxy")
    sys.path.pop(0)

    audit = tmp_path / "audit.jsonl"
    ok, detail = proxy_mod.verify_audit_chain(audit)
    assert ok


def test_the_proxy_actually_DIES_on_SIGTERM(tmp_path):
    """It ignored SIGTERM entirely, and that was OBSERVED before it was explained.

    A `timeout 5 python3 proxy.py` from an unrelated measurement was still alive
    **3h29m** later, parked in `futex_wait_queue`, socket not unlinked, no
    `proxy_stopped` audit record. `timeout` had sent SIGTERM at five seconds.

    THE CAUSE. `socketserver.shutdown()` sets a flag and then BLOCKS until
    `serve_forever()` acknowledges it. A signal handler runs ON the thread inside
    `serve_forever`, so the thing it waits for cannot happen — the documented
    CPython deadlock. The #108 watchdog in the same file gets this right (it
    calls `shutdown()` from a thread) and `plugins/wolfram-sidecar/proxy.py` has
    always got it right, so the correct shape was in the repo twice while the
    signal path had it wrong.

    WHY A TEST AND NOT A COMMENT: this is only visible by running the process and
    signalling it. Every static lens in this repo reads text, and a handler that
    deadlocks looks perfectly reasonable in source — `server.shutdown()` is the
    documented way to stop a socketserver, just not from there.

    Bounded in production today by `stop_proxy.py` escalating to SIGKILL after
    3s, and by whole-proxy auth mode being refused at v0.1.0. Neither is a reason
    to keep a handler that cannot run, and neither will still be true when the
    mode is un-refused.
    """
    import signal
    import time

    sock = tmp_path / "p.sock"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BOTAINER_PROXY_SOCKET_PATH": str(sock),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": "eph",
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(tmp_path / "creds.json"),
        "BOTAINER_PROXY_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "BOTAINER_PROXY_UPSTREAM": "https://api.anthropic.com",
    }
    proc = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):                      # up to 10s to bind
            time.sleep(0.1)
            if sock.exists():
                break
        else:
            proc.kill()
            raise AssertionError("the proxy never bound its socket")

        proc.send_signal(signal.SIGTERM)
        for _ in range(50):                       # 5s to die
            time.sleep(0.1)
            if proc.poll() is not None:
                break
        else:
            raise AssertionError(
                "the proxy is STILL RUNNING 5s after SIGTERM. `shutdown()` is "
                "being called on the serving thread and has deadlocked — run it "
                "on a throwaway thread, as the #108 watchdog does.")

        assert proc.returncode == 0, (
            f"died, but not cleanly: rc={proc.returncode}")
        assert not sock.exists(), (
            "exited without unlinking its socket; the next launch fails with "
            "`address in use` on the same path")
        audit = tmp_path / "audit.jsonl"
        assert audit.exists() and "proxy_stopped" in audit.read_text(), (
            "no `proxy_stopped` audit record — the shutdown path did not run "
            "its cleanup, so a stop is indistinguishable from a crash")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
