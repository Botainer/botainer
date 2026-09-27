"""The gate this project mandates before pushing a security-surface change
has to RUN on the machine the developer is standing at, it has to say which
plan it checked, and `exit 0` must not be reachable while a plan went
unverified.

`botainer start --preflight` composes a session, verifies the
capability-surface invariants and launches nothing. This project's own
development rules call it "the gate" to clear before pushing any
security-surface change, and its `--help` called it "safe to run anywhere".
MEASURED on a host with neither docker nor apptainer (this dev container,
2026-09-13):

    $ botainer start --preflight
    refused: runtime-not-available: no docker or apptainer found.
        → Install Docker (laptop) or apptainer (HPC) and re-run.
        → To preview without launching, use `botainer dry-run` or `botainer inspect`.
    $ echo $?
    3

`start` refuses the mock fallback because a session cannot LAUNCH without a
runtime — correct for a launch, and it fired before the preflight branch was
ever reached, so the mandated gate exited 3 on exactly the hosts its help
text called safe. The two remedies it offers are `dry-run` and `inspect`,
neither of which performs any invariant check.

SCOPE OF THAT DEFECT, corrected by the refuting review after I had written the
broader version here: it hits projects whose config says `runtime: auto`, which
is what plain `botainer init` writes. `_resolve_runtime` consults PATH only for
`auto`, so a project that says `runtime: apptainer` — what `init --runtime
apptainer` writes, as GETTING_STARTED-HPC tells an HPC user to — reached the
gate and had its apptainer plan checked.

AND THE HALF THAT MATTERS MORE. Even where the gate did run it checked ONE
runtime, and the verdict read as "the surface". An `auto` project on a laptop
got its docker plan checked and its apptainer plan not checked at all — and the
two are not substitutes: measured on an `hpc-launcher` project, `--runtime
docker` is REFUSED outright (`unsupported-runtime-feature`), because such a
project has no docker plan to check.

THE FIRST FIX MADE THAT WORSE IN ONE WAY, which is why these tests exist in
this shape. Checking both plans and merely NAMING the ones that would not
compose turned five invocations that exited 2 into exit 0 plus the green word
"clean" — including a `.sif` whose sha256 no longer matched `installed.lock`,
botainer's own tamper refusal, reported as if the host were short of a binary.
So a skip is a pass ONLY when the plan does not exist (an apptainer-only plugin
has no docker plan); every other skip is exit 3, and the plan the project
actually targets must be among the checked.

Every test here drives the REAL CLI as a subprocess, because the defect was
in `start`'s ordering — calling `preflight.run()` directly passes whatever
spec you hand it and never meets the refusal that made the gate unrunnable.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    shutil.which("botainer") is None,
    reason="drives the installed CLI on purpose: the defect was in start's "
           "ordering, not in the checks",
)

# Enough to run `botainer` and for it to do its own work, and NOTHING that
# `_resolve_runtime` could discover. Building the PATH out of symlinks (rather
# than trusting that this host has no docker) is what makes the mock-fallback
# case reproducible on a developer laptop that does.
_PATH_TOOLS = ("botainer", "git", "sh", "bash", "uname", "env", "python3")


def _restricted_path(tmp_path: Path) -> str:
    bindir = tmp_path / "bin-no-runtime"
    bindir.mkdir(exist_ok=True)
    for name in _PATH_TOOLS:
        real = shutil.which(name)
        if real and not (bindir / name).exists():
            (bindir / name).symlink_to(real)
    assert shutil.which("docker", path=str(bindir)) is None
    assert shutil.which("apptainer", path=str(bindir)) is None
    assert shutil.which("singularity", path=str(bindir)) is None
    return str(bindir)


def _run(args, *, home, state, cwd, path=None):
    """The real CLI, with HOME and MY_BOTAINER pointed at scratch."""
    env = {
        **os.environ,
        "HOME": str(home),
        "MY_BOTAINER": str(state),
        "BOTAINER_TESTING": "1",
    }
    env.pop("BOTAINER_STATE_ROOT", None)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["botainer", *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


def _set_config(proj: Path, *, runtime: str | None = None,
                plugins: tuple[str, ...] | None = None) -> None:
    """Rewrite `runtime:` / the whole `plugins_enabled:` block in place.

    The block is REPLACED, not appended to: `config.yaml` mentions
    `plugins_enabled` and `hpc-launcher` in its own comments, so an append
    guarded by a substring test silently does nothing (it did, while I was
    measuring, and the result looked like a code finding).
    """
    cfg = proj / ".botainer" / "config.yaml"
    s = cfg.read_text()
    if plugins is not None:
        i = s.index("plugins_enabled:")
        j = s.index("\n# ", i)
        s = s[:i] + "plugins_enabled:\n" + "".join(
            f"  - {p}\n" for p in plugins) + s[j:]
    if runtime is not None:
        import re
        if re.search(r"^runtime:", s, re.M):
            s = re.sub(r"^runtime:.*$", f"runtime: {runtime}", s, count=1,
                       flags=re.M)
        else:
            s = f"runtime: {runtime}\n" + s
    cfg.write_text(s)


@pytest.fixture(scope="module")
def _base(tmp_path_factory):
    """`setup` + `init` ONCE for the module; every test restores from here.

    Two constraints shape this fixture. Repeated `setup --force` + `init`
    subprocesses are expensive. A state root and a project root reference
    each other by ABSOLUTE path, so a per-test COPY is a
    second location for one project UUID and botainer correctly refuses it
    (`identity-change-refused` — measured, which is how this fixture ended up
    written this way rather than as a copytree).

    So: one project, created once, and the `project` fixture below restores the
    two things tests mutate before each test. Restoring rather than sharing is
    what keeps the file order-independent under the suite's randomisation.

    Load-bearing, each found by running rather than reading:

    * `git init` — `init` wants a repo.
    * an `image:` line — without a recorded image the docker-shaped resolver
      refuses `config-missing` before any check runs.
    * a `.sif` at the canonical prefixed name. EXISTENCE is the whole
      requirement on both routes: `state/dir.py`'s resolver tests `is_file()`
      and nothing more, and a 0-byte file at that name is accepted (measured —
      an earlier version of this docstring claimed a ~1 MiB plausibility floor,
      which belongs to `image build --verify`, not here). So this writes a few
      bytes: imitating a plausible image size here would be theatre.

    `botainer init` writes `runtime: auto`, which is the shape the original
    defect needs; tests that care about a pinned runtime call `_set_config`.
    """
    base = tmp_path_factory.mktemp("preflight")
    home, state, proj = base / "home", base / "state", base / "proj"
    for d in (home, proj):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=proj, check=True)

    r = _run(["setup", "--force"], home=home, state=state, cwd=proj)
    assert r.returncode == 0, f"setup failed:\n{r.stdout}\n{r.stderr}"
    r = _run(["init", "--agent", "claude"], home=home, state=state, cwd=proj)
    assert r.returncode == 0, f"init failed:\n{r.stdout}\n{r.stderr}"

    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text() + "\nimage: ubuntu:24.04\n")
    (state / "images").mkdir(parents=True, exist_ok=True)
    return home, state, proj, cfg.read_text()


@pytest.fixture
def project(_base):
    """(home, state, proj, sif), with config + .sif restored to pristine."""
    home, state, proj, pristine_cfg = _base
    (proj / ".botainer" / "config.yaml").write_text(pristine_cfg)
    sif = state / "images" / "botainer-agent-claude.sif"
    sif.write_bytes(b"SIF")
    return home, state, proj, sif


def test_the_gate_runs_on_a_host_with_no_container_runtime(project, tmp_path):
    """THE DEFECT: exit 3 before the gate was reached.

    Nothing is launched by a preflight, so nothing about it needs a daemon or
    an installed `apptainer`. Fails against HEAD with
    `refused: runtime-not-available`.
    """
    home, state, proj, _sif = project
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"the gate did not run:\n{out}"
    assert "runtime-not-available" not in out, out
    assert "Binds:" in out, out
    # And it must not "run" by checking the mock plan, which is a third plan
    # neither runtime uses — that would be a gate passing on a fiction.
    assert "Runtime: mock" not in out, f"verified the mock plan:\n{out}"


def test_it_checks_both_plans_and_they_are_two_DIFFERENT_plans(project, tmp_path):
    """One runtime checked, verdict read as "the surface" — the parity half.

    Pinned on the IMAGE each plan resolves, not on the section headers: the
    docker plan takes the docker-shaped branch and the apptainer plan resolves
    a `.sif`, so if both appear the two composes really happened. A test that
    only counted headers would pass if one plan were printed twice.
    """
    home, state, proj, sif = project
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "runtime: docker" in out and "runtime: apptainer" in out, out
    assert "ubuntu:24.04" in out, f"no docker-shaped image resolved:\n{out}"
    assert str(sif) in out, f"the apptainer plan was not composed:\n{out}"


def test_an_UNVERIFIED_plan_is_named_AND_fails_the_gate(project, tmp_path):
    """"exit 0 is the gate", so an unchecked plan cannot be one.

    With no `.sif` the apptainer plan cannot be composed here. The docker plan
    still can — but a verdict covering half the surface must not be green, and
    the refuting review caught exactly this: naming the skip while exiting 0
    turned botainer's own tamper refusal into a pass.
    """
    home, state, proj, sif = project
    sif.unlink()
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 3, f"an unverified plan passed the gate:\n{out}"
    assert "NOT VERIFIED" in r.stderr, (
        f"an unverified runtime was dropped in silence:\n{out}")
    assert "apptainer" in out and ".sif" in out, (
        f"the skip does not say WHY, so nobody can act on it:\n{out}")
    assert "INCOMPLETE" in out and "NOT VERIFIED apptainer" in out, (
        f"the verdict line does not carry the unverified plan:\n{out}")


def test_a_plan_that_DOES_NOT_EXIST_is_not_held_against_the_gate(project, tmp_path):
    """The other direction, and it is what keeps the rule usable.

    `hpc-launcher` declares `runtimes: [apptainer]`, so this project HAS no
    docker plan — on any host, not just this one. That is not an unverified
    plan and must not block a clean verdict, or every HPC project would be
    permanently ungateable.
    """
    home, state, proj, _sif = project
    _set_config(proj, plugins=("agent-claude", "git", "hpc-launcher"))
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"a non-existent plan blocked the gate:\n{out}"
    assert "not applicable" in out and "no docker plan" in out, out
    assert "NOT VERIFIED" not in out, (
        f"a plan that cannot exist is reported as unverified:\n{out}")


def test_the_plan_the_project_TARGETS_must_be_among_the_checked(project, tmp_path):
    """`runtime: apptainer` + no `.sif` → the session's own plan is unchecked.

    Whatever the skip reason, a verdict that covers only the plan this project
    does NOT run is a false all-clear. The message must say that this is the
    targeted plan, because "apptainer was skipped" reads very differently when
    apptainer is the whole point of the project.
    """
    home, state, proj, sif = project
    _set_config(proj, runtime="apptainer")
    sif.unlink()
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert "the plan this project targets" in out, (
        f"nothing says the skipped plan is the one this project runs:\n{out}")

    # And the inapplicable-docker case must still pass for such a project.
    sif.write_bytes(b"SIF")
    _set_config(proj, plugins=("agent-claude", "git", "hpc-launcher"))
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"an apptainer-pinned HPC project cannot pass:\n{out}"
    assert "checked apptainer" in out, out


def test_a_project_pinned_to_a_runtime_its_own_plugin_refuses_is_not_a_pass(
        project, tmp_path):
    """`runtime: docker` + an apptainer-only plugin — the case a mutation found.

    "No such plan" is benign for a runtime this project does not use, and NOT
    benign for the one it pins: the session it would launch cannot compose at
    all. Deleting the `rt != targeted_runtime` clause killed NO test until this
    one existed, because every other unverified case here is `config-missing`,
    which the category rule catches on its own. That is the whole reason the
    protocol says to mutate every branch.
    """
    home, state, proj, _sif = project
    _set_config(proj, runtime="docker",
                plugins=("agent-claude", "git", "hpc-launcher"))
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 3, (
        f"a project whose own plugin refuses its pinned runtime passed:\n{out}")
    assert "NOT VERIFIED" in out and "the plan this project targets" in out, out
    assert "not applicable" not in out, (
        f"the pinned runtime's refusal was written off as inapplicable:\n{out}")


def test_zero_checkable_plans_is_a_FAILURE_not_a_quiet_pass(project, tmp_path):
    """Realistic shape: an HPC project before the `.sif` has been built.

    `hpc-launcher` makes the docker plan inapplicable and the missing `.sif`
    makes the apptainer plan uncomposable, so there is nothing true the gate
    can say. It must say THAT, and not 0.
    """
    home, state, proj, sif = project
    sif.unlink()
    _set_config(proj, plugins=("agent-claude", "git", "hpc-launcher"))
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 3, f"reported success having checked nothing:\n{out}"
    assert "PREFLIGHT DID NOT RUN" in out and "not a pass" in out, out
    assert "docker" in out and "apptainer" in out, (
        f"does not say which plans could not be composed:\n{out}")
    assert "Binds:" not in out, (
        f"claims to have checked a plan it could not compose:\n{out}")


def test_an_explicit_runtime_still_checks_that_one_only(project, tmp_path):
    """`--runtime docker` means docker. The multi-runtime default must not
    silently start composing a second plan for someone who named one."""
    home, state, proj, _sif = project
    r = _run(["start", "--preflight", "--runtime", "docker"],
             home=home, state=state, cwd=proj, path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "runtime: docker" in out, f"no docker section at all:\n{out}"
    assert "runtime: apptainer" not in out, (
        f"checked a runtime the user did not ask for:\n{out}")


def test_an_explicit_runtime_that_cannot_compose_is_a_plain_refusal(project, tmp_path):
    """`--runtime apptainer` with no .sif asks one question and gets one answer.

    Nothing is partial and nothing needs aggregating, so it stays the ordinary
    refusal (exit 2, with the build command) instead of being re-dressed as a
    preflight verdict — the multi-runtime skip list exists for the case where
    OTHER plans were still checked.
    """
    home, state, proj, sif = project
    sif.unlink()
    r = _run(["start", "--preflight", "--runtime", "apptainer"],
             home=home, state=state, cwd=proj, path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 2, f"expected the refusal contract, got {r.returncode}:\n{out}"
    assert "refused:" in out and "--runtime apptainer" in out, out
    assert "NOT VERIFIED" not in out, (
        f"reported a partial verdict for a single named runtime:\n{out}")


def test_a_shared_mode_preflight_still_says_the_credential_is_shared(project, tmp_path):
    """A disclosure the gate used to make and must not stop making.

    The shared-mode banner lives past the launch compose, which a preflight no
    longer reaches. It is the ONLY place a preflight says this session shares
    one credential with other projects — the `/shared-auth` bind itself is a
    hook contribution, so it is absent from the compose-time plan the gate
    prints. Found by the refuting review of the first version of this fix.
    """
    home, state, proj, _sif = project
    _set_config(proj, plugins=("agent-claude-shared", "git"))
    r = _run(["start", "--preflight"], home=home, state=state, cwd=proj,
             path=_restricted_path(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode in (0, 3), out
    # Assert on the BANNER's own words, from the one function that owns them —
    # not on "shared", which the plugin name puts in the bind list anyway and
    # would pass with no banner at all.
    from botainer.cli.start import shared_mode_banner
    lead = shared_mode_banner()[0][:60]
    assert lead in " ".join(out.split()) or "ONE SESSION AT A TIME" in out, (
        f"a shared-mode session was previewed with no shared-mode warning:\n{out}")


def test_a_forbidden_bind_in_ONE_plan_fails_the_whole_gate(tmp_path):
    """Per-runtime findings must aggregate. A clean docker plan must not mask a
    forbidden bind in the apptainer plan — that is the bug shape the split gate
    exists to catch, so it is pinned through `run_all`, not through `run`."""
    from botainer.core.spec import (
        Bind, BindMode, EnvSpec, MountPlan, NetworkMode, NetworkSpec,
        Provenance, SessionSpec,
    )
    from botainer.inspect import preflight

    def _spec(runtime: str, source: str) -> SessionSpec:
        return SessionSpec(
            session_id="ses-x", project_uuid="u" * 32, project_root="/tmp/p",
            image="t:0.1", runtime=runtime, state_dir="/tmp/s",
            plugins_enabled=(), env=EnvSpec(values={}),
            mount_plan=MountPlan(binds=(Bind(
                source=source, target="/data", mode=BindMode.RO,
                provenance=Provenance.USER, provenance_detail="test"),)),
            network=NetworkSpec(mode=NetworkMode.NONE),
        )

    # BOTH orders: a last-one-wins aggregate passes the first case and fails
    # the second, which is exactly the mutation this pins.
    for bad in ("apptainer", "docker"):
        rc = preflight.run_all(
            lambda rt, bad=bad: _spec(
                rt, "/home/user/.ssh" if rt == bad else "/home/user/project"),
            requested_runtime="auto",
        )
        assert rc == 1, f"a forbidden bind in the {bad} plan did not fail the gate"
    assert preflight.run_all(
        lambda rt: _spec(rt, "/home/user/project"), requested_runtime="auto"
    ) == 0, "two clean plans did not pass — the aggregate is not a max()"


def test_the_gate_does_not_claim_to_render_argv(tmp_path):
    """It never touches an adapter, and said it did.

    `--preflight`'s help and the module docstring both promised a "rendered
    runtime argv"; the module reads `spec.mount_plan` / `spec.env` and prints
    the runtime NAME. A reader who believed the claim would think flag
    rendering — `--bind` vs `-v`, `--cleanenv`, `--drop-caps` — was covered
    here. It is not; `tests/unit/test_adapter_argv.py` covers that.
    """
    src = (REPO / "botainer/inspect/preflight.py").read_text()
    assert "render_argv(" not in src and "botainer.adapters" not in src, (
        "preflight now calls an adapter — if it really renders argv, say so in "
        "the help text and delete this test")

    from botainer.cli.start import start as start_cmd
    opt = next(p for p in start_cmd.params if p.name == "preflight")
    # The OPTION's own help, not the whole --help page: `--dry-run` prints the
    # runtime argv for real and says so, which a page-wide grep would flag.
    assert "argv" not in (opt.help or ""), (
        f"--preflight still promises argv rendering:\n{opt.help}")
    assert "compose" in (opt.help or "").lower(), (
        f"--preflight does not say it is the COMPOSE-time plan:\n{opt.help}")
