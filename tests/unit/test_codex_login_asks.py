"""The Codex login must run in a CONTAINER, and must ASK which account to bill.

Two defects reported by the user on, fixed together because they had
one cause and one fix.

1. "why are you asking for api key?" — the login decided from
   `shutil.which("codex")` alone: binary present -> OAuth (subscription),
   absent -> paste a key (API account, per token). The user was never asked. As
   they put it, sharper than my own framing: "it didn't decide for me - first it
   failed, then went to something else. It surprised me." A step FAILED and the
   failure was silently converted into a DIFFERENT ACTION with a different cost.

2. "Are you stealing credentials from the host again??" — it ran
   `subprocess.run(["codex", "login"])` ON THE HOST with the full host
   environment and HOME, merely SETTING CODEX_HOME and hoping the CLI honoured
   it. The plugin's own comment admitted that was unverified, and it detected
   the breach AFTER the fact ("the L1 'no host credential store' promise was
   violated for this login"). Its sibling agent-claude-shared has always run
   the OAuth flow in a container precisely so that cannot happen.

Running the login in the agent-codex container fixes both: the container ships
codex (so no host install, which is why it failed on the cluster at all), and
inside --containall there is no host ~/.codex to reach.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import sys
from pathlib import Path

import pytest

_HOOK = (Path(__file__).resolve().parents[2]
         / "plugins" / "agent-codex-shared" / "hooks" / "login.py")


def _load():
    spec = importlib.util.spec_from_file_location("_codex_login", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# The structural property: no host binary, ever.
# --------------------------------------------------------------------------

def test_the_login_never_executes_a_host_codex_binary() -> None:
    """THE regression guard for the L1 breach.

    Asserted against the SOURCE rather than by mocking, because the failure
    mode is a future author reaching for `subprocess.run(["codex", ...])` as an
    obvious shortcut. A behavioural test would only catch it on the path the
    test happens to drive; this catches it anywhere in the file.
    """
    import ast

    # AST, not a substring. check-test-assertion-shapes rejected the grep
    # version and was right: the module docstring QUOTES the old
    # `subprocess.run(["codex", ...])` call, so a text search is satisfied by
    # prose. Walking the tree asks the only question that matters — is there a
    # CALL whose argv literal starts with a bare "codex" — which prose cannot
    # answer either way.
    tree = ast.parse(_HOOK.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
            continue
        head = first.elts[0]
        if isinstance(head, ast.Constant) and head.value == "codex":
            offenders.append(getattr(node, "lineno", "?"))
    assert offenders == [], (
        f"line(s) {offenders}: exec the HOST codex binary. That is the L1 'no "
        f"host credential store' breach this rewrite removed — run it inside "
        f"the agent-codex container instead.")


def test_docker_argv_is_a_one_shot_container_with_a_single_bind() -> None:
    mod = _load()
    argv = mod.build_docker_argv("botainer/agent-codex:0.1",
                                 Path("/S/shared-auth/agent-codex"), 54545, 54549)
    assert "--rm" in argv, "container must not persist"
    assert "--init" in argv, "orphan reaping (#108) applies here too"
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert binds == ["/S/shared-auth/agent-codex:/out"], (
        f"expected exactly one bind, got {binds}")
    assert "CODEX_HOME=/out" in argv
    assert "umask 0077 && exec codex login" in argv[-1]


def test_apptainer_argv_contains_and_refuses_home() -> None:
    """`--no-home` is not decoration: some apptainer builds bind $HOME even
    under --containall, which would put ~/.ssh and ~/.aws inside the very
    login container that exists to avoid them."""
    mod = _load()
    argv = mod.build_apptainer_argv(
        "/usr/bin/apptainer", Path("/S/images/botainer-agent-codex.sif"),
        Path("/S/shared-auth/agent-codex"))
    for flag in ("--containall", "--cleanenv", "--no-home"):
        assert flag in argv, f"missing {flag}"
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert binds == ["/S/shared-auth/agent-codex:/out"]


# --------------------------------------------------------------------------
# The choice: asked, never inferred.
# --------------------------------------------------------------------------

def test_non_interactive_with_no_choice_REFUSES(monkeypatch, capsys) -> None:
    """Fail CLOSED on the option that spends money. With no TTY and no explicit
    setting the old code would have silently picked one."""
    mod = _load()
    monkeypatch.delenv("BOTAINER_CODEX_LOGIN_METHOD", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    with pytest.raises(SystemExit) as exc:
        mod._choose_method(oauth_available=False)
    assert exc.value.code == 2
    assert "not choosing a billing method on your behalf" in \
        capsys.readouterr().err.lower()


@pytest.mark.parametrize("value,expected", [
    ("oauth", "oauth"), ("api-key", "api-key"),
    ("apikey", "api-key"), ("key", "api-key"), ("OAuth", "oauth"),
])
def test_an_explicit_setting_is_honoured(monkeypatch, value, expected) -> None:
    mod = _load()
    monkeypatch.setenv("BOTAINER_CODEX_LOGIN_METHOD", value)
    assert mod._choose_method(oauth_available=True) == expected


def test_a_bogus_setting_refuses_rather_than_guessing(
        monkeypatch, capsys) -> None:
    """`subscription` is the plausible-but-wrong value someone WILL type,
    since that is the word for what OAuth bills. Refusing is only useful if it
    says which value was rejected and what the valid ones are — a bare exit
    code sends them back to the source to find out."""
    mod = _load()
    monkeypatch.setenv("BOTAINER_CODEX_LOGIN_METHOD", "subscription")
    with pytest.raises(SystemExit):
        mod._choose_method(oauth_available=True)
    err = capsys.readouterr().err
    assert "subscription" in err, "did not echo the value it rejected"
    assert "oauth" in err and "api-key" in err, "did not name the valid values"


def test_the_prompt_states_that_the_two_paths_bill_differently(
        monkeypatch, capsys) -> None:
    """A choice offered without its consequence is not a choice. The old text
    said "Falling back to API-key paste" — true, and useless: it named the
    mechanism and omitted the cost."""
    mod = _load()
    monkeypatch.delenv("BOTAINER_CODEX_LOGIN_METHOD", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "2")
    assert mod._choose_method(oauth_available=True) == "api-key"
    err = capsys.readouterr().err
    assert "DIFFERENT accounts" in err
    assert "SUBSCRIPTION" in err
    assert "PER TOKEN" in err


def test_choosing_oauth_without_the_image_refuses_with_the_remedy(
        monkeypatch, capsys) -> None:
    """Never silently downgrade to the paid path when OAuth was asked for —
    that IS the original bug."""
    mod = _load()
    monkeypatch.delenv("BOTAINER_CODEX_LOGIN_METHOD", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "1")
    with pytest.raises(SystemExit):
        mod._choose_method(oauth_available=False)
    assert "botainer image build agent-codex" in capsys.readouterr().err


def test_unavailable_oauth_is_explained_not_hidden(monkeypatch, capsys) -> None:
    mod = _load()
    monkeypatch.delenv("BOTAINER_CODEX_LOGIN_METHOD", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "2")
    mod._choose_method(oauth_available=False)
    err = capsys.readouterr().err
    assert "NO" in err
    assert "botainer image build agent-codex" in err


# --------------------------------------------------------------------------
# Which OAuth flow. The user was logging in on a cluster node when codex told
# them to run `codex login --device-auth`, which they could not do — and the
# browser flow could not work either, since no browser there can reach the
# callback port.
# --------------------------------------------------------------------------

def test_device_code_is_the_default(monkeypatch) -> None:
    """One option works EVERYWHERE; the other works locally only. When a branch
    is universally correct, detecting which branch you are in is cleverness
    that can only be wrong.

    An earlier version sniffed SSH_CONNECTION/DISPLAY to guess. The user
    retired it: "you really think I'd default into running ssh command line?"
    """
    mod = _load()
    monkeypatch.delenv("BOTAINER_CODEX_OAUTH_FLOW", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "")   # just press enter
    assert mod._choose_oauth_flow() is True


def test_non_interactive_defaults_to_device_code_not_to_a_tunnel(
        monkeypatch) -> None:
    """Automation must not land on the flow that needs a human, a browser and
    an ssh tunnel."""
    mod = _load()
    monkeypatch.delenv("BOTAINER_CODEX_OAUTH_FLOW", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert mod._choose_oauth_flow() is True


@pytest.mark.parametrize("value,expected", [
    ("device", True), ("device-auth", True), ("deviceauth", True),
    ("browser", False), ("callback", False), ("local", False),
])
def test_the_flow_can_be_forced(monkeypatch, value, expected) -> None:
    mod = _load()
    monkeypatch.setenv("BOTAINER_CODEX_OAUTH_FLOW", value)
    assert mod._choose_oauth_flow() is expected


def test_device_flow_publishes_NO_ports(monkeypatch) -> None:
    """The device-code flow opens no callback listener, so publishing a port
    range for it would widen the surface for nothing."""
    mod = _load()
    argv = mod.build_docker_argv("img", Path("/S/c"), 54545, 54549,
                                 device_auth=True)
    assert "-p" not in argv, "published a port the device flow never listens on"
    assert argv[-1].endswith("codex login --device-auth")


def test_browser_flow_still_publishes_the_callback_port() -> None:
    mod = _load()
    argv = mod.build_docker_argv("img", Path("/S/c"), 54545, 54549,
                                 device_auth=False)
    assert "-p" in argv
    assert argv[-1].endswith("codex login")


def test_apptainer_gets_the_device_flag_too() -> None:
    """HPC is where this matters most — it is the case that prompted it."""
    mod = _load()
    argv = mod.build_apptainer_argv("/u/apptainer", Path("/S/x.sif"),
                                    Path("/S/c"), device_auth=True)
    assert argv[-1].endswith("codex login --device-auth")


# --------------------------------------------------------------------------
# The two codex logins are duplicated on purpose (hooks are standalone scripts
# and none of them import the botainer package — that isolation is deliberate).
# Duplication is only acceptable if it is CHECKED: unchecked copies are how one
# auth mode ends up hardened and the other not, which is the exact shape of
# every codex bug found on.
# --------------------------------------------------------------------------

_ISOLATED_HOOK = (Path(__file__).resolve().parents[2]
                  / "plugins" / "agent-codex" / "hooks" / "login.py")


def _load_isolated():
    spec = importlib.util.spec_from_file_location("_codex_login_iso",
                                                  _ISOLATED_HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("device_auth", [True, False])
def test_both_codex_logins_build_the_same_docker_argv(device_auth) -> None:
    """Same inputs must give the same container. The creds dir is a PARAMETER,
    so the only intended difference between isolated and shared — per-project
    vs host-wide credential — cannot show up here."""
    shared, iso = _load(), _load_isolated()
    args = ("botainer/agent-codex:0.1", Path("/S/creds"), 54545, 54549,
            device_auth)
    assert iso.build_docker_argv(*args) == shared.build_docker_argv(*args)


@pytest.mark.parametrize("device_auth", [True, False])
def test_both_codex_logins_build_the_same_apptainer_argv(device_auth) -> None:
    """HPC is the product; a hardening that lands on one mode's apptainer argv
    and not the other's is a parity break that no user would ever see coming."""
    shared, iso = _load(), _load_isolated()
    args = ("/usr/bin/apptainer", Path("/S/x.sif"), Path("/S/creds"), device_auth)
    assert iso.build_apptainer_argv(*args) == shared.build_apptainer_argv(*args)


def test_the_isolated_login_never_executes_a_host_codex_binary() -> None:
    """Same L1 guard as the shared login: the OAuth flow must run in the
    container, never against a host codex install."""
    tree = ast.parse(_ISOLATED_HOOK.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
            continue
        head = first.elts[0]
        if isinstance(head, ast.Constant) and head.value == "codex":
            offenders.append(getattr(node, "lineno", "?"))
    assert offenders == [], (
        f"line(s) {offenders}: exec the HOST codex binary — the L1 'no host "
        f"credential store' breach. Run it inside the agent-codex container.")


def test_isolated_api_key_is_stored_RAW_not_json(tmp_path, monkeypatch) -> None:
    """entrypoint_wrap.sh does `export OPENAI_API_KEY="$(cat "$FILE")"`, so a
    JSON-wrapped key would export the whole blob as the key. agent-codex-shared
    writes JSON because nothing cats ITS file; the two formats are deliberate
    and must not be "unified" by a future tidy-up.

    Asserted on the BYTES WRITTEN, not the source: a source grep for
    `json.dumps` stays green if the behaviour is deleted, and
    check-test-assertion-shapes rejected exactly that version of this test.
    """
    mod = _load_isolated()
    creds_file = tmp_path / "api_key"
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-test-abc123\n"))

    assert mod._run_api_key_paste(creds_file) == 0
    written = creds_file.read_text()

    assert written.strip() == "sk-test-abc123", (
        f"the entrypoint cats this file into OPENAI_API_KEY; it must contain "
        f"the bare key and nothing else, got {written!r}")
    assert not written.lstrip().startswith("{"), "key was JSON-wrapped"
    assert oct(creds_file.stat().st_mode)[-3:] == "600"
