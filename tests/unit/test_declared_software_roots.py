"""Declared software roots pass the same guards as module-derived roots.

Module-derived roots need a module load and site-policy prefixes. Declared roots
provide a path where that mechanism is unavailable. A declaration is not proof
of safety: a cluster profile can be mistaken, and binding host libraries over
container libraries can break a session regardless of the declaration's source."""
from __future__ import annotations

import inspect
import os

import pytest

from botainer.hpc.module_binds import (
    DeclaredRoot,
    SoftwareRootBindError,
    declared_software_root_binds,
)


@pytest.fixture()
def real_root(tmp_path):
    d = tmp_path / "apps" / "gcc-13"
    d.mkdir(parents=True)
    return str(d)


def test_a_declared_root_becomes_a_read_only_identity_bind(real_root):
    binds, dropped, origins = declared_software_root_binds(
        [DeclaredRoot(real_root, "cluster profile")], site_ceiling=[])

    assert binds == [{"source": real_root, "target": real_root, "mode": "ro"}]
    assert dropped == []
    assert origins[real_root] == "cluster profile"


def test_read_only_is_not_a_parameter():
    """No argument makes these writable, so no caller can get it wrong.

    Structure over a rule: cluster software belongs to the site, and the way to
    guarantee botainer never writes to it is to leave no way to ask.
    """
    params = inspect.signature(declared_software_root_binds).parameters
    assert "mode" not in params and "writable" not in params and "rw" not in params


@pytest.mark.parametrize("path,expect_in_reason", [
    ("/usr/lib", "refused subtree"),
    ("/etc", "refused subtree"),
    ("/bin", "refused subtree"),
    ("/", "shallow system mount point"),
    ("not/absolute", "not an absolute path"),
])
def test_the_derived_paths_guards_apply_to_declared_ones_too(path, expect_in_reason):
    binds, dropped, _ = declared_software_root_binds(
        [DeclaredRoot(path, "cluster profile")], site_ceiling=[])

    assert binds == [], f"{path} must not be bound"
    assert dropped, f"{path} must be reported, not silently skipped"
    assert expect_in_reason in dropped[0][1]


def test_a_root_that_does_not_exist_is_reported_not_ignored(tmp_path):
    """The most likely real-world case: a profile written for a sibling
    cluster, or a path that moved. Binding nothing and staying quiet is how
    "I enabled it and nothing happened" becomes an evening."""
    missing = str(tmp_path / "apps" / "moved-away")

    binds, dropped, _ = declared_software_root_binds(
        [DeclaredRoot(missing, "cluster profile")], site_ceiling=[])

    assert binds == []
    assert "does not exist on this host" in dropped[0][1]


def test_every_refusal_names_where_the_root_CAME_FROM(real_root, tmp_path):
    """A refused path must name its configuration origin so the user can
    decide whether to edit project config, change cluster profile, or contact
    the site administrator.
    """
    missing = str(tmp_path / "apps" / "nope")
    _, dropped, _ = declared_software_root_binds([
        DeclaredRoot(missing, "your config"),
        DeclaredRoot("/etc", "cluster profile"),
    ], site_ceiling=[])

    reasons = " ".join(r for _, r in dropped)
    assert "your config" in reasons
    assert "cluster profile" in reasons


def test_too_many_roots_refuses_rather_than_truncating(tmp_path):
    """Same choice the derived path makes, and for the same reason: a silently
    dropped root yields a binary the agent cannot find, with no error to
    explain it."""
    roots = []
    for i in range(12):
        d = tmp_path / f"apps{i}" / "sub"
        d.mkdir(parents=True)
        roots.append(DeclaredRoot(str(d), "cluster profile"))

    with pytest.raises(SoftwareRootBindError) as exc:
        declared_software_root_binds(roots, site_ceiling=[])
    assert "max_roots" in str(exc.value)
    assert "truncating" in str(exc.value)


def test_one_bad_root_does_not_lose_the_good_ones(real_root):
    """A typo in a profile drops that root and leaves the rest working. The
    alternative — refusing everything — turns one wrong line into an unusable
    cluster profile."""
    binds, dropped, _ = declared_software_root_binds([
        DeclaredRoot(real_root, "cluster profile"),
        DeclaredRoot("/etc", "cluster profile"),
    ], site_ceiling=[])

    assert [b["source"] for b in binds] == [real_root]
    assert len(dropped) == 1


def test_duplicate_roots_collapse(real_root):
    binds, _, _ = declared_software_root_binds([
        DeclaredRoot(real_root, "cluster profile"),
        DeclaredRoot(real_root + "/", "your config"),
    ], site_ceiling=[])

    assert len(binds) == 1


# ── the cluster profile carries them, and must not lose them ────────────────

def test_software_roots_survive_a_profile_round_trip(tmp_path, monkeypatch):
    """WRITE then READ, not just parse.

    The first version of the serialiser emitted `software_roots` at the TOP
    level while `from_dict` reads it under `cluster`. Parsing alone passed;
    every round-trip silently dropped the value. Only writing a profile and
    reading it back caught it, which is why this test does both.
    """
    from botainer.state.cluster_profile import (
        ClusterProfile, load_user_profile, write_user_profile,
    )

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True)

    original = ClusterProfile.from_dict({
        "version": "cluster-profile-v1",
        "cluster": {"name": "somewhere", "software_roots": ["/apps/x", "/sw/y"]},
    })
    assert original.software_roots == ("/apps/x", "/sw/y"), "parse"

    write_user_profile(original)
    assert load_user_profile().software_roots == original.software_roots, (
        "software_roots were lost writing the profile and reading it back — "
        "check the serialiser puts them where from_dict looks"
    )


def test_a_profile_with_no_roots_does_not_gain_an_empty_key(tmp_path, monkeypatch):
    """An empty `software_roots: []` in a written profile reads like a setting
    someone deliberately turned off. Absent means absent."""
    from botainer.state.cluster_profile import ClusterProfile, write_user_profile

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True)
    write_user_profile(ClusterProfile(name="somewhere"))

    assert "software_roots" not in (tmp_path / "cluster.yaml").read_text()


# ── the ceiling may come from the profile, but never OVERRIDE a site one ─────
#
# These call the SHIPPED function. An earlier version of this block restated
# the precedence expression in the test — which would have passed while the
# real code said something else, the exact defect this file's neighbours keep
# catching.

class _Mounts:
    def __init__(self, roots): self.cluster_software_roots = list(roots)


class _Policy:
    def __init__(self, roots): self.mounts = _Mounts(roots)


def _with_profile_roots(monkeypatch, roots):
    from botainer.state import cluster_profile as cp

    class _P:
        software_roots = tuple(roots)

    monkeypatch.setattr(cp, "active_profile", lambda: _P() if roots else None)


def test_the_profile_fills_a_ceiling_nobody_set(monkeypatch):
    """#156: without this, a user with no site admin gets NOTHING bound and no
    way to change it."""
    from botainer.core.composition import software_root_ceiling

    _with_profile_roots(monkeypatch, ["/apps"])
    assert software_root_ceiling(_Policy([])) == (["/apps"], "cluster profile")


def test_a_site_ceiling_wins_and_the_profile_cannot_widen_it(monkeypatch):
    """PRECEDENCE IS ONE-WAY. A cluster profile — which ships with botainer or
    is user-editable — must not reopen what an administrator closed."""
    from botainer.core.composition import software_root_ceiling

    _with_profile_roots(monkeypatch, ["/apps", "/"])
    ceiling, origin = software_root_ceiling(_Policy(["/opt/sitesw"]))
    assert ceiling == ["/opt/sitesw"], "the profile widened a site ceiling"
    assert origin == "site policy"


def test_no_ceiling_from_either_source_stays_off(monkeypatch):
    """Fail-closed: the feature is OFF rather than guessing a root."""
    from botainer.core.composition import software_root_ceiling

    _with_profile_roots(monkeypatch, [])
    assert software_root_ceiling(_Policy([]))[0] == []


def test_a_broken_profile_does_not_take_out_the_session(monkeypatch):
    """No active profile is the normal case on a laptop, and a profile that
    fails to load must not turn a working session into a traceback."""
    from botainer.core.composition import software_root_ceiling
    from botainer.state import cluster_profile as cp

    def boom():
        raise RuntimeError("unreadable cluster.yaml")

    monkeypatch.setattr(cp, "active_profile", boom)
    assert software_root_ceiling(_Policy([]))[0] == []


# ── what the USER is told, which was a condition of making these a default ──

def test_doctor_says_nothing_off_a_cluster():
    """Nothing configured is the normal state on a laptop. A line about a
    feature with no bearing on you is noise, and noise is what makes people
    stop reading the ones that matter."""
    from botainer.cli.doctor import software_root_findings

    assert software_root_findings(
        [], [], {},
        cap_held=True, dropped=[]) == []


def test_doctor_names_the_paths_AND_where_they_came_from():
    from botainer.cli.doctor import software_root_findings

    (f,) = software_root_findings(
        [("/apps/gcc", "cluster profile"), ("/apps/py", "cluster profile")], [], {"/apps/gcc": True, "/apps/py": True},
        cap_held=True, dropped=[])

    assert "/apps/gcc" in f.detail and "/apps/py" in f.detail
    assert "cluster profile" in f.detail, (
        "a path with no origin cannot be acted on — the user cannot tell "
        "whether to edit their config, change profile, or ask an admin"
    )
    assert "read-only" in f.detail


def test_doctor_warns_the_ABI_trap_where_the_user_will_meet_it():
    """Cluster software is built for the HOST OS. A binary that runs on the
    login node can fail in the container, and that is not a broken mount."""
    from botainer.cli.doctor import software_root_findings

    (f,) = software_root_findings(
        [("/apps/gcc", "cluster profile")], [], {"/apps/gcc": True},
        cap_held=True, dropped=[])
    assert "GLIBC" in f.remediation and "ABI" in f.remediation


def test_doctor_says_so_when_it_could_not_WORK_OUT_the_roots(monkeypatch, tmp_path):
    """A swallowed failure in a diagnostic tool is worse than no check.

    The first version of this wiring ended `except Exception: pass`, which
    makes "the check crashed" and "there is nothing to report" produce the
    identical output — silence — and the user reads silence as an all-clear.
    """
    from botainer.cli import doctor
    from botainer.core import composition

    # This check runs only for an existing state root. Do not depend on another
    # test (or the developer's installation) having created the ambient root.
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)

    def boom(_policy):
        raise RuntimeError("unreadable policy.yaml")

    monkeypatch.setattr(composition, "declared_software_roots", boom)

    hits = [f for f in doctor.collect_findings()
            if f.check == "hpc.software_roots"]
    assert hits, "doctor swallowed the failure and reported nothing"
    assert hits[0].severity == "warn"
    assert "could not determine" in hits[0].detail


def test_doctor_reports_a_declared_root_that_is_not_here():
    """The likeliest real failure: a profile written for a sibling cluster.
    Mounting nothing and saying nothing is how that becomes an evening."""
    from botainer.cli.doctor import software_root_findings

    out = software_root_findings(
        [("/apps/here", "cluster profile"), ("/apps/gone", "cluster profile")], [], {"/apps/here": True, "/apps/gone": False},
        cap_held=True, dropped=[])

    missing = [f for f in out if f.check.endswith(".missing")]
    assert missing, "a declared root that does not exist must be reported"
    assert "/apps/gone" in missing[0].detail
    assert "/apps/here" not in missing[0].detail
    assert missing[0].severity == "warn"
    assert "sibling cluster" in missing[0].remediation


# ── the site ceiling still LIMITS a declaration, and is not itself one ───────
#
# #171's wiring turned "declared" into real binds. That made a distinction
# load-bearing which had been merely conceptual: `mounts.cluster_software_roots`
# is an administrator drawing a LIMIT, while `cluster.software_roots` in a
# profile is a user DECLARING a mount. Reading the ceiling as a declaration
# would have silently started binding whole software trees at every site that
# already set one — a behaviour change to someone else's security decision.


def test_a_declaration_outside_the_site_ceiling_is_refused(tmp_path):
    """One-way precedence, at the binding step this time. A cluster profile
    ships with botainer or is user-editable; it must not reopen what an
    administrator closed."""
    inside = tmp_path / "opt" / "sitesw" / "gcc"
    outside = tmp_path / "home" / "me" / "mysw"
    inside.mkdir(parents=True)
    outside.mkdir(parents=True)

    binds, dropped, _ = declared_software_root_binds(
        [DeclaredRoot(str(inside), "cluster profile"),
         DeclaredRoot(str(outside), "cluster profile")],
        site_ceiling=[str(tmp_path / "opt" / "sitesw")])

    assert [b["source"] for b in binds] == [str(inside)]
    assert "outside the site policy ceiling" in dropped[0][1]
    assert str(outside) in dropped[0][0]


def test_the_ceiling_argument_cannot_be_forgotten():
    """Structure over a rule. A default of `[]` would mean a caller who forgot
    it silently dropped an administrator's restriction; a TypeError cannot be
    overlooked. Same shape as run_hook(agent_writable_roots=…).
    """
    params = inspect.signature(declared_software_root_binds).parameters
    p = params["site_ceiling"]
    assert p.default is inspect.Parameter.empty, (
        "site_ceiling gained a default — a caller that omits it now silently "
        "ignores the site policy ceiling instead of failing loudly"
    )


# ── the wiring. Built-and-called-by-nothing is the defect this closes. ───────


class _Manifest:
    def __init__(self, caps): self.capabilities = list(caps)


def _wire(monkeypatch, caps):
    """Make `hpc-modules` an enabled plugin holding `caps`."""
    from botainer.core import composition

    monkeypatch.setattr(composition, "_plugins_declaring_cap",
                        lambda cap, *, spec, inst_by_name:
                        ["hpc-modules"] if cap in caps else [])


def _profile_declaring(monkeypatch, roots):
    from botainer.state import cluster_profile as cp

    class _P:
        software_roots = tuple(roots)

    monkeypatch.setattr(cp, "active_profile", lambda: _P())


def test_a_declared_root_becomes_a_REAL_bind_in_the_session(tmp_path, monkeypatch):
    """THE regression guard. `declared_software_root_binds` was correct,
    tested, and called by nothing — so #171 was not delivered, only written.
    """
    from botainer.core import composition

    root = tmp_path / "apps" / "gcc-13"
    root.mkdir(parents=True)
    _profile_declaring(monkeypatch, [str(root)])
    _wire(monkeypatch, {"caps.modules_software_roots"})

    binds = composition._declared_software_root_binds(
        spec=None, effective_policy=_Policy([]), inst_by_name={})

    assert [b.source for b in binds] == [str(root)]
    assert [b.target for b in binds] == [str(root)], "identity bind"
    assert all(b.mode.value == "ro" for b in binds)
    assert "cluster profile" in binds[0].provenance_detail


def test_the_declared_binds_are_covered_by_selftest(tmp_path, monkeypatch):
    """A read-only claim nothing observes at runtime is the class of assertion
    this project keeps finding wrong. These carry the same self-test label as
    the derived binds, so `botainer selftest` probes them for real."""
    from botainer.core import composition
    from botainer.preflight.checks import checks_for

    root = tmp_path / "apps" / "gcc-13"
    root.mkdir(parents=True)
    _profile_declaring(monkeypatch, [str(root)])
    _wire(monkeypatch, {"caps.modules_software_roots"})

    (bind,) = composition._declared_software_root_binds(
        spec=None, effective_policy=_Policy([]), inst_by_name={})

    assert checks_for(bind.self_test), (
        f"self_test={bind.self_test!r} maps to no runnable preflight check, so "
        f"nothing ever verifies these binds are read-only in the container"
    )


def test_without_the_capability_nothing_is_bound_and_the_user_is_TOLD(
    tmp_path, monkeypatch, capsys
):
    """"The profile is trusted" is a statement about provenance, not about
    authorisation — the cap grant still gates the bind. But a user who wrote
    software_roots and got silence has done the work and seen no result, which
    is #156's whole shape."""
    from botainer.core import composition

    root = tmp_path / "apps" / "gcc-13"
    root.mkdir(parents=True)
    _profile_declaring(monkeypatch, [str(root)])
    _wire(monkeypatch, set())          # no plugin holds the cap

    assert composition._declared_software_root_binds(
        spec=None, effective_policy=_Policy([]), inst_by_name={}) == []
    err = capsys.readouterr().err
    assert "hpc-modules" in err and "plugins_enabled" in err
    assert "module load" in err, (
        "the note must say the module load is NOT the missing piece, or the "
        "user goes looking for the wrong fix"
    )


def test_no_declaration_means_no_binds_and_no_noise(monkeypatch, capsys):
    """Fail-closed and quiet: the laptop case, and every cluster where nobody
    declared anything."""
    from botainer.core import composition
    from botainer.state import cluster_profile as cp

    monkeypatch.setattr(cp, "active_profile", lambda: None)
    _wire(monkeypatch, {"caps.modules_software_roots"})

    assert composition._declared_software_root_binds(
        spec=None, effective_policy=_Policy([]), inst_by_name={}) == []
    assert capsys.readouterr().err == ""


def test_a_declaration_that_is_dropped_is_reported_at_launch(tmp_path, monkeypatch, capsys):
    from botainer.core import composition

    _profile_declaring(monkeypatch, ["/etc", str(tmp_path / "gone")])
    _wire(monkeypatch, {"caps.modules_software_roots"})

    assert composition._declared_software_root_binds(
        spec=None, effective_policy=_Policy([]), inst_by_name={}) == []
    err = capsys.readouterr().err
    assert "NOT mounted" in err
    assert "/etc" in err and "gone" in err


def test_the_REAL_hpc_modules_manifest_holds_the_cap_the_gate_asks_for():
    """The wiring tests monkeypatch the cap lookup, so this one closes the
    circle against the actual shipped manifest. Without it, the whole feature
    could be gated on a capability no bundled plugin declares — a gate nothing
    can pass, and every test above would still be green.
    """
    import pathlib

    from botainer.plugins.manifest import load_manifest

    repo = pathlib.Path(__file__).resolve().parents[2]
    man = load_manifest(repo / "plugins" / "hpc-modules")
    assert "caps.modules_software_roots" in man.capabilities, (
        "the declared-software-roots gate asks for caps.modules_software_roots "
        "and the plugin the user is told to enable does not declare it"
    )


def test_home_is_refused_and_the_reason_names_the_rule(tmp_path):
    """The load-bearing case for min_depth, pinned so a future loosening has to
    face it.

    On a shared cluster `/home` read-only in the container exposes every user's
    home directory. `_is_sensitive_home` does NOT stop it — that guard only
    covers the CURRENT user's own credential dirs (#168). The depth floor is
    what refuses it, so anyone lowering the floor breaks this test and has to
    say what replaces it.
    """
    binds, dropped, _ = declared_software_root_binds(
        [DeclaredRoot("/home", "cluster profile")], site_ceiling=[])

    assert binds == []
    assert "min_depth" in dropped[0][1]


def test_a_deep_system_subtree_IS_bound_and_the_docstring_says_so(tmp_path):
    """The guards are NOT the same as the derived path's, and the code now says
    which. This pins the difference rather than the wish.

    `is_unsafe_module_tree_source` deliberately permits a deep system subtree so
    a real Lmod install at /usr/share/lmod/lmod works; the derived path's
    `_unacceptable_reason` refuses all of /usr. An earlier docstring claimed
    "every guard the derived path applies applies here", which was false and was
    only caught by RUNNING it.
    """
    import os

    from botainer.hpc.module_binds import is_unsafe_module_tree_source

    if not os.path.isdir("/usr/share"):
        import pytest as _pytest
        _pytest.skip("no /usr/share on this host")

    binds, _, _ = declared_software_root_binds(
        [DeclaredRoot("/usr/share", "cluster profile")], site_ceiling=[])
    assert [b["source"] for b in binds] == ["/usr/share"], (
        "declared roots no longer accept a deep system subtree — if that is "
        "deliberate, the Lmod-at-/usr/share/lmod/lmod case needs another route"
    )
    # ...and the shared guard is the one that decided it, not a local rule.
    assert is_unsafe_module_tree_source("/usr/share") == (False, "")


def test_the_wiring_line_itself_is_present_so_the_feature_is_DELIVERED():
    """The gap that let #171 be written and not delivered, and that this file
    could not see until it was measured.

    `test_a_declared_root_becomes_a_REAL_bind_in_the_session` calls
    `_declared_software_root_binds` DIRECTLY. That proves the function is
    correct; it says nothing about whether anything invokes it. Measured
    2026-09-04: deleting the call site from `run_host_pre_launch_hooks` left
    all 33 tests in this file GREEN — the suite could not distinguish a
    delivered feature from a dead one, which is the exact defect the test above
    names in its own docstring.

    A composed-session assertion would be the stronger form. It needs an
    installed state root, a real profile and a plugin holding the cap — the
    machinery the wiring tests deliberately monkeypatch away — so it belongs in
    the integration tier. Until it exists, this pins the call site itself:
    weaker, honest about being weaker, and it FAILS when the line goes.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "botainer" / "core" / "composition.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    called_in: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_declared_software_root_binds"):
                called_in.append(fn.name)

    assert called_in, (
        "_declared_software_root_binds is defined and called by NOTHING, so a "
        "declared software root is computed and then discarded: #171 is "
        "written, not delivered. Restore the call in run_host_pre_launch_hooks."
    )
    assert "run_host_pre_launch_hooks" in called_in, (
        f"the only caller(s) are {called_in}. The declared roots must be added "
        f"where the derived ones are, so they go through the same "
        f"validate_mount_plan backstop."
    )


def test_a_symlink_inside_the_ceiling_cannot_bind_its_target_outside(tmp_path):
    """The site ceiling contains the REALPATH, not the spelling.

    The bind source is the realpath. The ceiling check used to accept a root
    whose LITERAL path sat inside the ceiling, so a symlink inside it bound its
    target — anywhere — and did not even appear in `dropped`. Observed
    2026-09-04 rendered onto both runtimes:

        --bind <C>/otherlab-private:<C>/otherlab-private:ro

    `mounts.cluster_software_roots` is the only control a site admin has over
    this bind class, and it is SITE-ONLY in `intersect` precisely so a user
    cannot widen it. A user-editable cluster.yaml plus one symlink reopened it.

    The other half matters as much: the literal branch existed for a ceiling
    REACHED THROUGH a symlink, which is what tmp_path looks like on macOS
    (/var/... -> /private/var/...). Both directions are pinned below, because
    fixing one by breaking the other is the obvious wrong repair.
    """
    ceiling = tmp_path / "apps"
    (ceiling / "gcc-13").mkdir(parents=True)
    outside = tmp_path / "otherlab-private"
    outside.mkdir()
    (outside / "SECRET").write_text("x", encoding="utf-8")
    (ceiling / "mysw").symlink_to(outside)

    binds, dropped, _ = declared_software_root_binds(
        [DeclaredRoot(str(ceiling / "mysw"), "cluster profile"),
         DeclaredRoot(str(ceiling / "gcc-13"), "cluster profile")],
        site_ceiling=[str(ceiling)])

    sources = [b["source"] for b in binds]
    assert not any("otherlab-private" in s for s in sources), (
        f"a symlink inside the ceiling bound its target OUTSIDE it: {sources}. "
        f"The admin's mounts.cluster_software_roots is the only limit on this "
        f"bind class and a user-editable cluster.yaml just reopened it."
    )
    assert [os.path.basename(s) for s in sources] == ["gcc-13"], (
        f"the legitimate sibling root was lost too: {sources}"
    )
    assert any("outside the site policy ceiling" in r for _p, r in dropped), (
        f"the escape was refused but not REPORTED, so nobody would know a "
        f"declared root went missing: {dropped}"
    )


def test_a_ceiling_reached_through_a_symlink_still_works(tmp_path):
    """The half the naive fix breaks. On macOS `tmp_path` itself is behind a
    symlink, so resolving only the declared root and not the ceiling would drop
    every legitimate root on that platform."""
    (tmp_path / "real" / "apps" / "gcc").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")

    binds, dropped, _ = declared_software_root_binds(
        [DeclaredRoot(str(tmp_path / "link" / "apps" / "gcc"), "cluster profile")],
        site_ceiling=[str(tmp_path / "link" / "apps")])

    assert [b["source"] for b in binds] == [str(tmp_path / "real" / "apps" / "gcc")], (
        f"a ceiling reached through a symlink now refuses its own contents "
        f"(binds={binds}, dropped={dropped})"
    )


def test_doctor_does_not_claim_a_mount_when_no_plugin_holds_the_cap():
    """The default-configuration lie, and the reason cap_held is REQUIRED.

    `hpc-modules` ships COMMENTED OUT in the config `botainer init` writes, so
    on a fresh install nothing holds `caps.modules_software_roots` and the
    launcher binds nothing. Doctor used to be handed the raw profile list and
    print "N root(s) ... mounted read-only" anyway. Observed 2026-09-04 on a
    real install: doctor said three roots were mounted while composition's own
    output was `ACTUAL software-root binds: []`.

    Zero of 51 bundled profiles declare software_roots, so every user of this
    feature hand-edits cluster.yaml and then runs doctor — the whole user
    population was reading the overstatement.
    """
    from botainer.cli.doctor import software_root_findings

    out = software_root_findings(
        [("/apps/gcc-13", "cluster profile")], [], {"/apps/gcc-13": True},
        cap_held=False, dropped=[])

    assert out, "doctor said nothing at all about a declared-but-unmounted root"
    joined = " ".join(f.detail + " " + f.remediation for f in out)
    assert not any("mounted read-only" in f.detail for f in out), (
        f"doctor still claims these are mounted: {[f.detail for f in out]}"
    )
    assert "NONE are mounted" in out[0].detail
    assert out[0].severity == "warn"
    assert "hpc-modules" in joined and "plugins_enabled" in joined, (
        "the finding must name the fix; 'not mounted' alone sends the user "
        "looking for a broken mount rather than an unenabled plugin"
    )
    assert "module load" in joined, (
        "must say the module load is NOT the missing piece (#156)"
    )


def test_doctor_reports_a_root_the_guards_REFUSED_with_the_reason():
    """A refused root is not a missing one, and the reason is the actionable
    part: 'outside the site policy ceiling' needs an admin, 'shallower than
    min_depth' needs a different path. Silence here is how the ceiling escape
    fixed today would have gone unseen on a real cluster."""
    from botainer.cli.doctor import software_root_findings

    out = software_root_findings(
        [("/apps/gcc-13", "cluster profile")], ["/apps"],
        {"/apps/gcc-13": True}, cap_held=True,
        dropped=[("/tmp/elsewhere", "outside the site policy ceiling "
                                    "mounts.cluster_software_roots")])

    refused = [f for f in out if f.check.endswith(".refused")]
    assert refused, f"a refused root was not reported at all: {out}"
    assert "/tmp/elsewhere" in refused[0].remediation
    assert "outside the site policy ceiling" in refused[0].remediation
    assert refused[0].severity == "warn"
