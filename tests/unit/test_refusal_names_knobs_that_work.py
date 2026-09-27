"""A refusal must name a remedy that DOES the thing. (#232)

WHAT WAS WRONG. Setting `resources.cpu` or `resources.memory_mb` hard-refuses
every apptainer launch, and the refusal said:

    "For HPC, use the hpc-launcher plugin which sets SLURM --cpus / --mem at
     sbatch time. Clear resources to launch direct."

Nothing on the HPC path reads those two fields. Their only readers are the
DOCKER adapter (which honours them via `--cpus` / `--memory`), two display
surfaces, a composition passthrough, and this refusal itself. `hpc submit`
builds its sbatch request from the hpc-launcher plugin's OWN settings —
`plugins.hpc-launcher.cpus` / `.memory_gb` — plus `--cpus` / `--memory-gb`
flags, and never consults `resources.*`.

So a user who followed the remedy exactly — enable hpc-launcher — was refused
again, by the message that had just told them to do that. This is the #130 class
(shipped output naming something that does not do what it says), and the
resources fields are declared-not-read on the HPC path.

WHY THE FIX IS NOT "MAKE hpc-launcher READ resources.cpu". That would make the
old sentence true, and would also create TWO config sources for one number with
a precedence rule to get wrong. `plugins.hpc-launcher.cpus` already exists, is
read, and is what `botainer init` writes. Two readers of one field updated at
different times is the shape that produced #220. Say the truth instead.

WHY THIS TEST PARSES THE SCHEMA INSTEAD OF MATCHING A STRING. Asserting the new
wording appears would pin the wording and prove nothing about whether it WORKS —
the old message would have passed such a test just as happily. So every
`plugins.hpc-launcher.<key>` the refusal names is looked up in the plugin's
declared settings schema. A future edit that invents a plausible-sounding knob
fails here, which is the defect this item is about.
"""
from __future__ import annotations

import pathlib
import re
import textwrap

import pytest
import yaml

from botainer.adapters.apptainer import ApptainerAdapter
from botainer.core.refusal import Refused
from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    ResourceSpec,
    SessionSpec,
)

# Anchored to the repo, not to cwd. The cwd-relative form failed outright
# when pytest was invoked from anywhere else, which breaks the intent that
# these run inside the EXPORTED tree as well as here.
PLUGIN_YAML = (pathlib.Path(__file__).resolve().parents[2]
               / "plugins" / "hpc-launcher" / "botainer-plugin.yaml")

# A refusal may name a setting in EITHER form, and both are legitimate:
#
#   dotted      "set plugins.hpc-launcher.cpus"      — matches `config set`
#   YAML block  "plugins:\n  hpc-launcher:\n    cpus: 4"
#                                                    — pasteable into config.yaml
#
# Matching only the dotted form would have failed this refusal for using the
# friendlier one, which is a test pinning a house style rather than the claim.
# What is being checked is "the key it names is real", so both are extracted.
_KNOB_DOTTED = re.compile(r"plugins\.hpc-launcher\.([A-Za-z_][A-Za-z0-9_]*)")


def _block_after_hpc_launcher(msg: str) -> list[str]:
    """Every line belonging to an `hpc-launcher:` block, by INDENTATION.

    Deliberately not a regex over key-shaped lines. The first version of this
    was `(?:[ \\t]+[A-Za-z_][A-Za-z0-9_]*:.*\\n?)+` — it used the same pattern
    to find the block AND to decide what was in it, so a line it could not
    tokenize simply ended the capture and vanished. Feeding that to a YAML
    parser changes nothing: the bad lines were already gone before the parser
    saw them. The hole has to close where the block is DELIMITED.

    Indentation is the actual delimiter, so use it: take everything more
    indented than the `hpc-launcher:` line, blank lines included, whatever it
    looks like.
    """
    out: list[str] = []
    lines = msg.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "hpc-launcher:":
            continue
        indent = len(line) - len(line.lstrip())
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue                       # blank: inside, contributes nothing
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break                          # dedented: block over
            out.append(nxt)
    return out


def _keys_named_in(msg: str) -> set[str]:
    """Every key the message tells the user to set, including malformed ones.

    THE FIRST VERSION OF THIS WAS VACUOUS and a refutation pass broke it with
    four different wrong messages. It walked the block line-by-line with the
    same key regex used to FIND the block, so any line the regex could not
    tokenize was silently DROPPED from the result rather than reported as
    bogus. One good key satisfied the `assert named` guard and the bad ones
    became invisible.

    That is not theoretical. The likeliest wrong spelling is `memory-gb`,
    because the CLI flag is `--memory-gb` — and with `additionalProperties:
    false` a hyphenated key does not merely get ignored, it makes the whole
    config fail to load. The one form the test could not see was the one most
    likely to be written.

    So the block is parsed as YAML and every key under `hpc-launcher:` is
    returned verbatim, whatever it looks like. A checker that drops what it
    cannot parse reports success for the inputs it understands least.
    """
    named = set(_KNOB_DOTTED.findall(msg))
    block = _block_after_hpc_launcher(msg)
    if block:
        text = textwrap.dedent("\n".join(block))
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:                     # unparseable → VISIBLE
            named.add("<unparseable-yaml-block>")
        else:
            if isinstance(parsed, dict):
                named.update(str(k) for k in parsed)
            else:                                  # not a mapping → VISIBLE
                named.add(f"<non-mapping-block:{type(parsed).__name__}>")
    return named


def _declared_settings_keys() -> set[str]:
    """The plugin's OWN declaration of what `plugins.hpc-launcher.*` accepts.

    Read from `config_schema.properties` — the real path. An earlier version of
    this helper guessed at `settings_schema`, found nothing, and returned an
    empty set, which made the assertion below fail with a true-sounding message
    for an entirely false reason. Hence the emptiness guard: a lookup that finds
    nothing must SAY so rather than silently reporting every key as bogus.
    """
    doc = yaml.safe_load(open(PLUGIN_YAML))
    props = (doc.get("config_schema") or {}).get("properties") or {}
    assert props, (
        f"{PLUGIN_YAML} has no config_schema.properties — this helper is "
        "looking in the wrong place, and without this guard every key would "
        "read as undeclared")
    return set(props)


def _spec_with_resources(**res) -> SessionSpec:
    plan = MountPlan(binds=(
        Bind(source="/host/proj", target="/workspace", mode=BindMode.RW,
             provenance=Provenance.CORE),
    ))
    return SessionSpec(
        session_id="abcdef0123456789",
        project_uuid="11111111-1111-1111-1111-111111111111",
        project_root="/host/proj",
        state_dir="/state/dir",
        runtime="apptainer",
        image="/scratch/images/agent-claude.sif",
        mount_plan=plan,
        network=NetworkSpec(mode=NetworkMode.INTERNET),
        resources=ResourceSpec(**res),
    )


@pytest.mark.parametrize("res", [{"cpu": 4}, {"memory_mb": 16000},
                                 {"cpu": 4, "memory_mb": 16000}])
def test_the_refusal_still_fires(res) -> None:
    """The gap is real on apptainer; the fix is the WORDING, not the refusal.

    Guards against "fix the confusing message by deleting the check", which
    would silently drop limits the user asked for.
    """
    with pytest.raises(Refused):
        ApptainerAdapter().render_argv(_spec_with_resources(**res))


def test_every_config_key_the_refusal_NAMES_actually_exists() -> None:
    """The point of the item: a remedy that does not do the thing is a defect."""
    with pytest.raises(Refused) as exc:
        ApptainerAdapter().render_argv(_spec_with_resources(cpu=4))
    msg = str(exc.value)

    named = _keys_named_in(msg)
    assert named, (
        "the refusal names no `plugins.hpc-launcher.<key>` at all, so it still "
        f"does not tell the user where to set this. message: {msg!r}")

    declared = _declared_settings_keys()
    bogus = named - declared
    assert not bogus, (
        f"the refusal tells the user to set {sorted(bogus)}, which {PLUGIN_YAML} "
        f"does not declare. Declared: {sorted(declared)}. This is the defect the "
        "item is about, one edit later.")


def test_the_refusal_no_longer_offers_the_remedy_that_does_NOTHING() -> None:
    """Enabling the plugin does not make `resources.*` read. It never did.

    Asserted on the specific false claim rather than on the new prose, so
    rewording stays free and reintroducing the falsehood does not.
    """
    with pytest.raises(Refused) as exc:
        ApptainerAdapter().render_argv(_spec_with_resources(cpu=4))
    msg = str(exc.value).lower()

    assert not ("use the hpc-launcher plugin which sets" in msg), (
        "the refusal still says enabling hpc-launcher makes these fields take "
        f"effect; it does not read them: {msg!r}")


def test_the_refusal_says_these_fields_are_DOCKER_ONLY() -> None:
    """The user's real question is "why is this here at all, then?".

    `resources.cpu` is honoured — by the docker adapter. Saying so is the
    difference between a refusal that explains and one that just blocks.
    """
    with pytest.raises(Refused) as exc:
        ApptainerAdapter().render_argv(_spec_with_resources(cpu=4))
    msg = str(exc.value).lower()
    assert "docker" in msg, (
        f"the refusal never says where these fields DO work: {msg!r}")


def test_docker_still_honours_the_same_fields() -> None:
    """The claim "DOCKER-ONLY" must itself be true, not just asserted.

    If docker ever stopped rendering them, the refusal would be telling the
    user the fields work somewhere they do not — the same defect pointed the
    other way.
    """
    from botainer.adapters.docker import DockerAdapter
    spec = _spec_with_resources(cpu=4, memory_mb=16000)
    argv = DockerAdapter().render_argv(spec.model_copy(update={
        "runtime": "docker",
        "image": "botainer/agent-claude:0.1@sha256:" + "a" * 64,
    }))
    flat = " ".join(str(a) for a in argv)
    assert "--cpus" in flat and "4" in flat, f"docker dropped cpu: {flat!r}"
    assert "16000m" in flat, f"docker dropped memory_mb: {flat!r}"


# ── `config explain`: say it where the field is SET, not only where it dies ──
#
# The old refusal fired at LAUNCH. By then the user has already written
# `resources.cpu: 4` into config.yaml and believed it meant something. `config
# explain` is advertised as the summary of the effective config and printed the
# value with no hint it would be ignored — so the first news of the problem was
# a refused launch, possibly weeks later on a cluster.

def _explain(tmp_path, monkeypatch, body: str) -> str:
    from click.testing import CliRunner
    from botainer.cli.config_cmd import config
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n")
    (proj / ".botainer" / "config.yaml").write_text(body)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    result = CliRunner().invoke(config, ["explain"])
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output


def test_config_explain_marks_the_fields_dead_on_an_apptainer_project(
        tmp_path, monkeypatch) -> None:
    out = _explain(tmp_path, monkeypatch,
                   "agent: claude\nruntime: apptainer\n"
                   "network:\n  mode: internet\n"
                   "resources:\n  cpu: 4\n  memory_mb: 16000\n")
    assert "docker-only" in out.lower(), (
        "`config explain` printed resources.cpu on an APPTAINER project with no "
        f"sign it is ignored and will refuse the launch:\n{out}")


def test_config_explain_does_NOT_cry_wolf_on_a_docker_project(
        tmp_path, monkeypatch) -> None:
    """The marker must discriminate, or it is noise on every project.

    A warning that fires when nothing is wrong is the shape this project calls a
    bug or a lie — it trains the reader to skip the line that matters.
    """
    out = _explain(tmp_path, monkeypatch,
                   "agent: claude\nruntime: docker\n"
                   "network:\n  mode: internet\n"
                   "resources:\n  cpu: 4\n  memory_mb: 16000\n")
    assert "docker-only" not in out.lower(), (
        f"marked the fields dead on a DOCKER project, where they work:\n{out}")


def test_config_explain_is_silent_when_no_resources_are_set(
        tmp_path, monkeypatch) -> None:
    """Apptainer alone is not a problem — only apptainer PLUS a set field."""
    out = _explain(tmp_path, monkeypatch,
                   "agent: claude\nruntime: apptainer\n"
                   "network:\n  mode: internet\n")
    assert "docker-only" not in out.lower(), (
        f"warned about resources the user never set:\n{out}")


# ── The extractor's own regression test ─────────────────────────────────────
#
# A checker that silently drops what it cannot parse reports success for the
# inputs it understands least, and this one did. These are the exact wrong
# messages a refutation pass used to slip past it. They belong here rather than
# in a comment: the vacuity was invisible precisely because nothing exercised
# the checker with input it was meant to reject.

@pytest.mark.parametrize("label,block", [
    # The likeliest real mistake: the CLI flag is spelled `--memory-gb`, so
    # `memory-gb:` is what a future editor writes. `additionalProperties: false`
    # means it does not get ignored — the whole config fails to load.
    ("hyphenated, matching the CLI flag",
     "plugins:\n  hpc-launcher:\n    cpus: 4\n    memory-gb: 16\n"),
    ("quoted key",
     "plugins:\n  hpc-launcher:\n    cpus: 4\n    \"memlimit\": 3\n"),
    ("key after a blank line",
     "plugins:\n  hpc-launcher:\n    cpus: 4\n\n    totally_fake: 1\n"),
])
def test_the_extractor_SEES_keys_it_cannot_tokenize(label, block) -> None:
    named = _keys_named_in(block)
    bogus = named - _declared_settings_keys()
    assert bogus, (
        f"{label}: the extractor reported {sorted(named)} and found nothing "
        "wrong. A bogus key that the extractor cannot see is a bogus key the "
        "test above cannot catch — which is how this check was vacuous.")


def test_the_extractor_does_not_cry_wolf_on_a_correct_block() -> None:
    """The counterpart: it must not flag the real message.

    Without this, "flag everything" would satisfy the test above.
    """
    named = _keys_named_in(
        "plugins:\n  hpc-launcher:\n    cpus: 4\n    memory_gb: 16\n")
    assert named == {"cpus", "memory_gb"}, named
    assert not (named - _declared_settings_keys())


def test_config_explain_warns_when_runtime_is_AUTO(tmp_path, monkeypatch) -> None:
    """The default path, and the case the first version of this marker missed.

    `ProjectConfig.runtime` defaults to "auto" and `botainer init` WRITES
    "auto". A marker keyed on `runtime == "apptainer"` was therefore silent for
    the config the product itself generates — the one case it exists for. On a
    cluster login node `auto` resolves to apptainer, so this is not a corner.
    """
    out = _explain(tmp_path, monkeypatch,
                   "agent: claude\nruntime: auto\n"
                   "network:\n  mode: internet\n"
                   "resources:\n  cpu: 4\n")
    assert "docker-only" in out.lower(), (
        f"runtime 'auto' got no warning; on a cluster it resolves to apptainer, "
        f"where these are not read and `start` refuses:\n{out}")


def test_config_explain_warns_when_runtime_is_OMITTED(tmp_path, monkeypatch) -> None:
    """Same case, reached by leaving the field out rather than writing 'auto'."""
    out = _explain(tmp_path, monkeypatch,
                   "agent: claude\nnetwork:\n  mode: internet\n"
                   "resources:\n  memory_mb: 16000\n")
    assert "docker-only" in out.lower(), (
        f"omitted runtime (defaults to 'auto') got no warning:\n{out}")


def test_config_explain_does_not_imply_TIME_is_live(tmp_path, monkeypatch) -> None:
    """A distinction that does not exist is worse than uniform silence.

    Marking cpu/memory dead while `Time:` printed bare directly beneath told the
    reader Time was the one that works. It is not: `resources.time_minutes` is
    read by no runtime and no submit path — the sbatch `--time` comes from
    `plugins.hpc-launcher.time_minutes`. This defect was CREATED by the fix.
    """
    out = _explain(tmp_path, monkeypatch,
                   "agent: claude\nruntime: apptainer\n"
                   "network:\n  mode: internet\n"
                   "resources:\n  cpu: 4\n  time_minutes: 240\n")
    time_line = [ln for ln in out.splitlines() if ln.strip().startswith("Time:")]
    assert time_line, f"no Time line at all:\n{out}"
    # Asserted on the actionable pointer, not the phrasing: what the reader
    # needs is the knob that DOES work, and pinning prose would just freeze
    # today's wording.
    assert "plugins.hpc-launcher.time_minutes" in time_line[0], (
        "Time is printed unmarked beside two fields marked dead, which implies "
        f"it is the one that works. Nothing reads it: {time_line[0]!r}")


# ── The class, not the instance ─────────────────────────────────────────────
#
# The refusal was confusing, but the CAUSE was upstream: the `botainer init`
# template wrote "CPU + memory limits. Applied to BOTH runtimes ... apptainer →
# #SBATCH --cpus-per-task" into every new project's config.yaml, and a shipped
# doc and a shipped example repeated it. All three were run verbatim during
# review and all three hard-refuse. Fixing the message alone would have left
# the product still telling people to write the config it rejects.

def test_no_shipped_example_ships_a_config_that_would_REFUSE() -> None:
    """An apptainer example that sets resources.cpu/memory_mb cannot launch.

    Composed mechanically and reviewed editorially by nobody until asked —
    which is why `examples` is its own prerelease-review category.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for path in sorted((root / "examples").rglob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict) or doc.get("runtime") != "apptainer":
            continue
        res = doc.get("resources") or {}
        set_fields = [k for k in ("cpu", "memory_mb") if res.get(k) is not None]
        if set_fields:
            offenders.append(f"{path.relative_to(root)}: resources.{set_fields}")
    assert not offenders, (
        "these shipped examples declare runtime: apptainer AND set docker-only "
        "resource fields, so following them verbatim is refused:\n  "
        + "\n  ".join(offenders))


def test_the_init_TEMPLATE_does_not_promise_these_work_on_apptainer() -> None:
    """The template is the origin of the belief; assert on what it WRITES.

    Read from the generated text rather than the source comment, so rewording
    the template is free and reintroducing the claim is not.
    """
    from botainer.core.config import write_initial_config
    import tempfile
    # Asserted on the file it WRITES, not on a helper's return value. Two
    # earlier versions of this test guessed at a symbol name that does not
    # exist (`render_config_template`) and at a call shape — the same
    # guess-instead-of-look error as the plugin-yaml path above. What ships to
    # the user is the file, so read the file.
    with tempfile.TemporaryDirectory() as td:
        proj = pathlib.Path(td)
        (proj / ".botainer").mkdir()
        write_initial_config(proj, agent="claude", force=True,
                             runtime="apptainer")
        text = (proj / ".botainer" / "config.yaml").read_text()
    assert "resources:" in text, (
        f"the generated config has no resources block at all:\n{text}")
    lines = [ln for ln in text.splitlines()
             if "cpus-per-task" in ln or "BOTH runtimes" in ln]
    assert not lines, (
        "the init template still tells every new project that resources.cpu / "
        "memory_mb reach apptainer as SBATCH directives. They do not — setting "
        f"either refuses the launch:\n  " + "\n  ".join(lines))
