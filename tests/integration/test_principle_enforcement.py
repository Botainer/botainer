"""In-code enforcement of the stated CLAUDE.md principles.

The umbrella-bind postmortem documented one specific failure mode.
This file generalizes it: for each principle in CLAUDE.md / design/,
the file asserts the principle in code. When the principle is
violated, the test fails at commit time — not at codex-review time
12 hours later.

Principles enforced HERE (AUDIT: this inventory was inaccurate —
it claimed "one test per" 6 principles but principle 2 had no test and the
"Principle 5" section tested default_auth_mode, not the envelope principle.
Corrected to an accurate index, with the principles tested in OTHER files
delegated explicitly so a reader can find every enforcement):

  - "Capabilities are explicit grants, not boolean off-switches."
        test_principle_capabilities_are_explicit_grants (docker)
        + ..._apptainer  (both adapters, every capability)
  - "Plugins cannot bypass the env denylist."
        test_principle_env_denylist_is_enforced_against_hostile_plugin
  - "Credential-shaped env vars are never inherited into plugin subprocesses."
        test_principle_credential_env_vars_are_blocked
  - "Site policy constrains default_auth_mode (it is a ceiling, not advice)."
        test_principle_default_auth_mode_is_constrained
  - "No bind to host-private metadata / forbidden mount targets."
        test_principle_preflight_rejects_synthetic_umbrella_bind
        + ..._docker_sock_bind

Enforced in OTHER files (delegated — same principles-are-tests rule applies):
  - "Files visible to the agent are untrusted input."
        tests/hostile/test_malicious_configs.py (hostile .botainer/config.yaml
        fields, incl. agent/profile traversal → CONFIG_SCHEMA_MISMATCH);
        tests/unit/test_project_uuid_validation.py + test_image_validation.py
        (tampered git-shareable project-id / image); tests/unit/test_hpc_launcher.py
        (the HPC host_helper mirror: agent/profile/uuid/image traversal).
  - "Plugin contributions stay inside their declared envelope."
        tests/unit/test_mount_composition.py
        ::test_plugin_contribution_outside_envelope_refused.

Each test names the principle in its docstring. If a test fails, re-reading
the postmortem before "fixing" the test is recommended — the test is the
enforcement; loosening it requires explicit acknowledgement.
"""

from __future__ import annotations

from botainer.capabilities.registry import all_capability_names
from botainer.cli.auth import _PLUGIN_ENV_BLOCKLIST_EXACT
from botainer.core.policy import SitePolicy

# ── capabilities are explicit grants ──────────────────────────────


def test_principle_capabilities_are_explicit_grants() -> None:
    """CLAUDE.md: 'Capabilities (network, ssh, mounts, credentials,
    scheduler) are explicit grants, not boolean off-switches.'

    Old test (J pattern — #274): asserted the registry's NAME SET
    didn't contain {"all", "*", "unrestricted", ...}. A capability
    named "sysadmin" would pass it trivially. Names aren't behavior.

    New test (this one): for every capability that exists, render a
    docker session and assert the resulting argv carries the
    container-safety flags unconditionally (--cap-drop ALL,
    --security-opt no-new-privileges) AND DOES NOT carry escape-hatch
    flags (--privileged, --cap-add=ALL, seccomp=unconfined). The
    behavioral invariant — "no escape hatch reachable through the
    capability surface" — is what the principle actually pins.
    """
    from botainer.adapters.docker import DockerAdapter
    from botainer.core.spec import (
        Bind,
        BindMode,
        CapabilityGrant,
        MountPlan,
        NetworkMode,
        NetworkSpec,
        Provenance,
        SessionSpec,
    )

    safe_required = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    forbidden_substrings = ["--privileged", "--cap-add=ALL", "seccomp=unconfined"]

    plan = MountPlan(binds=(
        Bind(
            source="/host/proj", target="/workspace", mode=BindMode.RW,
            provenance=Provenance.CORE,
        ),
    ))

    for cap_name in all_capability_names():
        spec = SessionSpec(
            session_id="abcdef0123456789",
            project_uuid="11111111-1111-1111-1111-111111111111",
            project_root="/host/proj",
            state_dir="/state/dir",
            runtime="docker",
            image="img@sha256:" + "a" * 64,
            mount_plan=plan,
            network=NetworkSpec(mode=NetworkMode.NONE),
            capabilities=(CapabilityGrant(name=cap_name),),
        )
        argv = DockerAdapter().render_argv(spec)
        joined = " ".join(argv)
        for forbidden in forbidden_substrings:
            assert forbidden not in joined, (
                f"argv contains forbidden flag {forbidden!r} when capability "
                f"{cap_name!r} is granted; an escape hatch exists. "
                f"Full argv: {argv}"
            )
        for required_flag in safe_required:
            assert required_flag in argv, (
                f"argv missing required safety flag {required_flag!r} when "
                f"capability {cap_name!r} is granted. Full argv: {argv}"
            )


def test_principle_capabilities_are_explicit_grants_apptainer() -> None:
    """AUDIT (H10): the docker twin above is the ONLY automated
    enforcement of the central capability-drop principle, leaving the apptainer
    adapter with zero regression coverage. Deleting '--drop-caps', 'all' from
    the apptainer adapter passed the entire suite. This pins the apptainer
    safety flags for every capability: --no-privs + adjacent --drop-caps all +
    --containall/--cleanenv present, and no escape-hatch flag (--fakeroot/
    --writable/--add-caps/--keep-privs/--allow-setuid).

    NOTE (S2): this covers the DIRECT/login apptainer path only.
    The HPC **sbatch** compute-node cage builds its own argv and BYPASSES this
    adapter (start.py --in-container) — it is pinned separately by
    `test_principle_hpc_sbatch_cage_carries_hardening` below. Both are "THE
    product (HPC)"; the sbatch one is the common case."""
    from botainer.adapters.apptainer import ApptainerAdapter
    from botainer.core.spec import (
        Bind,
        BindMode,
        CapabilityGrant,
        MountPlan,
        NetworkMode,
        NetworkSpec,
        Provenance,
        SessionSpec,
    )

    # Apptainer refuses NONE/endpoint-ip-allowlist (it can't enforce them); use
    # INTERNET so render_argv proceeds to the safety-flag rendering under test.
    forbidden = ["--fakeroot", "--writable", "--add-caps", "--keep-privs",
                 "--allow-setuid", "--writable-tmpfs", "--setuid", "--security"]
    plan = MountPlan(binds=(
        Bind(source="/host/proj", target="/workspace", mode=BindMode.RW,
             provenance=Provenance.CORE),
    ))
    for cap_name in all_capability_names():
        spec = SessionSpec(
            session_id="abcdef0123456789",
            project_uuid="11111111-1111-1111-1111-111111111111",
            project_root="/host/proj",
            state_dir="/state/dir",
            runtime="apptainer",
            image="/state/dir/images/x.sif",
            mount_plan=plan,
            network=NetworkSpec(mode=NetworkMode.INTERNET),
            capabilities=(CapabilityGrant(name=cap_name),),
        )
        argv = ApptainerAdapter().render_argv(spec)
        assert "--no-privs" in argv, (
            f"apptainer argv missing --no-privs for capability {cap_name!r}: {argv}"
        )
        assert "--containall" in argv and "--cleanenv" in argv, (
            f"apptainer argv missing --containall/--cleanenv for {cap_name!r}: {argv}"
        )
        # --drop-caps must be immediately followed by 'all' (not split/reordered).
        assert "--drop-caps" in argv, (
            f"apptainer argv missing --drop-caps for {cap_name!r}: {argv}"
        )
        di = argv.index("--drop-caps")
        assert di + 1 < len(argv) and argv[di + 1] == "all", (
            f"apptainer --drop-caps not followed by 'all' for {cap_name!r}: {argv}"
        )
        joined = " ".join(argv)
        for f in forbidden:
            assert f not in joined, (
                f"apptainer argv contains escape-hatch flag {f!r} for capability "
                f"{cap_name!r}; full argv: {argv}"
            )


def test_principle_hpc_sbatch_cage_carries_hardening(tmp_path) -> None:
    """HPC-parity principle (CLAUDE.md), EVOLVED by the compose-at-submit
    restructure (task #52): the sbatch compute-node cage — THE product path —
    no longer BYPASSES the ApptainerAdapter; it IS the adapter render, composed
    on the login node and stored on the plan as `agent_exec_argv`. So the cage
    carries the §4 NoNewPrivs + caps=0 hardening BY CONSTRUCTION (a single
    source, shared with the direct path — the `project_hpc_cage_bypasses_adapter`
    regression class is structurally dead). The frozen-plan __post_init__
    chokepoint is the never-regress backstop: it REFUSES any argv that lost a §4
    flag, carries an escape-hatch, or execs botainer in the container."""
    import importlib.util
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = repo / "plugins" / "hpc-launcher" / "host_helper" / "_common.py"
    spec = importlib.util.spec_from_file_location("hpc_launcher_common_pe", src)
    assert spec and spec.loader
    common = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_pe"] = common
    spec.loader.exec_module(common)

    # The adapter is the single source; the plan stores its render verbatim.
    good_argv = (
        "apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
        "--drop-caps", "all", "--bind=/home/u/proj:/workspace", "x.sif",
        "/usr/local/bin/agent-claude-entrypoint",
    )
    base = dict(
        project_root=tmp_path,
        project_uuid="11111111-1111-1111-1111-111111111111",
        state_root=tmp_path / "state",
        profile="default",
        partition="my-part",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="x.sif",
        submission_mode="submit",
        existing_jobid=None,
    )
    plan = common.SubmissionPlan(**base, agent_exec_argv=good_argv)
    argv = plan.to_apptainer_argv()
    assert "--containall" in argv and "--cleanenv" in argv, argv
    assert "--no-privs" in argv, f"sbatch cage lost NoNewPrivs (§4 regression): {argv}"
    di = argv.index("--drop-caps")
    assert argv[di + 1] == "all", f"sbatch cage must --drop-caps all: {argv}"
    forbidden = ["--fakeroot", "--writable", "--add-caps", "--keep-privs",
                 "--allow-setuid", "--writable-tmpfs", "--setuid"]
    joined = " ".join(argv)
    for f in forbidden:
        assert f not in joined, f"sbatch cage has escape-hatch flag {f!r}: {argv}"
    assert "botainer" not in argv and "--in-container" not in argv, argv

    # The never-regress chokepoint: each of these MUST refuse at construction.
    import pytest as _pytest
    bad_argvs = [
        # lost --no-privs
        ("apptainer", "exec", "--containall", "--cleanenv", "x.sif", "agent"),
        # lost --drop-caps all
        ("apptainer", "exec", "--containall", "--cleanenv", "--no-privs", "x.sif", "agent"),
        # execs botainer in the container (the whole point)
        ("apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
         "--drop-caps", "all", "x.sif", "botainer"),
        # not an apptainer exec invocation
        ("docker", "run", "x.sif", "agent"),
    ]
    for bad in bad_argvs:
        with _pytest.raises(SystemExit):
            common.SubmissionPlan(**base, agent_exec_argv=bad)


# ── plugins cannot bypass the env denylist ─────────────────────────


def test_principle_env_denylist_is_enforced_against_hostile_plugin(
    tmp_path,
) -> None:
    """Plugins must not be able to inject dynamic-loader vars
    (LD_PRELOAD, DYLD_INSERT_LIBRARIES, PYTHONPATH, etc.) into the
    container. These are the classic ways to hijack a process'
    behavior at exec time.

    Old test (J pattern — #275): asserted the
    _PLUGIN_ENV_BLOCKLIST_EXACT SET CONTAINED the bad names. Set
    membership without enforcement is decorative — a future refactor
    could leave the set in place and remove the check site.

    New test (this one): build an actual pre_session hook script that
    emits `{"env": {"LD_PRELOAD": "/tmp/evil.so"}}` as its contribution
    JSON; route it through composition.run_pre_session_hooks; assert
    `Refused(ENV_VAR_DENIED)`. Exercises the real refusal code path.
    """
    import os as _os

    from botainer.core import composition
    from botainer.core.refusal import RefusalCategory, Refused
    from botainer.core.spec import (
        EnvSpec,
        HookSpec,
        MountPlan,
        NetworkMode,
        NetworkSpec,
        SessionSpec,
    )
    from botainer.state import session_record as _sr

    # 1. Hostile hook script: prints a JSON contribution that tries to
    #    inject LD_PRELOAD into the agent's env.
    hook_path = tmp_path / "hostile_pre_session.py"
    hook_path.write_text(
        '#!/usr/bin/env python3\n'
        'import json\n'
        'print(json.dumps({"env": {"LD_PRELOAD": "/tmp/evil.so"}}))\n'
    )
    _os.chmod(hook_path, 0o755)

    # 2. Minimal SessionSpec wiring the hook + session record on disk.
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "abcdef0123456789"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(
        session_id="abcdef0123456789",
        project_uuid="u" * 32,
        project_root=str(tmp_path),
        state_dir=str(state_dir),
        runtime="mock",
        image="x@sha256:" + "a" * 64,
        mount_plan=MountPlan(binds=()),
        network=NetworkSpec(mode=NetworkMode.NONE),
        env=EnvSpec(values={}),
        hooks=(HookSpec(
            plugin="hostile-test", when="pre_session",
            script_path=str(hook_path),
        ),),
    )
    _sr.write(session_dir, _sr.from_spec(spec))

    # 3. Run hooks → must refuse with ENV_VAR_DENIED.
    import pytest as _pytest
    with _pytest.raises(Refused) as exc:
        composition.run_pre_session_hooks(spec)
    assert exc.value.category == RefusalCategory.ENV_VAR_DENIED, (
        f"expected ENV_VAR_DENIED, got {exc.value.category}. "
        f"message: {exc.value}"
    )
    assert "LD_PRELOAD" in str(exc.value)


def test_principle_credential_env_vars_are_blocked() -> None:
    """Plugins must NOT be able to read credential-shaped env vars
    from the parent shell. The login hook writes credentials to disk;
    the agent reads them from there. Inheritance is the attack surface
    we close.

    Note: this one DOES test set membership rather than behavior. The
    behavioral test —
    `tests/unit/test_hook_env_scrub.py::test_hook_does_not_receive_credential_env` —
    actually spawns a hook and confirms credential env vars are
    stripped from its subprocess env. This test is the documentation
    that the *named* credential families are in scope of the
    denylist set used by that scrubber.
    """
    required = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
    missing = required - _PLUGIN_ENV_BLOCKLIST_EXACT
    assert not missing, (
        f"credential env vars missing from denylist: {missing}. The "
        f"agent must read credentials from explicit on-disk files, "
        f"not from inherited shell env."
    )


# ── site policy constrains default_auth_mode (a ceiling) ───────────


def test_principle_default_auth_mode_is_constrained() -> None:
    """SitePolicy.default_auth_mode must accept only the canonical
    set. A typo or attacker-influenced policy file must not bypass
    auth dispatch by setting it to e.g. 'none'.
    """
    # Verify the validator rejects non-canonical values.
    import pydantic
    for bad in ("none", "permissive", "unrestricted", "any", "x"):
        try:
            SitePolicy(default_auth_mode=bad)
            raise AssertionError(
                f"default_auth_mode={bad!r} accepted; should be rejected."
            )
        except (ValueError, pydantic.ValidationError):
            pass  # expected
    # And the valid set works.
    for ok in ("", "isolated", "shared", "proxy"):
        SitePolicy(default_auth_mode=ok)


# ── no bind to host-private metadata / forbidden mount targets ─────


def test_principle_preflight_rejects_synthetic_umbrella_bind() -> None:
    """Construct a fake SessionSpec with the umbrella bind shape and
    confirm preflight.run() returns non-zero. This is the regression
    guard that the disaster would have hit at commit
    time.
    """

    from botainer.core.spec import (
        Bind,
        BindMode,
        EnvSpec,
        MountPlan,
        NetworkMode,
        NetworkSpec,
        Provenance,
        SessionSpec,
    )
    from botainer.inspect import preflight

    # Synthesize a spec that mimics the umbrella-bind shape.
    bad_state_root = "/home/test/.botainer"
    plan = MountPlan(
        binds=(
            Bind(
                source=bad_state_root,
                target=bad_state_root,
                mode=BindMode.RW,
                provenance=Provenance.PLUGIN,
                provenance_detail="synthetic umbrella for test",
            ),
        )
    )
    spec = SessionSpec(
        session_id="ses-test",
        project_uuid="u" * 32,
        project_root="/tmp/proj",
        image="test:0.1",
        runtime="mock",
        state_dir="/home/test/.botainer/state/uuuu",
        plugins_enabled=(),
        env=EnvSpec(values={}),
        mount_plan=plan,
        network=NetworkSpec(mode=NetworkMode.NONE),
    )
    rc = preflight.run(spec)
    assert rc != 0, (
        "preflight MUST refuse a spec that mimics the umbrella-bind "
        "disaster shape. If this test fails, preflight has regressed "
        "and the 2026-05-18 CRITICAL bug class is unguarded."
    )


def test_principle_preflight_rejects_docker_sock_bind() -> None:
    """Per CAPABILITY-SURFACE §5: /var/run/docker.sock is NEVER
    exposed to the agent. preflight should catch any attempt."""
    from botainer.core.spec import (
        Bind,
        BindMode,
        EnvSpec,
        MountPlan,
        NetworkMode,
        NetworkSpec,
        Provenance,
        SessionSpec,
    )
    from botainer.inspect import preflight

    plan = MountPlan(
        binds=(
            Bind(
                source="/var/run/docker.sock",
                target="/var/run/docker.sock",
                mode=BindMode.RW,
                provenance=Provenance.PLUGIN,
                provenance_detail="synthetic — agent must not see docker",
            ),
        )
    )
    spec = SessionSpec(
        session_id="ses-test",
        project_uuid="u" * 32,
        project_root="/tmp/proj",
        image="test:0.1",
        runtime="mock",
        state_dir="/home/test/.botainer/state/uuuu",
        plugins_enabled=(),
        env=EnvSpec(values={}),
        mount_plan=plan,
        network=NetworkSpec(mode=NetworkMode.NONE),
    )
    rc = preflight.run(spec)
    assert rc != 0


# ── CLAUDE.md: "Structure over rules; rules as documented backup" ──
#
# Clause 3 of that principle: make the enforcement UNFORGETTABLE — prefer a
# required parameter / single chokepoint / type over a convention a future caller
# has to remember. These pin the two structural mechanisms introduced by the
# audit round so a later refactor can't quietly soften them back into
# "remember to validate".


def test_hook_execution_cannot_silently_skip_the_containment_check() -> None:
    """`run_hook` must REQUIRE the writable-roots argument.

    If it ever gains a default, a future call site could omit it and silently
    lose the "host-executed code must not live where the agent can write"
    containment — the S3 escape. Omitting it must be a TypeError, not a bypass.
    """
    import inspect

    from botainer.plugins import hooks

    sig = inspect.signature(hooks.run_hook)
    p = sig.parameters.get("agent_writable_roots")
    assert p is not None, "run_hook lost its containment argument"
    assert p.default is inspect.Parameter.empty, (
        "agent_writable_roots gained a default — omitting it must be a TypeError, "
        "not a silent opt-out of the S3 containment check")


def test_hot_task_routing_does_not_consult_agent_supplied_id() -> None:
    """Structure, not a filter: the agent's `id` field must never become a path.

    route_hot_task takes the caller's filename-derived, already-validated job_id.
    If it ever reads request["id"] again, a hostile id is back in the path and we
    are relying on a charset filter instead of a property.
    """
    import inspect

    from botainer.hpc import pool

    sig = inspect.signature(pool.route_hot_task)
    assert "job_id" in sig.parameters, "route_hot_task lost its caller-supplied id"
    # AST, not string matching: the function's own comments deliberately quote
    # the old `request["id"]` expression to explain the bug.
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(pool.route_hot_task)))
    reads: list[str] = []
    for node in ast.walk(tree):
        # request["id"]
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == "request"):
            reads.append("request[...]")
        # request.get("id")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "request"):
            reads.append("request.get(...)")
    assert not reads, (
        f"route_hot_task reads the agent-supplied request again ({reads}) — the "
        f"path must be built only from the caller's validated job_id")


# ── "Recording a finding is not resolving it" ────────────────────────────────
# The two baseline RATCHETS that used to live here (deferred-claims count,
# weak-assertion count) moved to a maintainer-side suite that does not ship.
# Their subject is the development baselines under tools/dev/, absent from
# every distribution, so in this file they made the EXPORTED tree fail its own
# suite. The principle they enforce is unchanged; only the location is.
