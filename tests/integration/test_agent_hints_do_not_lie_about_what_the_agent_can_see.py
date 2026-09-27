"""What botainer TELLS the agent about its own reach must be true.

THE FALSE SENTENCE. `AGENT_HINTS.md` told every agent, in every session:

    "Files outside `/workspace` are not visible to you."

Measured on a plain default project, the agent also reaches `/packages`,
`/scratch` and `/home/user` — and plugins add more. The claim was false on the
very first session anyone runs.

WHY THIS DIRECTION OF ERROR IS THE BAD ONE. AGENT_HINTS is injected into the
agent's system prompt: it is the agent's picture of its own cage. An agent told
it cannot see something it CAN see will say so to the user — confidently, and
about privacy. "I can't see anything outside the project" is exactly the
sentence a user would rely on before pasting something sensitive into a
neighbouring directory.

THE OTHER HALF, WHICH NO AGENT-FACING SURFACE MENTIONED AT ALL. `/workspace/
.botainer` is masked with a null bind, hiding the host's copy of the project's
config and id. Nothing told the agent that was deliberate, so the honest
readings available to it were "empty" or "broken".

AND THE FIRST FIX OVERSTATED IT. I wrote that the directory "appears empty",
twelve lines after listing three readable paths inside it — three targets are
mounted back ON TOP of the mask. A reader who believes "empty" will not look,
and what is there is their access summary, these hints, and the agent's own
data dir.

DERIVED, NOT ASSERTED. The list is built from `spec.mount_plan.binds` — the same
plan the adapter renders into argv. A hardcoded sentence was wrong the moment a
plugin contributed a bind; this cannot drift from the session it describes,
which is the point.

AND THE REPLACEMENT DOES NOT OVERCORRECT. "Nothing else is visible" would be the
same error pointing the other way: everything from the IMAGE — `/usr`, `/bin`,
the agent's own install — is visible and has nothing to do with the user's
machine. The claim is scoped to host paths, and that scoping is tested.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli
from botainer.core import composition
from botainer.core.spec import BindMode
from botainer.inspect import agent_hints


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """A real `setup` + `init`, composed and rendered like a real session."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0
    assert r.invoke(cli, ["init"]).exit_code == 0

    def _render(extra_config: dict | None = None):
        if extra_config:
            cfg = proj / ".botainer" / "config.yaml"
            d = yaml.safe_load(cfg.read_text())
            d.update(extra_config)
            cfg.write_text(yaml.safe_dump(d, sort_keys=False))
        spec = composition.compose_session(
            proj, runtime_choice="mock", identity_accept=True)
        return agent_hints.render(spec), spec

    return _render


def test_it_does_not_claim_the_agent_cannot_see_outside_the_workspace(rendered):
    """The literal false sentence, gone."""
    text, _spec = rendered()

    assert "outside `/workspace` are not visible" not in text, (
        "AGENT_HINTS still tells the agent it cannot see outside /workspace, "
        "which is false on every default session")


def test_every_bound_host_path_is_NAMED_to_the_agent(rendered):
    """Not "some paths are bound" — each one, with its mode.

    Derived from the plan, so this fails if a bind is ever added without the
    agent being told. That is the property; a hardcoded list cannot have it.
    """
    text, spec = rendered()

    reachable = [b for b in spec.mount_plan.binds
                 if b.mode in (BindMode.RO, BindMode.RW)]
    assert reachable, "fixture composed no readable binds at all"
    missing = [b.target for b in reachable
               if f"`{b.target.rstrip('/')}`" not in text]
    assert not missing, (
        f"the agent is not told about host paths it can reach: {missing}\n"
        f"It will describe its own isolation wrongly to the user.")


def test_a_read_only_bind_is_not_described_as_writable(rendered):
    """Mode matters: an agent that thinks a read-only path is writable will
    plan work that fails, and tell the user it saved something it did not."""
    text, spec = rendered()

    ro = [b for b in spec.mount_plan.binds if b.mode == BindMode.RO]
    if not ro:
        pytest.skip("this default session composes no read-only bind")
    for b in ro:
        line = next((ln for ln in text.splitlines()
                     if ln.startswith(f"- `{b.target.rstrip('/')}`")), None)
        assert line is not None, f"{b.target} is not listed at all"
        assert "read-only" in line, (
            f"{b.target} is bound read-only but the agent is told "
            f"{line!r}")


def test_the_MASKED_path_is_disclosed_as_deliberate(rendered):
    """The one bind that genuinely hides something, which no agent-facing
    surface mentioned. Without this the agent sees an empty directory and
    cannot tell "hidden on purpose" from "broken"."""
    text, spec = rendered()

    masked = [b.target.rstrip("/") for b in spec.mount_plan.binds
              if b.mode == BindMode.NULL_BIND]
    assert masked, "fixture composed no null bind, so this test proves nothing"
    for t in masked:
        assert f"`{t}`" in text, f"the masked path {t} is never mentioned"
    assert "BLANKS OUT" in text, (
        "the masked path is listed but not explained, so an agent reading it "
        "learns a path and not what is true about it")


def test_it_does_NOT_claim_the_image_is_part_of_the_users_machine(rendered):
    """THE OVERCORRECTION, and it would be the same defect reversed.

    `/usr` and `/bin` are visible and come from the image. A blanket "nothing
    else is present here" would be false in the opposite direction, so the
    claim is scoped to the user's machine and says where the rest comes from.
    """
    text, _spec = rendered()

    assert "comes" in text and "container image" in text, (
        f"the hints do not explain that everything outside the bind list comes "
        f"from the image, so a scoped claim reads as an absolute one:\n{text}")
    assert "No other part of the user's machine" in text, (
        "the 'nothing else' claim is not scoped to the host, so it asserts "
        "something false about /usr and /bin")


def test_the_list_tracks_the_SESSION_not_a_hardcoded_default(rendered, tmp_path):
    """The property that makes this derived rather than written down.

    Add a bind via the project's own config and it must appear. A hardcoded
    paragraph passes every test above and fails this one.
    """
    extra = tmp_path / "extra-data"
    extra.mkdir()
    text, _spec = rendered({"mounts": {"extra": [
        {"source": str(extra), "target": "/data", "mode": "ro"}]}})

    assert "`/data`" in text, (
        f"a bind this session actually has is absent from what the agent is "
        f"told, so the text is a fixed paragraph rather than a description of "
        f"this cage:\n{text}")
    assert "read-only" in text.split("`/data`")[1].split("\n")[0], (
        "the added bind is listed without its mode")


def test_it_does_not_call_a_masked_path_EMPTY_while_listing_files_in_it(rendered):
    """The contradiction a refuting review found, twelve lines apart.

    The first version said the masked path would "appear empty even though the
    host has files there" — while the list above it named three readable paths
    UNDER that same directory. Both halves were in the same rendered file. The
    mask hides the host's copy; three targets are then mounted back on top of
    it, which §1 of the capability contract already describes.

    A reader who believes "empty" will not look, and the three things they need
    most — their access summary, these hints, and the agent's own data dir —
    are exactly what is there.
    """
    text, spec = rendered()

    masked = [b.target.rstrip("/") for b in spec.mount_plan.binds
              if b.mode == BindMode.NULL_BIND]
    assert masked, "fixture composed no null bind, so this proves nothing"

    nested = [b.target.rstrip("/") for b in spec.mount_plan.binds
              if b.mode in (BindMode.RO, BindMode.RW)
              and any(b.target.rstrip("/").startswith(m + "/") for m in masked)]
    if not nested:
        pytest.skip("no readable path is mounted inside a masked one here")

    assert "appear" not in text or "empty even though" not in text, (
        f"the hints claim the masked path appears empty while listing "
        f"{nested} inside it:\n{text}")
    assert "not empty" in text, (
        f"paths are mounted back on top of the mask and the agent is not told, "
        f"so the honest readings left to it are 'empty' or 'broken': {nested}")
