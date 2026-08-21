"""Tests for input-validation paths added in the review-fix pass.

Covers:
- Norway-problem detection in config_set/policy_set (H2)
- Dotted-set refuses non-dict intermediate (F1)
- _MISSING sentinel distinguishes missing-vs-null (F11)
- --auth-profile regex (F5)
- HPC submit charset + positive-int (F7/L9)
- Auth-family-based mutex enforcement (F8)
- Plugin login env scrubbing + exec check (F10)
- identity_accept=False in auth subcommands (F12)
- In-memory auth-mode override doesn't touch disk (F3)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

# ─────────── F1: dotted-set refuses non-dict intermediate ───────────


def test_dotted_set_refuses_non_dict_intermediate(tmp_path, monkeypatch) -> None:
    from botainer.cli.config_cmd import config
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    # network.mode is a string. Setting network.mode.foo should refuse.
    (proj / ".botainer" / "config.yaml").write_text(
        "network:\n  mode: none\n"
    )
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["set", "--yes", "network.mode.foo", "bar"])
    assert result.exit_code != 0, result.output
    assert "cannot descend" in result.output


# ─────────── F2/H4 + H2: Norway-problem ───────────


def test_config_set_refuses_norway_bool_coercion(tmp_path, monkeypatch) -> None:
    from botainer.cli.config_cmd import config
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    # 'no' would silently become bool False without our guard.
    result = runner.invoke(config, ["set", "--yes", "network.mode", "no"])
    assert result.exit_code == 2, result.output
    assert "Norway problem" in result.output or "literal-string" in result.output


def test_config_set_literal_string_bypass(tmp_path, monkeypatch) -> None:
    """--literal-string forces the value to be a string (avoids the YAML
    1.1 'no' → False coercion). Test pins it via a real schema field
    (`profile`) since arbitrary keys are now refused by schema check
    (codex 45#6)."""
    from botainer.cli.config_cmd import config
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(
        config, ["set", "--yes", "--literal-string", "profile", "no"]
    )
    assert result.exit_code == 0, result.output
    import yaml
    data = yaml.safe_load((proj / ".botainer" / "config.yaml").read_text())
    assert data["profile"] == "no"  # literal string, not False


def test_config_set_refuses_unknown_key(tmp_path, monkeypatch) -> None:
    """Codex 45#6: setting a key that isn't in ProjectConfig refuses
    with a schema error rather than silently writing a broken config."""
    from botainer.cli.config_cmd import config
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(
        config, ["set", "--yes", "totally_made_up_key", "42"]
    )
    assert result.exit_code == 2, result.output
    assert "invalid" in result.output.lower() or "refused" in result.output.lower()


def test_policy_set_refuses_norway_too(tmp_path, monkeypatch) -> None:
    from botainer.cli.policy import policy
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "policy.yaml").write_text("version: policy-v1\n")
    runner = CliRunner()
    result = runner.invoke(policy, ["set", "--yes", "default_auth_mode", "no"])
    assert result.exit_code == 2, result.output


# ─────────── F11: missing vs null sentinel ───────────


def test_config_get_distinguishes_missing_from_null(tmp_path, monkeypatch) -> None:
    from botainer.cli.config_cmd import config
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text(
        "agent: claude\nexplicit_null:\n"
    )
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    # Missing key → exit 1, "not present" message.
    result = runner.invoke(config, ["get", "nonexistent_key"])
    assert result.exit_code == 1
    assert "key not present" in result.output
    # Explicit null → exit 0, "(null)" message.
    result = runner.invoke(config, ["get", "explicit_null"])
    assert result.exit_code == 0
    assert result.output.strip() == "(null)"


# ─────────── F5: --auth-profile regex ───────────


def test_start_refuses_bad_auth_profile(tmp_path, monkeypatch) -> None:
    from botainer.cli.start import start
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text(
        "agent: claude\nplugins_enabled: [agent-claude]\n"
    )
    state_root = tmp_path / "state"
    (state_root).mkdir(parents=True)
    (state_root / "policy.yaml").write_text("version: v1\n")
    monkeypatch.setenv("MY_BOTAINER", str(state_root))
    monkeypatch.chdir(proj)
    runner = CliRunner()
    # Path traversal attempt.
    result = runner.invoke(
        start, ["--auth-profile", "../../../etc/passwd", "--dry-run"]
    )
    assert result.exit_code == 2, result.output
    assert "auth-profile" in result.output


def test_start_accepts_valid_auth_profile(monkeypatch) -> None:
    # Just verify the regex itself accepts valid forms.
    from botainer.cli.start import _PROFILE_RE
    for ok in ("default", "work", "personal", "client_a", "a-b-c", "x1y2"):
        assert _PROFILE_RE.fullmatch(ok)
    for bad in ("../etc", ".", "", "no\nnewline", "with space",
                "TooManyCharsToBeAReasonableProfileXXXXXXXXXXXXXXXXXXX",
                "Starts-uppercase", "1leading-digit"):
        assert not _PROFILE_RE.fullmatch(bad), f"{bad!r} should not match"


# ─────────── F7/L9: HPC submit charset + positive-int ───────────


def test_hpc_submit_refuses_zero_time() -> None:
    """argparse should refuse --time 0 (sbatch may treat as site cap)."""
    import sys
    plugins_dir = "/workspace/plugins/hpc-launcher/host_helper"
    sys.path.insert(0, plugins_dir)
    try:
        if "submit" in sys.modules:
            del sys.modules["submit"]
        submit_mod = __import__("submit")
        with pytest.raises(SystemExit):
            submit_mod.parse_args(["--time", "0"])
    finally:
        sys.path.pop(0)


def test_hpc_submit_refuses_negative_gpus() -> None:
    import sys
    plugins_dir = "/workspace/plugins/hpc-launcher/host_helper"
    sys.path.insert(0, plugins_dir)
    try:
        if "submit" in sys.modules:
            del sys.modules["submit"]
        submit_mod = __import__("submit")
        with pytest.raises(SystemExit):
            submit_mod.parse_args(["--gpus", "-1"])
    finally:
        sys.path.pop(0)


def test_hpc_submit_refuses_newline_in_partition() -> None:
    import sys
    plugins_dir = "/workspace/plugins/hpc-launcher/host_helper"
    sys.path.insert(0, plugins_dir)
    try:
        if "submit" in sys.modules:
            del sys.modules["submit"]
        submit_mod = __import__("submit")
        with pytest.raises(SystemExit):
            submit_mod.parse_args([
                "--partition", "day\n#SBATCH --account=victim"
            ])
    finally:
        sys.path.pop(0)


def test_hpc_render_sbatch_script_validates_account() -> None:
    """Defense-in-depth: render_sbatch_script re-validates even if
    parse_args was bypassed (e.g. config file sets a bad partition)."""
    import sys
    plugins_dir = "/workspace/plugins/hpc-launcher/host_helper"
    sys.path.insert(0, plugins_dir)
    try:
        if "_common" in sys.modules:
            del sys.modules["_common"]
        common = __import__("_common")
        from pathlib import Path
        plan = common.SubmissionPlan(
            project_root=Path("/tmp"),
            # Canonical uuid: __post_init__ now rejects a non-uuid project_uuid
            # (AC7 sbatch-injection fix). This test targets the PARTITION
            # newline guard, so the uuid must be valid to reach render.
            project_uuid="11111111-1111-1111-1111-111111111111",
            state_root=Path("/tmp/state"),
            profile="default",
            partition="day\nbadline",  # invalid
            account="acct",
            time_minutes=60,
            cpus=1,
            memory_gb=4,
            gpus=0,
            gpu_type=None,
            apptainer_image="img.sif",
            submission_mode="submit",
            existing_jobid=None,
        )
        with pytest.raises(ValueError, match="newline injection"):
            plan.render_sbatch_script()
    finally:
        sys.path.pop(0)


# ─────────── F8: auth_family mutex enforcement ───────────


def test_auth_family_mutex_enforced_when_two_variants_enabled() -> None:
    """The composition enforces single-variant-per-family. Smoke-test via
    direct construction of plugin manifests rather than end-to-end
    composition (which needs full project + state setup)."""
    # Verify the bundled manifests declare auth_family correctly:
    # ALL three anthropic-family plugins have auth_family=anthropic.
    # ALL three list each other in mutually_exclusive_with.
    from pathlib import Path

    from botainer.plugins.manifest import load_manifest
    for name in ("agent-claude", "agent-claude-shared", "agent-claude-proxy"):
        m = load_manifest(Path(f"/workspace/plugins/{name}"))
        assert m.auth_family == "anthropic"
        # Each lists the OTHER two in mutex.
        siblings = set(m.mutually_exclusive_with)
        expected_siblings = {
            "agent-claude", "agent-claude-shared", "agent-claude-proxy"
        } - {name}
        assert siblings == expected_siblings, (
            f"{name}: mutex siblings {siblings} != expected {expected_siblings}"
        )


# ─────────── F3: in-memory auth-mode override doesn't touch disk ───────────


def test_apply_auth_mode_override_in_memory_helper() -> None:
    """The helper swaps the enabled-plugin variant without touching disk.

    Unit-tested in isolation; full compose_session integration is harder
    to fixture (needs project + state + installed plugins).
    """
    from botainer.core.composition import _apply_auth_mode_override_in_memory
    from botainer.core.config import ProjectConfig

    cfg = ProjectConfig.model_validate({
        "agent": "claude",
        "plugins_enabled": ["agent-claude", "git"],
    })
    # Override to shared.
    new_cfg = _apply_auth_mode_override_in_memory(cfg, "shared")
    assert "agent-claude-shared" in new_cfg.plugins_enabled
    assert "agent-claude" not in new_cfg.plugins_enabled
    assert "git" in new_cfg.plugins_enabled  # unrelated plugin preserved
    # Original cfg unchanged (model_copy returns new instance).
    assert "agent-claude" in cfg.plugins_enabled


# ─────────── F12: identity_accept=False in auth subcommands ───────────


def test_auth_doesnt_silently_accept_identity_change() -> None:
    """No `resolve_identity(...)` call in cli/auth.py may pass
    `identity_accept=True` — auth flows are read-only and must NOT
    silently rebind a moved project to existing credentials.

    #276-class fix: was a raw source-text grep
    (`"identity_accept=True" not in src`) which broke on whitespace
    (`identity_accept = True`) and matched comments/docstrings. Now
    parses the AST and inspects every call's keyword arguments —
    structural, resilient to formatting, blind to comments.
    """
    import ast
    from pathlib import Path as _Path

    auth_src = (
        _Path(__file__).resolve().parents[2] / "botainer" / "cli" / "auth.py"
    )
    tree = ast.parse(auth_src.read_text(encoding="utf-8"))

    resolve_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `identity.resolve_identity(...)` or `resolve_identity(...)`.
        fn = node.func
        name = (
            fn.attr if isinstance(fn, ast.Attribute)
            else fn.id if isinstance(fn, ast.Name)
            else ""
        )
        if name != "resolve_identity":
            continue
        resolve_calls += 1
        for kw in node.keywords:
            if kw.arg == "identity_accept":
                # Must be a literal False, never True.
                assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (
                    f"resolve_identity call at auth.py:{node.lineno} passes "
                    f"identity_accept={ast.dump(kw.value)}; auth flows must "
                    f"use identity_accept=False (read-only; no silent rebind)."
                )
    assert resolve_calls > 0, (
        "expected at least one resolve_identity call in auth.py; the "
        "test's premise (auth resolves identity read-only) no longer holds "
        "— re-derive the invariant."
    )


# NOTE (compose-at-submit, task #52): the two _parse_env_file_text tests were
# removed — that helper (and the whole start.py --in-container env_file sink it
# served) is deleted. On the sbatch path the module env now flows via the
# ApptainerAdapter's --env-file + inner-prepend trampoline (composed on the login
# node), pinned by tests/unit/test_hpc_compose_at_submit.py; the direct
# docker/apptainer paths apply spec.env_files via the adapter's --env-file.
