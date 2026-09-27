"""No bind may reach a credential the session did not select. (#221)

WHAT WENT WRONG. `composition.py` binds every plugin's data dir read-only at
`/workspace/.botainer/<plugin-name>/` when the plugin declares that prefix. For
the ISOLATED claude plugin the data dir is `data/agent-claude/` — and
`history_dir_for` strips the mode suffix, so `data/agent-claude/` is ALSO the
root under which EVERY auth profile keeps its credential:

    data/agent-claude/profiles/default/.credentials.json
    data/agent-claude/profiles/work/.credentials.json      <- a different account
    data/agent-claude/broker-state/<profile>/

Two namespaces — "this plugin's settings dir" and "this family's credential
store" — resolved to one path, so a session running profile `default` could read
profile `work`'s refresh token. Observed on the real rendered docker argv, after
hooks:

    source=.../data/agent-claude,target=/workspace/.botainer/agent-claude,readonly
    source=.../data/agent-claude/profiles/default,target=/home/agent/.claude

The second bind is correct and narrow. The first is the parent of all of them.

WHY IT IS WORTH A STRUCTURAL FIX RATHER THAN A CASE FIX. Nothing was special
about claude. The collision happens for ANY plugin whose name equals the bare
agent name and declares the prefix. `agent-codex` escapes today only because it
happens not to declare one; `agent-claude-shared` escapes because its data dir
(`data/agent-claude-shared`) is a different directory. Both are accidents of
naming, not decisions — so the invariant is asserted over the whole matrix here
rather than on the one plugin that happened to trip it.

Refresh tokens live for months (EF-1), so a read is not a small leak: the point
of separate auth profiles is separate accounts, and a prompt-injected agent in
one profile could take another's durable credential.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from botainer.core.composition import compose_session

# Filenames that ARE a credential, per history_carry.CREDENTIAL_FILENAMES.
CREDENTIAL_FILES = (".credentials.json", "auth.json", "api_key")


def _project(tmp_path, monkeypatch, agent: str, plugins: list[str],
             profile: str = "default") -> tuple[Path, Path]:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    uid = str(uuid.uuid4())
    (proj / ".botainer" / "project-id").write_text(uid)
    (proj / ".botainer" / "config.yaml").write_text(
        f"version: config-v1\nagent: {agent}\nruntime: docker\n"
        f"profile: {profile}\n"
        "image: ubuntu:24.04\nnetwork:\n  mode: internet\n"
        f"plugins_enabled: [{', '.join(plugins)}, git]\n")
    return proj, tmp_path / "bot" / "state" / uid / "data"


def _plant_two_profiles(data: Path, agent: str, credfile: str) -> Path:
    """A realistic store: the selected profile AND a second account.

    One profile is what every existing fixture had, and one profile cannot
    express "reachable across profiles" — the defect is invisible without a
    neighbour to reach.
    """
    for prof, token in (("default", "SELECTED"), ("work", "OTHER-ACCOUNT")):
        d = data / f"agent-{agent}" / "profiles" / prof
        d.mkdir(parents=True, exist_ok=True)
        (d / credfile).write_text('{"refreshToken": "%s"}' % token)
    return data / f"agent-{agent}" / "profiles" / "work" / credfile


def _reachable(spec, host_path: Path) -> list[tuple[str, str]]:
    """Binds whose SOURCE is `host_path` or an ancestor of it.

    Ancestor, not equality: the whole defect is that a bind of the PARENT makes
    every child readable. An equality check would have passed throughout.
    """
    hits = []
    for b in spec.mount_plan.binds:
        src = Path(str(b.source))
        if src == host_path or src in host_path.parents:
            hits.append((str(b.source), str(b.target)))
    return hits


@pytest.mark.parametrize("agent,plugin,credfile", [
    ("claude", "agent-claude", ".credentials.json"),
    ("claude", "agent-claude-shared", ".credentials.json"),
    ("codex", "agent-codex", "auth.json"),
    ("codex", "agent-codex-shared", "auth.json"),
])
def test_no_bind_reaches_an_unselected_profiles_credential(
        tmp_path, monkeypatch, agent, plugin, credfile) -> None:
    """The whole matrix, because which plugin leaks is an accident of naming."""
    proj, data = _project(tmp_path, monkeypatch, agent, [plugin])
    other = _plant_two_profiles(data, agent, credfile)
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    leaks = _reachable(spec, other)
    assert not leaks, (
        f"{plugin}: the session runs profile 'default' but these binds reach "
        f"profile 'work''s {credfile}:\n" +
        "\n".join(f"    source={s}\n    target={t}" for s, t in leaks))


def test_the_settings_bind_still_EXISTS_and_points_inside_the_plugin_dir(
        tmp_path, monkeypatch) -> None:
    """Guard against 'fix the leak by deleting the mechanism'.

    THIS GUARD WAS WRONG THE FIRST TIME, in a way worth recording. It asserted
    that the SELECTED profile's credential was still reachable — and it passed
    before the fix and failed after it. Not because the fix broke anything: the
    selected profile is bound by the plugin's pre_session HOOK
    (data/agent-claude/profiles/<profile> -> /home/agent/.claude), which
    compose_session does not run. So at compose time the selected credential was
    only ever "reachable" THROUGH THE LEAK ITSELF. The guard was measuring the
    bug and calling it the feature.

    What compose is actually responsible for is the settings bind. So that is
    what this asserts: it still exists at the declared target, and its source is
    inside the plugin's own data dir — deleting the bind, or repointing it
    somewhere unrelated, both fail here.
    """
    proj, data = _project(tmp_path, monkeypatch, "claude", ["agent-claude"])
    _plant_two_profiles(data, "claude", ".credentials.json")
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    settings = [b for b in spec.mount_plan.binds
                if str(b.target) == "/workspace/.botainer/agent-claude"]
    assert len(settings) == 1, (
        "the plugin settings bind is gone; the mechanism was deleted rather "
        f"than narrowed. binds={[str(b.target) for b in spec.mount_plan.binds]}")
    src = Path(str(settings[0].source))
    assert data / "agent-claude" in src.parents, (
        f"settings bind source {src} is not inside the plugin's data dir")
    assert src.name == "plugin-settings", (
        f"expected the dedicated subdirectory, got {src.name!r}")
