"""`config get` must not print a credential just because you asked for its parent.

THE DEFECT, measured through the real CLI before the fix:

    botainer config get env.ANTHROPIC_API_KEY  →  <redacted>
    botainer config get env                    →  ANTHROPIC_API_KEY: sk-ant-api03-…

`config_cmd.py`'s guard reads

    if not show_secrets and isinstance(value, str) and looks_credential(key)

Ask for a PARENT key and `value` is a dict: `isinstance(value, str)` is False,
`looks_credential("env")` is False, the guard is skipped entirely, and the
`yaml.safe_dump` below emits every child raw.

The leaf case was hardened by task #297 and the parent case was never
considered — the same shape as #297/#298, in the command
`botainer doctor`'s own advice points people at for inspecting their config.
Found by the loop tzar at checkpoint 6 while re-verifying something else.
"""
from __future__ import annotations

import yaml
from click.testing import CliRunner

from botainer.cli import config_cmd

_SECRET = "sk-ant-api03-REALLOOKINGSECRET0123456789abcdef"
# SHORT AND OBVIOUSLY FAKE, on purpose, and do not "improve" it into a
# realistic PAT. A 36-char `ghp_` string is the exact shape the shipped secret
# scanner hunts for, so a realistic one makes the tree fail its own export gate
# — which is what happened, and the gate caught it on staging rather than in a
# release. It also happens to be the stronger fixture: a short value leaves a
# length-threshold mutation nowhere to hide. `ghp_real` in test_broker_env_scrub
# is the same convention.
_GH = "ghp_fake"


def _project(tmp_path, monkeypatch, data):
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(yaml.safe_dump(data))
    (proj / ".botainer" / "project-id").write_text("test-uuid-row151\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.chdir(proj)
    return proj


def _get(args):
    # Click 8.4 removed `mix_stderr`; stderr is a separate stream by default
    # and `result.stderr` is the accessor. Pinned here because the
    # "it SAYS something was hidden" assertion depends on which stream the
    # note lands on.
    return CliRunner().invoke(
        config_cmd.config, ["get", *args], catch_exceptions=False)


def test_asking_for_the_PARENT_does_not_print_the_secret(tmp_path, monkeypatch):
    """THE DEFECT. `config get env` used to dump the key in full."""
    _project(tmp_path, monkeypatch,
             {"agent": "claude", "env": {"ANTHROPIC_API_KEY": _SECRET,
                                         "HARMLESS": "yes"}})

    result = _get(["env"])

    assert _SECRET not in result.output, (
        f"the secret was printed in full when its PARENT key was requested:\n"
        f"{result.output}")
    assert "HARMLESS" in result.output, (
        "redaction ate the non-credential values too; `config get` still has "
        "to show you your config")


def test_it_SAYS_that_something_was_hidden(tmp_path, monkeypatch):
    """Silent redaction swaps one wrong impression for another.

    A user reading `config get env` needs to know the value they cannot see
    EXISTS and is being withheld — otherwise the output reads as complete.
    """
    _project(tmp_path, monkeypatch,
             {"env": {"ANTHROPIC_API_KEY": _SECRET, "HARMLESS": "yes"}})

    result = _get(["env"])

    assert "redacted" in result.stderr, result.stderr
    assert "--show-secrets" in result.stderr, (
        f"it hid something and did not say how to see it: {result.stderr}")


def test_a_credential_nested_TWO_levels_down_is_caught(tmp_path, monkeypatch):
    """Depth is arbitrary — the user picks the parent, not the schema.

    `plugins.git.token` is judged by `token`, its OWN key name, exactly as a
    bare `token` would be. A fix that only handled one level would pass the
    test above and still leak here.
    """
    _project(tmp_path, monkeypatch,
             {"plugins": {"git": {"token": _GH, "mode": "readonly"}}})

    result = _get(["plugins"])

    assert _GH not in result.output, result.output
    assert "readonly" in result.output, "non-credential siblings must survive"


def test_a_credential_inside_a_LIST_is_caught(tmp_path, monkeypatch):
    """Lists are values too. A secret in a list element is still a secret."""
    _project(tmp_path, monkeypatch,
             {"env": {"tokens": [_SECRET, "harmless-value"]}})

    result = _get(["env"])

    assert _SECRET not in result.output, result.output


def test_the_LEAF_case_still_redacts(tmp_path, monkeypatch):
    """The half that already worked. Without this the fix could regress it."""
    _project(tmp_path, monkeypatch, {"env": {"ANTHROPIC_API_KEY": _SECRET}})

    result = _get(["env.ANTHROPIC_API_KEY"])

    assert _SECRET not in result.output, result.output


def test_show_secrets_still_shows_them(tmp_path, monkeypatch):
    """The escape hatch must keep working, or the fix has broken the command.

    Someone who passes `--show-secrets` has asked explicitly and is entitled to
    the value; redacting unconditionally would be a different defect.
    """
    _project(tmp_path, monkeypatch, {"env": {"ANTHROPIC_API_KEY": _SECRET}})

    result = _get(["env", "--show-secrets"])

    assert _SECRET in result.output, (
        "--show-secrets no longer shows the secret")


def test_a_config_with_NO_credentials_is_printed_unchanged(tmp_path, monkeypatch):
    """The control, and it is mutation-proven rather than merely asserted.

    If the walk redacted indiscriminately, or if the "something was hidden"
    note fired on every parent key, this fails. A note on output that hid
    nothing is the scenery problem that halted this loop once already.
    """
    _project(tmp_path, monkeypatch,
             {"env": {"EDITOR": "vim", "LANG": "en_GB.UTF-8"}})

    result = _get(["env"])

    assert "vim" in result.output and "en_GB.UTF-8" in result.output
    assert "redacted" not in result.output, result.output
    assert "redacted" not in result.stderr, (
        f"claimed to redact something in a config with no credentials: "
        f"{result.stderr}")


def test_the_stored_config_is_NOT_rewritten(tmp_path, monkeypatch):
    """`config get` displays; it must never mutate what it was asked to show.

    The walk builds a copy. If it edited in place and anything later wrote the
    config back, the redaction placeholder would replace the real credential on
    disk — turning a display bug into data loss.
    """
    proj = _project(tmp_path, monkeypatch, {"env": {"ANTHROPIC_API_KEY": _SECRET}})

    _get(["env"])

    on_disk = yaml.safe_load((proj / ".botainer" / "config.yaml").read_text())
    assert on_disk["env"]["ANTHROPIC_API_KEY"] == _SECRET, (
        "reading the config rewrote the credential on disk")


# ---------------------------------------------------------------------------
# The FIRST version of this fix shipped a regression, found by a reviewer told
# to refute it rather than review it. Everything below exists because of that.
# ---------------------------------------------------------------------------

def test_a_SHORT_credential_through_a_parent_is_still_fully_hidden(
        tmp_path, monkeypatch):
    """The fixtures above are 39-43 chars, which pins nothing on LENGTH.

    Two mutations recreated the original leak and passed all eight tests above:

        `… and looks_credential(key) and len(node) > 20`  → printed in full
        `mode="preview"` instead of `mode="safe"`         → `sk-a…cdef (46 chars)`

    The first is the verbatim defect this file was opened for; the second is the
    first-4/last-4 oracle that task #298 closed on the leaf path. A short value
    catches both, because there is nothing left to show.
    """
    short = "sk-ant-fake"
    assert len(short) < 20, "the fixture only works while it is SHORT"
    _project(tmp_path, monkeypatch, {"env": {"ANTHROPIC_API_KEY": short}})

    result = _get(["env"])

    assert short not in result.output, (
        f"a short credential survived the walk — either a length threshold or "
        f"a preview mode is in play:\n{result.output}")
    assert "<redacted>" in result.output, result.output
    assert "sk-a" not in result.output, (
        f"a first-4/last-4 preview is not redaction; #298 closed that oracle "
        f"on the leaf path and it must not reappear here:\n{result.output}")


def test_botainers_OWN_credential_SETTINGS_are_not_mistaken_for_secrets(
        tmp_path, monkeypatch):
    """THE REGRESSION the first version of this fix introduced.

    `looks_credential` answers a question about ENV VAR names, and matches the
    substring "credential". Two fields of botainer's own config schema contain
    that word while holding a SETTING:

        inject_credentials  — WHOSE credentials are bound into the cage
        credential_scope    — WHICH login store a broker opens

    On a config written by `botainer init --agent claude` these were the ONLY
    two keys the first version changed, and both were wrong: `config get
    plugins` answered `credential_scope: <redacted>`. Those are the exact two
    fields someone debugging a cross-project login problem has to read, and
    they were replaced with a placeholder that asserts they are secrets.

    `inject_credentials` is the sharper half — it is a LIST, so the pre-fix leaf
    guard never touched it. Walking lists is what introduced it.
    """
    _project(tmp_path, monkeypatch, {
        "inject_credentials": ["codex"],
        "plugins": {"agent-claude-broker": {"credential_scope": "shared"}},
    })

    injected = _get(["inject_credentials"])
    assert "codex" in injected.output, (
        f"`inject_credentials` is a list of AGENT NAMES, not a secret:\n"
        f"{injected.output}")
    assert "redacted" not in injected.output + injected.stderr, injected.output

    scope = _get(["plugins"])
    assert "shared" in scope.output, (
        f"`credential_scope` names a login store, not a secret:\n{scope.output}")
    assert "redacted" not in scope.output + scope.stderr, scope.output


def test_the_exemption_list_names_only_REAL_fields(tmp_path, monkeypatch):
    """An exemption list is a filter, and a filter rots into a permission slip.

    Each name here must be a field the product actually defines. If one is
    renamed or deleted, this fails and the exemption goes with it — otherwise
    the list quietly accumulates names that exempt a future credential field
    nobody re-examined.
    """
    import yaml as _yaml
    from pathlib import Path
    from botainer.core.config import ProjectConfig
    from botainer.inspect._redact import _CONFIG_SETTING_NAMES

    known = set(ProjectConfig.model_fields)
    root = Path(__file__).resolve().parents[2]
    for manifest in (root / "plugins").glob("*/botainer-plugin.yaml"):
        text = manifest.read_text()
        for name in _CONFIG_SETTING_NAMES:
            if name in text:
                known.add(name)

    assert _CONFIG_SETTING_NAMES, "an empty exemption list should be deleted"
    unknown = sorted(set(_CONFIG_SETTING_NAMES) - known)
    assert not unknown, (
        f"exempted from redaction but not a field of ProjectConfig or of any "
        f"shipped plugin manifest: {unknown}. Either the field was renamed — "
        f"in which case fix the name — or the exemption is unused and should "
        f"be removed.")


def test_a_YAML_BOOLEAN_key_does_not_crash_the_command(tmp_path, monkeypatch):
    """`ON:` is parsed by PyYAML as the boolean True, not the string "ON".

    The first version called `.lower()` on whatever key it was handed, so a
    config with a bare `ON:` or a numeric key died with a raw AttributeError
    traceback and exit 1 — where the code it replaced printed the value fine.
    Making output SAFER must not make it ABSENT.
    """
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "plugins:\n  web-ports:\n    ON: enabled\n    8080: forwarded\n")
    (proj / ".botainer" / "project-id").write_text("test-uuid-row151b\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.chdir(proj)

    result = _get(["plugins"])

    assert result.exit_code == 0, (
        f"non-string key crashed `config get`:\n{result.output}\n"
        f"{result.exception!r}")
    assert "enabled" in result.output and "forwarded" in result.output


def test_config_SET_does_not_print_the_credential_it_is_replacing(
        tmp_path, monkeypatch):
    """The sibling that mattered most, and the reason this file grew.

    `config get` redacted and `config set` did not, in the same module. Its
    diff printed `- 'sk-ant-…'` in full — and on a PARENT key, the entire old
    dict. That is the command someone runs TO ROTATE A LEAKED KEY: the leak was
    worst exactly where it mattered most.
    """
    old, new = "sk-ant-fake", "sk-ant-new9"
    _project(tmp_path, monkeypatch, {"env": {"ANTHROPIC_API_KEY": old}})

    result = CliRunner().invoke(
        config_cmd.config,
        ["set", "env.ANTHROPIC_API_KEY", new, "--yes"],
        catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert old not in result.output, (
        f"`config set` printed the credential it was replacing:\n"
        f"{result.output}")
    assert new not in result.output, (
        f"`config set` echoed the new credential back to the terminal:\n"
        f"{result.output}")
    assert "still applied" in result.output, (
        f"`- '<redacted>'` above `+ '<redacted>'` reads as 'nothing changed'. "
        f"It must say the write took, or the user re-runs the command looking "
        f"for a failure that did not happen:\n{result.output}")


def test_config_EXPLAIN_does_not_print_plugin_credentials(
        tmp_path, monkeypatch):
    """The third sibling, and the most confusing to read.

    `config explain` printed `token: ghp_…` in full while redacting the `env:`
    block twelve lines further down — one screen, two answers.
    """
    _project(tmp_path, monkeypatch, {
        "agent": "claude",
        "plugins": {"git": {"token": _GH, "mode": "readonly"}},
    })

    result = CliRunner().invoke(
        config_cmd.config, ["explain"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert _GH not in result.output, (
        f"`config explain` printed a plugin credential in full:\n"
        f"{result.output}")
    assert "readonly" in result.output, (
        "redaction ate the non-credential plugin settings")
