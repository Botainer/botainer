"""`botainer doctor` must say WHICH botainer is running.

Built after an hour was lost to exactly this. A dev container had an
editable install (a .pth pointing at the checkout) AND a plain copied install in
site-packages. The copy shadows the .pth, so `import botainer` returned code
SEVEN WEEKS OLD — while `pip list`, `pip show` and the version string all
reported the editable install and looked entirely correct.

The failure mode is the nastiest kind: a fix is written, committed, and passes
its tests (which run from the checkout), then "doesn't work" — because the thing
being RUN is not the thing being EDITED. Every other doctor finding describes
the behaviour of whatever code got imported, so this check runs FIRST: if a
stale copy is live, the rest of the report is about the wrong program.

The user's question was "what's the install issue? why aren't we fixing?" — and
the answer was that nothing in botainer could answer "which install is
live?", so it took hand-inspection of site-packages. A question the user has to
ask is usually a missing capability (CLAUDE.md), and this is the capability.

Tests drive the PURE decision function with explicit facts, so the shadowed
state can be tested without constructing a shadowed install.
"""
from __future__ import annotations

from pathlib import Path

from botainer.cli.doctor import install_findings


def _by_check(findings, name):
    return next((f for f in findings if f.check == name), None)


# --------------------------------------------------------------------------
# The state that actually happened.
# --------------------------------------------------------------------------

def test_a_copy_shadowing_an_editable_checkout_is_an_ERROR() -> None:
    """THE regression guard. Editable install points at /workspace, but the
    imported package lives in site-packages: edits to the checkout are not what
    runs, and nothing said so."""
    findings = install_findings(
        live_module_dir=Path("/packages/pip/lib/python3.14/site-packages/botainer"),
        editable_target=Path("/workspace"),
        version="0.1.0a1",
    )
    f = _by_check(findings, "install.shadowed")
    assert f is not None, "a shadowed install produced no finding at all"
    assert f.severity == "err", (
        f"shadowing is silent and makes every fix appear not to work; it must "
        f"be actionable, got severity={f.severity!r}")
    assert "/workspace" in f.detail and "site-packages" in f.detail, (
        "the finding must name BOTH paths — which one is edited and which one "
        "actually runs — or it cannot be acted on")
    assert "pip uninstall" in f.remediation and "pip install -e" in f.remediation


def test_a_healthy_editable_install_says_NOTHING() -> None:
    """Silence on the happy path.

    doctor ALREADY reports the live path (`install.code_loaded_from`) and the
    clone (`install.editable_clone`) — checks that existed before this one, and
    which I failed to read before adding a third line saying the same thing.
    The user's own doctor output showed all three stacked up. Every line has to
    earn its place or the whole report becomes scenery people scroll past,
    which is how the next real finding gets missed.
    """
    findings = install_findings(
        live_module_dir=Path("/workspace/botainer"),
        editable_target=Path("/workspace"),
        version="0.1.0a1",
    )
    assert findings == [], (
        f"healthy install produced output that duplicates "
        f"install.code_loaded_from / install.editable_clone: {findings}")


def test_a_normal_non_editable_install_is_not_accused_of_shadowing() -> None:
    """Ordinary users `pip install botainer` with no checkout. There is nothing
    to diverge from, so shadowing is not a possible state — inventing a check
    that cannot fail would just be noise in everyone's doctor output."""
    findings = install_findings(
        live_module_dir=Path("/usr/lib/python3/site-packages/botainer"),
        editable_target=None,
        version="0.1.0",
    )
    assert findings == []


def test_an_unknown_import_location_warns_rather_than_claiming_health() -> None:
    findings = install_findings(
        live_module_dir=None, editable_target=Path("/workspace"), version="?")
    assert findings[0].severity == "warn"


# --------------------------------------------------------------------------
# The console script, broken the same evening by a deleted venv.
# --------------------------------------------------------------------------

def test_a_console_script_with_a_deleted_interpreter_is_an_ERROR(
        tmp_path) -> None:
    """`botainer` on PATH whose shebang names a venv that no longer exists
    fails with "bad interpreter", which reads as "botainer is broken" rather
    than "this launcher script is stale"."""
    findings = install_findings(
        live_module_dir=Path("/workspace/botainer"),
        editable_target=Path("/workspace"),
        version="0.1.0a1",
        console_script=tmp_path / "bin" / "botainer",
        script_interpreter=tmp_path / "gone" / "python3",
    )
    f = _by_check(findings, "install.console_script")
    assert f is not None and f.severity == "err"
    assert "bad interpreter" in f.detail
    assert str(tmp_path / "gone" / "python3") in f.detail


def test_a_console_script_with_a_live_interpreter_is_silent(tmp_path) -> None:
    """No finding at all — a working script is not worth a line of output."""
    interp = tmp_path / "python3"
    interp.write_text("#!/bin/sh\n")
    findings = install_findings(
        live_module_dir=Path("/workspace/botainer"),
        editable_target=Path("/workspace"),
        version="0.1.0a1",
        console_script=tmp_path / "botainer",
        script_interpreter=interp,
    )
    assert _by_check(findings, "install.console_script") is None


# --------------------------------------------------------------------------
# Wired in, and running against this very interpreter.
# --------------------------------------------------------------------------

def test_the_real_environment_is_gathered_without_exploding() -> None:
    """`collect_install_findings` probes real pip metadata, the real imported
    module and the real PATH. It must never raise — doctor's job is to report
    problems, and a doctor that crashes on a broken install is useless exactly
    when it is needed."""
    from botainer.cli.doctor import collect_install_findings
    findings = collect_install_findings()
    assert isinstance(findings, list)
    # Empty is the CORRECT result on a healthy install — this collector speaks
    # only when something is wrong. Anything it does emit must be an install
    # problem, never a status line duplicating install.code_loaded_from.
    for f in findings:
        assert f.check.startswith("install."), f"stray finding {f.check!r}"
        assert f.severity in ("warn", "err"), (
            f"{f.check} is severity {f.severity!r}; this collector reports "
            f"problems only")


def test_an_install_problem_is_reported_before_anything_else() -> None:
    """Ordering is load-bearing for the FAILURE case: every later finding
    describes the behaviour of whatever code got imported, so if the wrong copy
    is live the rest of the report is about the wrong program. The collector
    runs first so its errors head the list; on a healthy install it contributes
    nothing and the first line is whatever check comes next.
    """
    from botainer.cli.doctor import collect_findings, collect_install_findings
    problems = collect_install_findings()
    if problems:
        assert collect_findings()[0].check.startswith("install."), (
            "an install problem exists but is not the first thing reported")


def test_a_copy_is_reported_even_when_the_checkout_won_the_import() -> None:
    """The case that would have lied to us.

    Run from inside the repo, the current directory wins sys.path, so the
    import looks perfectly healthy — while `botainer` launched from any other
    directory still executes the installed copy. A developer asking "why isn't
    my fix live?" is standing IN the repo when they run doctor, which is
    exactly where the naive check reports "fine".

    So the check asks whether the copy EXISTS, not merely which one won today.
    """
    findings = install_findings(
        live_module_dir=Path("/workspace/botainer"),
        editable_target=Path("/workspace"),
        version="0.1.0a1",
        foreign_package_dirs=(
            Path("/packages/pip/lib/python3.14/site-packages/botainer"),),
    )
    f = _by_check(findings, "install.shadowed")
    assert f is not None, (
        "an installed copy existed alongside the editable install and doctor "
        "said nothing, because this run happened to import the checkout")
    assert f.severity == "err"
    assert "site-packages" in f.detail
    assert "depending on where you stand" in f.detail


# --------------------------------------------------------------------------
# The PROBE, not the decision. `install_findings` above is pure and was always
# testable; `collect_install_findings` reads the real environment, and that is
# the half where the shadow check went wrong — it called a `botainer/`
# directory in site-packages a shadowing COPY, which is what this project's own
# `pip install -e .` leaves behind. Every fresh editable install therefore got
# `botainer doctor` exit 1 and `botainer setup` ABORT (exit 2), on the install
# command README.md, both GETTING_STARTED files and DEPLOY.md all give.
#
# The five setup/doctor tests elsewhere in the suite DO catch a revert — but
# only where the packaging actually produces that directory. OBSERVED: with the
# fix reverted and the directory removed (this dev container's install shape,
# dated before force-include landed), all 39 of them pass. That is exactly how
# the defect shipped. These two drive the real collector against a constructed
# site-packages, so they discriminate on every machine.
# --------------------------------------------------------------------------

def _fake_site_packages(root: Path, *, importable: bool) -> Path:
    """A site-packages holding what `pip install -e .` REALLY leaves.

    Not a minimal directory — this is the observed content after
    `pip install -e .` into a fresh venv:
    `_builtin_plugins/`, `cluster_profiles/`, `licenses/`,
    `THIRD-PARTY-LICENSES.md`, and NO `__init__.py`. It exists because
    `[tool.hatch.build.targets.wheel.force-include]` maps those trees under
    `botainer/` and a .pth cannot redirect data files.

    `importable=True` adds the `__init__.py` that makes it a real package —
    the shape that DOES shadow, and the one the check exists for.
    """
    sp = root / "site-packages"
    pkg = sp / "botainer"
    (pkg / "_builtin_plugins" / "agent-claude").mkdir(parents=True)
    (pkg / "_builtin_plugins" / "agent-claude" / "botainer-plugin.yaml").write_text(
        "name: agent-claude\n")
    (pkg / "cluster_profiles").mkdir()
    (pkg / "cluster_profiles" / "generic-slurm.yaml").write_text("name: x\n")
    (pkg / "licenses").mkdir()
    (pkg / "licenses" / "MPL-2.0.txt").write_text("license text\n")
    (pkg / "THIRD-PARTY-LICENSES.md").write_text("attribution\n")
    if importable:
        (pkg / "__init__.py").write_text('__version__ = "0.1.0a4"\n')
        (pkg / "cli").mkdir()
        (pkg / "cli" / "__init__.py").write_text("")
    return sp


def _collect_against(monkeypatch, site_packages: Path):
    """Run the REAL collector with site-packages and the pip metadata faked.

    Driving `collect_install_findings` rather than a helper is the point: the
    bug was in the environment probe, and a test of the pure decision function
    could not see it (four such tests were green throughout).
    """
    import site as _site
    import sysconfig as _sysconfig
    import importlib.metadata as _md
    import json as _json
    import botainer as _pkg

    checkout = Path(_pkg.__file__).resolve().parent.parent
    sp = str(site_packages)
    monkeypatch.setattr(_sysconfig, "get_paths",
                        lambda *a, **k: {"purelib": sp, "platlib": sp})
    monkeypatch.setattr(_site, "getsitepackages", lambda: [sp])
    monkeypatch.setattr(_site, "getusersitepackages", lambda: sp)

    class _Dist:
        def read_text(self, name):
            if name != "direct_url.json":
                return None
            return _json.dumps({"dir_info": {"editable": True},
                                "url": checkout.as_uri()})

    monkeypatch.setattr(_md, "distribution", lambda _name: _Dist())
    from botainer.cli.doctor import collect_install_findings
    return collect_install_findings()


def test_the_data_tree_every_editable_install_leaves_is_not_a_copy(
    tmp_path: Path, monkeypatch,
) -> None:
    """THE regression guard for the onboarding block.

    A `site-packages/botainer/` holding force-included DATA and no
    `__init__.py` cannot win `import botainer` — a directory without one is
    only a namespace portion, and the checkout's regular package, added later
    by the .pth, beats it. OBSERVED both ways in a fresh venv. So it is not a
    shadowing copy and doctor must say nothing.
    """
    sp = _fake_site_packages(tmp_path, importable=False)
    assert (sp / "botainer").is_dir(), "fixture must be a real dir, as pip leaves it"
    findings = _collect_against(monkeypatch, sp)
    assert _by_check(findings, "install.shadowed") is None, (
        "the data tree pip leaves for a NORMAL editable install was reported "
        "as a shadowing copy — `botainer setup` aborts with exit 2 for every "
        "new user, and the remediation it prints recreates the same state")


def test_an_importable_copy_beside_the_editable_install_is_still_reported(
    tmp_path: Path, monkeypatch,
) -> None:
    """And the guard must still bite. With `__init__.py` present the directory
    DOES win the import (OBSERVED: planting one made `import botainer` resolve
    to site-packages instead of the checkout), which is the seven-weeks-stale
    install this check was built for."""
    sp = _fake_site_packages(tmp_path, importable=True)
    findings = _collect_against(monkeypatch, sp)
    f = _by_check(findings, "install.shadowed")
    assert f is not None, (
        "an importable installed copy sat beside the editable install and "
        "doctor said nothing")
    assert f.severity == "err"
