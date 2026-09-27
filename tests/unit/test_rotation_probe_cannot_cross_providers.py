"""`rotation-probe` must not be able to send one provider's token to another.

THE HAZARD, and why it is the serious half. The command took `--agent <anything>`
while its token endpoint was a module constant pinned to Anthropic. Those two
facts had no connection: nothing compared the family to the endpoint. Measured by
planting a `.credentials.json` under `agent-codex` and intercepting the request
rather than making it — the token in that file was handed to
`https://api.anthropic.com/v1/oauth/token`.

It had never happened in practice for exactly one reason: codex's real credential
is named `auth.json`, so the lookup missed. A coincidence of spelling is not a
security boundary, and anything that ever writes claude's filename under the
codex family turns it into a live cross-provider token disclosure.

THE FIX IS STRUCTURAL, not a check. Endpoint, client id, credential filename and
OAuth block key now all come out of ONE per-family registry entry, and
`_post_refresh` takes the endpoint and client id as REQUIRED keyword arguments.
So there is no way to obtain an endpoint without naming a family, and naming a
family also fixes which file is read. Omitting them is a `TypeError` rather than
a silent default.

AND THE DEAD-END LOOP IT ALSO REMOVES. `--agent agent-codex` used to look for
claude's `.credentials.json`, miss, and print "Run `botainer auth login` first".
A codex user who WAS logged in ran the login, it succeeded, and the probe refused
identically — with their real `auth.json` sitting in the same directory. The
refusal now names the family and says logging in again will not change the
answer.

BOTH DIRECTIONS ARE PINNED. Claude must still resolve the Anthropic endpoint and
reach the probe: a fix that refused everything would satisfy the hazard tests
while deleting the command.
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

import botainer.cli.auth_probe as auth_probe
from botainer.state import dir as state_dir

ANTHROPIC = "https://api.anthropic.com/v1/oauth/token"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A state root, with every outbound refresh intercepted and RECORDED.

    Recording rather than blocking is deliberate: a test that merely stubs the
    network proves the command did not crash. What has to be asserted is WHERE
    the token would have gone, which means capturing the endpoint argument.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    root = state_dir.ensure_user_state_dir(create_if_missing=True).root

    sent: list[dict] = []

    def _record(refresh_token, *, endpoint, client_id, timeout=20.0):
        sent.append({"endpoint": endpoint, "client_id": client_id,
                     "token": refresh_token})
        return 400, {"error": "intercepted by the test; no request was made"}

    monkeypatch.setattr(auth_probe, "_post_refresh", _record)

    def _plant(family: str, filename: str, doc: dict, mode: int = 0o600):
        d = root / "shared-auth" / family
        d.mkdir(parents=True, exist_ok=True)
        p = d / filename
        p.write_text(json.dumps(doc), encoding="utf-8")
        os.chmod(p, mode)
        return p

    def _run(*args):
        return CliRunner().invoke(auth_probe.rotation_probe, list(args))

    return _plant, _run, sent, root


def test_a_token_under_the_codex_family_never_reaches_anthropic(probe):
    """THE HAZARD. The planted file is the exact one that used to be posted."""
    plant, run, sent, _ = probe
    plant("agent-codex", ".credentials.json",
          {"claudeAiOauth": {"refreshToken": "OPENAI_SHAPED_TOKEN"}})

    res = run("--agent", "agent-codex", "--yes")

    assert not sent, (
        f"a refresh was attempted for a non-anthropic family; the token would "
        f"have gone to {sent[0]['endpoint'] if sent else '?'}")
    assert res.exit_code == 2, f"expected a refusal, got {res.exit_code}"


def test_the_codex_refusal_names_the_family_and_does_not_send_you_to_log_in(probe):
    """THE DEAD-END LOOP. "Run `botainer auth login` first" was the whole defect:
    true-looking, actionable, and wrong — the login succeeds and changes
    nothing."""
    plant, run, _, _ = probe
    plant("agent-codex", "auth.json", {"tokens": {"refresh_token": "CODEX_RT"}})

    out = run("--agent", "agent-codex", "--yes").output

    assert "agent-codex" in out, f"the refusal does not name the family:\n{out}"
    assert "auth login" not in out, (
        f"the refusal still points at a login that cannot help — a codex user "
        f"who IS logged in runs it and gets this same message:\n{out}")


def test_the_refusal_is_about_the_family_not_a_missing_file(probe):
    """The distinction that makes the message true. With codex's REAL credential
    present the answer must be unchanged, or the refusal is still secretly
    "I could not find a file"."""
    plant, run, _, _ = probe
    plant("agent-codex", "auth.json", {"tokens": {"refresh_token": "CODEX_RT"}})
    plant("agent-codex", ".credentials.json",
          {"claudeAiOauth": {"refreshToken": "ALSO_HERE"}})

    out = run("--agent", "agent-codex", "--yes").output

    assert "no credential at" not in out, (
        f"with two credential files present it still reports absence:\n{out}")


def test_a_bare_family_name_resolves_the_same_as_the_prefixed_one(probe):
    """`--agent codex` used to look in `shared-auth/codex/` — a directory that
    never exists — so it failed for a second, different reason."""
    plant, run, _, _ = probe
    plant("agent-codex", "auth.json", {"tokens": {"refresh_token": "CODEX_RT"}})

    bare = run("--agent", "codex", "--yes").output
    prefixed = run("--agent", "agent-codex", "--yes").output

    assert bare == prefixed, (
        f"the two spellings of one family disagree.\nbare:\n{bare}\n"
        f"prefixed:\n{prefixed}")


def test_an_unknown_family_is_refused_with_the_known_list(probe):
    _, run, sent, _ = probe

    out = run("--agent", "frobnicator", "--yes").output

    assert not sent, "a refresh was attempted for an agent that does not exist"
    assert "claude" in out and "codex" in out, (
        f"the refusal does not say which families ARE known:\n{out}")


def test_claude_still_reaches_the_anthropic_endpoint(probe):
    """OPPOSITE DIRECTION, and the half that keeps the command alive.

    A fix that refused every family would pass every test above.
    """
    plant, run, sent, _ = probe
    plant("agent-claude", ".credentials.json",
          {"claudeAiOauth": {"refreshToken": "CLAUDE_RT", "accessToken": "a"}})

    run("--agent", "agent-claude", "--yes")

    assert len(sent) == 1, f"claude no longer reaches the probe: {sent!r}"
    assert sent[0]["endpoint"] == ANTHROPIC, (
        f"claude's refresh went to {sent[0]['endpoint']!r}")
    assert sent[0]["token"] == "CLAUDE_RT", (
        f"the wrong token was sent: {sent[0]['token']!r}")


def test_the_real_post_refresh_requires_endpoint_and_client_id():
    """THE STRUCTURAL GUARD, checked against the REAL function.

    A future caller that forgets must get a `TypeError`, not a silent Anthropic
    default — a silent default is precisely what the module constants were.
    Asserted on the signature rather than by calling it, because calling it
    would make a network request on the very path this is guarding.
    """
    import inspect

    sig = inspect.signature(auth_probe._post_refresh)
    for name in ("endpoint", "client_id"):
        p = sig.parameters[name]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name} is not keyword-only, so it can be passed positionally by "
            f"accident")
        assert p.default is inspect.Parameter.empty, (
            f"{name} has a default ({p.default!r}); a default here is exactly "
            f"the module constant this fix removed")


def test_the_backup_is_not_world_readable(probe):
    """It holds a live refresh token. `write_text` took the umask and produced
    0644, while the sibling `rotation-test arm` gets 0600 from `shutil.copy2`.
    Measured: with the source at 0600 the backup was STILL 0644, so the mode
    was umask-derived, not inherited."""
    plant, run, _, root = probe
    plant("agent-claude", ".credentials.json",
          {"claudeAiOauth": {"refreshToken": "CLAUDE_RT"}})

    run("--agent", "agent-claude", "--yes")

    backups = sorted(root.glob("rotation-probe-backup-*.json"))
    assert backups, "no backup was written, so this test proves nothing"
    for b in backups:
        mode = b.stat().st_mode & 0o777
        assert mode == 0o600, (
            f"{b.name} is 0o{mode:o}; it holds a live refresh token and any "
            f"group- or world-readable bit discloses it")
