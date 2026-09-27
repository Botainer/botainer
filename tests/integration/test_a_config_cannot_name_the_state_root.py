"""A project's own config may not name the botainer state root as a bind source.

THE REACH. `/workspace` is read-write in the cage, so a caged agent can write
`mounts.extra` into the project's `config.yaml`. Before this, a source of
`$MY_BOTAINER` composed with no refusal — measured, `inspect --json` showed
`<state root> -> /data rw`. What that hands over is not a secret at the edges:

  * `state/<uuid>/` for EVERY OTHER PROJECT on the host, including their
    credentials;
  * `shared-auth/agent-*/` — the host-wide login;
  * `plugins/*/hooks/*.py`, which botainer executes ON THE HOST as the user at
    the next `start` in ANY project.

The last one is host code execution, not information disclosure, and it
persists after the session that planted it has ended.

WHY THE DENYLIST COULD NOT CARRY THIS. `DENYLISTED_SOURCES` is checked for every
bind, and botainer's own binds legitimately live inside the state root —
measured on a default session: nine binds, eight of them inside, and every one
of those eight is `core` or `plugin` provenance (`/packages`, `/scratch`,
`/home/user`, the `.botainer` subtree, the per-plugin data dir). Adding the root
to that tuple would refuse all of them and break every launch. The literal is
also wrong on HPC, where `MY_BOTAINER` is routinely moved to `$SCRATCH` and
`~/.botainer` is not the root at all.

SO THE RULE IS WHO ASKED, NOT WHICH PATH. `Provenance.USER` is exactly "the
project's config.yaml named this", which is exactly the attacker-reachable
channel. Core and plugin binds are untouched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A real install: `setup` then `init`, not a hand-built fixture.

    The check resolves the state root through the same code the product does,
    so a fixture that faked the root would be testing a different function.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0
    assert r.invoke(cli, ["init"]).exit_code == 0
    return r, proj, Path(os.environ["MY_BOTAINER"])


def _with_extra(proj: Path, source: str) -> None:
    cfg = proj / ".botainer" / "config.yaml"
    d = yaml.safe_load(cfg.read_text())
    d.setdefault("mounts", {})["extra"] = [
        {"source": source, "target": "/data", "mode": "rw"}]
    cfg.write_text(yaml.safe_dump(d, sort_keys=False))


@pytest.mark.parametrize("which,why", [
    ("root", "the state root itself"),
    ("state", "INSIDE it — every other project's credentials"),
    ("plugins", "the hook scripts botainer runs on the HOST as the user"),
    ("parent", "an ANCESTOR of it — the umbrella direction"),
])
def test_a_config_supplied_source_reaching_the_state_root_is_refused(
        project, which, why):
    r, proj, root = project
    source = {"root": root, "state": root / "state",
              "plugins": root / "plugins", "parent": root.parent}[which]
    _with_extra(proj, str(source))

    res = r.invoke(cli, ["inspect", "--json"])

    assert res.exit_code != 0, (
        f"a project config named {why} as a bind source and it composed:\n"
        f"{res.output[:600]}")
    assert "state root" in res.output, (
        f"refused, but not for this reason — the message must name what is at "
        f"stake or a user cannot act on it:\n{res.output[:400]}")


def test_a_NORMAL_session_still_composes(project):
    """THE OPPOSITE DIRECTION, and the reason this is not a denylist entry.

    Eight of a default session's nine binds have sources INSIDE the state root.
    A rule keyed on the path rather than on who asked would refuse all of them.
    Without this test, "refuse everything touching the root" passes the four
    above and breaks the product.
    """
    r, proj, root = project

    res = r.invoke(cli, ["inspect", "--json"])

    assert res.exit_code == 0, f"a plain session no longer composes:\n{res.output[:600]}"
    binds = json.loads(res.output).get("mount_plan", {}).get("binds", [])
    inside = [b for b in binds
              if b["source"] == str(root) or b["source"].startswith(str(root) + "/")]
    assert inside, "fixture is wrong: no bind is inside the state root at all"
    assert all(b["provenance"] in ("core", "plugin") for b in inside), (
        f"a non-core bind lives inside the state root, so the provenance rule "
        f"is the wrong discriminator: "
        f"{[(b['provenance'], b['target']) for b in inside]}")


def test_an_ORDINARY_extra_mount_is_still_allowed(project, tmp_path):
    """The feature still works. A rule that refused every `mounts.extra`
    would pass every test above and remove a documented capability."""
    r, proj, root = project
    data = tmp_path / "my-data"
    data.mkdir()
    _with_extra(proj, str(data))

    res = r.invoke(cli, ["inspect", "--json"])

    assert res.exit_code == 0, (
        f"an ordinary directory was refused as mounts.extra — the guard is "
        f"too broad:\n{res.output[:600]}")
    binds = json.loads(res.output).get("mount_plan", {}).get("binds", [])
    assert any(b["source"] == str(data) for b in binds), (
        "the allowed extra mount did not reach the plan")


def test_a_SYMLINK_to_the_state_root_is_refused(project, tmp_path):
    """The bypass the literal-path check cannot see, and the agent can build it.

    `/workspace` is rw, so a caged agent can create a symlink there pointing at
    the state root and name the LINK in `mounts.extra`. The literal source is
    an innocent path inside the project; the kernel binds the target.

    This survived the first version of these tests — dropping `realpath` from
    the candidate set kept all six green — which is why it is here as its own
    case rather than trusted to the implementation comment.
    """
    r, proj, root = project
    link = proj / "innocent-looking-dir"
    link.symlink_to(root)
    _with_extra(proj, str(link))

    res = r.invoke(cli, ["inspect", "--json"])

    assert res.exit_code != 0, (
        f"a symlink whose target IS the state root composed as an ordinary "
        f"bind source:\n{res.output[:600]}")
    assert "state root" in res.output, res.output[:400]


def test_it_FAILS_CLOSED_when_the_state_root_cannot_be_resolved(monkeypatch):
    """A guard that cannot locate what it protects must not answer "allowed".

    That is the exact shape this project keeps finding in its own code — a
    swallowed lookup becoming a plausible-looking permission — so the
    unresolvable case refuses and says why, rather than skipping the check.

    DRIVEN AT THE VALIDATOR, NOT THE CLI, AND THAT IS NOT A SHORTCUT. I tried
    the CLI first: `inspect` resolves the state root itself, well before
    composing a mount plan, so making the resolver raise kills the command
    before this branch is reached — the runner returns an uncaught OSError with
    empty output. So the branch is DEFENSIVE and not reachable through today's
    CLI. It is still worth having and worth pinning: `validate()` is called from
    the child-job composer as well, and "unreachable today" is exactly the
    property that rots silently.
    """
    from botainer.core.refusal import Refused
    from botainer.core.spec import Bind, BindMode, MountPlan, Provenance
    from botainer.core.policy import SitePolicy
    from botainer.mount_plan import validation

    def _boom(*a, **k):
        raise OSError("state root unreadable")

    monkeypatch.setattr("botainer.state.dir.ensure_user_state_dir", _boom)
    plan = MountPlan(binds=[Bind(source="/tmp/anything", target="/data",
                                 mode=BindMode.RW,
                                 provenance=Provenance.USER)])

    with pytest.raises(Refused) as exc:
        validation.validate(plan, policy=SitePolicy())

    assert "cannot determine the botainer state root" in str(exc.value), (
        f"refused, but without saying the check could not run — which is the "
        f"difference between a measurement and a guess: {exc.value}")


def test_a_SYMLINKED_HOME_does_not_defeat_the_rule(tmp_path, monkeypatch):
    """THE CRITICAL a refuting review found, and NO attacker is involved.

    `ensure_user_state_dir` resolves symlinks on its `MY_BOTAINER` branch and
    returns `Path.home() / ".botainer"` VERBATIM on its default one. The first
    version of this rule resolved the candidate source but compared it against
    that literal root — so on a default install whose `$HOME` traverses a
    symlink, the two were different strings and the comparison matched nothing.

    That is not an exotic setup. `/home/<netid>` → `/gpfs/...` is the ordinary
    cluster layout, which means the guard did nothing on exactly the platform
    this product targets. Both sides are resolved now.

    Driven at the validator: the CLI would need `HOME` swapped before import,
    and what is under test is the containment comparison, not argument parsing.
    """
    from botainer.core.policy import SitePolicy
    from botainer.core.refusal import Refused
    from botainer.core.spec import Bind, BindMode, MountPlan, Provenance
    from botainer.mount_plan import validation

    realhome = tmp_path / "realhome"
    (realhome / ".botainer" / "state").mkdir(parents=True)
    home = tmp_path / "home"
    home.symlink_to(realhome)                       # the site's layout
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MY_BOTAINER", raising=False)
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    # The PHYSICAL path — what a user (or an agent reading /proc/mounts) sees.
    plan = MountPlan(binds=[Bind(source=str(realhome / ".botainer" / "state"),
                                 target="/data", mode=BindMode.RW,
                                 provenance=Provenance.USER)])

    with pytest.raises(Refused) as exc:
        validation.validate(plan, policy=SitePolicy())
    assert "state root" in str(exc.value), exc.value


def test_a_READ_ONLY_bind_of_the_state_root_is_refused_too(project):
    """`ro` is still every credential on the host, readable.

    A refuting review found that narrowing the rule to `mode == RW` survived
    every other test here, because all of them used `rw`. Reading the store is
    the disclosure half of the same defect — the hooks cannot be rewritten, but
    every project's token can be read out.
    """
    r, proj, root = project
    cfg = proj / ".botainer" / "config.yaml"
    d = yaml.safe_load(cfg.read_text())
    d.setdefault("mounts", {})["extra"] = [
        {"source": str(root), "target": "/data", "mode": "ro"}]
    cfg.write_text(yaml.safe_dump(d, sort_keys=False))

    res = r.invoke(cli, ["inspect", "--json"])

    assert res.exit_code != 0, (
        f"a READ-ONLY bind of the state root composed — every project's "
        f"credential is readable from the container:\n{res.output[:600]}")
    assert "state root" in res.output, res.output[:400]
