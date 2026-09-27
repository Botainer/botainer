"""Advisories use native installed-plugin selection without changing projects."""
from __future__ import annotations

import errno
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
import yaml
from click.testing import CliRunner

from botainer.cli import _common, _history_prompt, hpc
from botainer.core import composition
from botainer.core.refusal import RefusalCategory, Refused
from botainer.plugins import lifecycle
from botainer.state import dir as state_dir


@pytest.fixture
def selection(tmp_path, monkeypatch):
    project = tmp_path / "project"
    metadata = project / ".botainer"
    metadata.mkdir(parents=True)
    uid = str(uuid.uuid4())
    (metadata / "project-id").write_text(uid)
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("MY_BOTAINER", str(root))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    monkeypatch.setattr(state_dir, "ensure_user_state_dir",
                        lambda **kwargs: SimpleNamespace(root=root))
    plugins = Path(composition.__file__).resolve().parents[2] / "plugins"
    available = {p.name for p in plugins.iterdir()
                 if p.name.startswith("agent-") or p.name == "hpc-launcher"}
    monkeypatch.setattr(lifecycle, "list_installed", lambda: [
        SimpleNamespace(name=name, plugin_dir=plugins / name)
        for name in sorted(available)])
    # A selection-only advisory must never compose or invoke launch hooks.
    def forbidden(*args, **kwargs):
        pytest.fail("an advisory attempted runtime composition")
    monkeypatch.setattr(composition, "compose_session", forbidden)
    monkeypatch.setattr(composition, "compose_agent_exec_for_hpc", forbidden)

    def configure(agent, enabled, profile="default"):
        (metadata / "config.yaml").write_text(yaml.safe_dump({
            "version": "config-v1", "agent": agent, "profile": profile,
            "runtime": "apptainer", "plugins_enabled": enabled,
            "network": {"mode": "internet"}}))
        for family in ("claude", "codex"):
            history = root / "state" / uid / "data" / ("agent-" + family) / "profiles" / profile
            history.mkdir(parents=True, exist_ok=True)
            (history / "history.jsonl").write_text('{"message":"synthetic history"}\n')
        return project

    def snapshot():
        return {str(p.relative_to(tmp_path)): p.read_bytes()
                for p in tmp_path.rglob("*") if p.is_file()}

    return SimpleNamespace(project=project, root=root, uid=uid,
                           available=available, configure=configure, snapshot=snapshot)


def warning(project, **kwargs):
    # Replay the baseline's real caller as well: before this fix it could not
    # pass an agent override. Omitting only that unsupported keyword exposes
    # the wrong history path rather than stopping at a signature TypeError.
    import inspect
    if "agent_override" not in inspect.signature(_history_prompt.warn_override_moves_history).parameters:
        kwargs.pop("agent_override", None)
    @click.command()
    def command():
        _history_prompt.warn_override_moves_history(project, **kwargs)
    result = CliRunner().invoke(command, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize("saved,target,target_plugin,mode,profile,subdir", [
    ("claude", "codex", "agent-codex-shared", None, "work", "profiles"),
    ("codex", "claude", "agent-claude-shared", "broker", "work", "broker-state"),
    ("claude", "codex", "agent-codex-broker", None, "work", "broker-state"),
    ("codex", "claude", "agent-claude-broker", "isolated", "default", "profiles"),
])
def test_combined_overrides_disclose_the_selected_family_and_actual_mode(
        selection, saved, target, target_plugin, mode, profile, subdir):
    project = selection.configure(saved, [f"agent-{saved}-shared", target_plugin], "saved")
    before = selection.snapshot()
    text = warning(project, agent_override=target, auth_mode_override=mode,
                   auth_profile_override=profile)
    expected = selection.root / "state" / selection.uid / "data" / f"agent-{target}" / subdir / profile
    declared = selection.root / "state" / selection.uid / "data" / f"agent-{saved}" / "profiles" / "saved"
    assert f"this session reads:  {expected}" in text
    assert f"your config's is:    {declared}" in text
    assert "separate histories" in text
    assert "offer to carry" not in text
    assert selection.snapshot() == before


@pytest.mark.parametrize("agent,from_plugin,mode,profile,warns", [
    ("claude", "agent-claude-shared", "isolated", None, False),
    ("codex", "agent-codex-shared", "broker", None, True),
    ("claude", "agent-claude-broker", "shared", None, True),
    ("codex", "agent-codex-shared", None, "default", True),
    ("claude", "agent-claude-shared", None, None, False),
])
def test_same_agent_mode_and_profile_controls(selection, agent, from_plugin, mode, profile, warns):
    project = selection.configure(agent, [from_plugin], "saved")
    before = selection.snapshot()
    text = warning(project, auth_mode_override=mode, auth_profile_override=profile)
    assert ("DIFFERENT history directory" in text) is warns
    if profile is not None:
        assert f"profiles/{profile}" in text
    assert selection.snapshot() == before


def test_missing_auth_variant_does_not_claim_an_unselected_history_path(selection):
    project = selection.configure("claude", ["agent-claude-shared"])
    selection.available.remove("agent-claude-broker")
    before = selection.snapshot()
    text = warning(project, auth_mode_override="broker", auth_profile_override=None)
    assert "DIFFERENT history directory" not in text
    assert "broker-state" not in text
    assert selection.snapshot() == before


def test_unavailable_agent_leaves_native_refusal_to_composition(selection):
    project = selection.configure("claude", ["agent-claude-shared"])
    selection.available.difference_update(n for n in list(selection.available) if n.startswith("agent-codex"))
    before = selection.snapshot()
    text = warning(project, agent_override="codex", auth_mode_override="broker",
                   auth_profile_override="work")
    assert text == ""
    assert selection.snapshot() == before


def test_agent_only_override_explains_separate_histories(selection):
    project = selection.configure("claude", ["agent-claude-shared", "agent-codex-shared"])
    text = warning(project, agent_override="codex", auth_mode_override=None,
                   auth_profile_override=None)
    assert "agent-codex/profiles/default" in text
    assert "separate histories" in text


def test_unreadable_history_is_not_reported_as_empty(selection, monkeypatch):
    project = selection.configure("codex", ["agent-codex-shared"])
    def unreadable(path, *, strict=False):
        raise PermissionError("synthetic unreadable history")
    monkeypatch.setattr(_history_prompt, "has_history", unreadable)
    before = selection.snapshot()
    text = warning(project, auth_mode_override="broker", auth_profile_override=None)
    assert "DIFFERENT history directory" in text
    assert "History availability is unknown" in text
    assert "EMPTY" not in text
    assert selection.snapshot() == before


@pytest.mark.parametrize("entry_kind", [
    "file-link", "directory-link", "credential-directory-link", "missing-link", "fifo",
])
def test_withheld_history_entries_are_unknown_not_empty(selection, tmp_path, entry_kind):
    project = selection.configure("codex", ["agent-codex-shared"])
    selected = (selection.root / "state" / selection.uid / "data" /
                "agent-codex" / "broker-state" / "default")
    selected.mkdir(parents=True)
    target = tmp_path / "linked-history"
    if entry_kind in {"directory-link", "credential-directory-link"}:
        target.mkdir()
        (target / "transcript.jsonl").write_text("synthetic history\n")
    elif entry_kind != "missing-link":
        target.write_text("synthetic history\n")
    name = ".credentials.json" if entry_kind == "credential-directory-link" else "history.jsonl"
    entry = selected / name
    if entry_kind == "fifo":
        os.mkfifo(entry)
    else:
        entry.symlink_to(target, target_is_directory=target.is_dir())

    text = warning(project, auth_mode_override="broker", auth_profile_override=None)

    assert "History availability is unknown" in text
    assert "EMPTY" not in text
    assert entry.exists() or entry.is_symlink(), "the advisory must not remove an unassessed entry"


def test_a_known_credential_file_link_does_not_count_as_history(selection, tmp_path):
    project = selection.configure("codex", ["agent-codex-shared"])
    selected = (selection.root / "state" / selection.uid / "data" /
                "agent-codex" / "broker-state" / "default")
    selected.mkdir(parents=True)
    credential = tmp_path / "synthetic-credential.json"
    credential.write_text("{}\n")
    (selected / ".credentials.json").symlink_to(credential)

    text = warning(project, auth_mode_override="broker", auth_profile_override=None)

    assert "EMPTY" in text
    assert "History availability is unknown" not in text
    assert credential.read_text() == "{}\n"


@pytest.mark.parametrize("root_kind", ["missing-directory", "directory-link", "missing-link"])
def test_history_root_links_are_unknown_and_missing_directory_is_empty(selection, tmp_path, root_kind):
    project = selection.configure("codex", ["agent-codex-shared"])
    selected = (selection.root / "state" / selection.uid / "data" /
                "agent-codex" / "broker-state" / "default")
    selected.parent.mkdir(parents=True)
    if root_kind != "missing-directory":
        target = tmp_path / "linked-profile"
        if root_kind == "directory-link":
            target.mkdir()
        selected.symlink_to(target, target_is_directory=True)

    text = warning(project, auth_mode_override="broker", auth_profile_override=None)

    if root_kind == "missing-directory":
        assert "EMPTY" in text
        assert "History availability is unknown" not in text
        assert not selected.exists()
    else:
        assert "History availability is unknown" in text
        assert "EMPTY" not in text
        assert selected.is_symlink()


@pytest.mark.parametrize("side", ["declared", "selected"])
@pytest.mark.parametrize("depth", ["root", "nested"])
def test_history_scan_error_reports_unknown_through_real_helper(
        selection, monkeypatch, side, depth):
    project = selection.configure("codex", ["agent-codex-shared"])
    base = selection.root / "state" / selection.uid / "data" / "agent-codex"
    declared = base / "profiles" / "default"
    selected = base / "broker-state" / "default"
    selected.mkdir(parents=True)
    unreadable = declared if side == "declared" else selected
    if side == "selected":
        (selected / "history.jsonl").write_text("synthetic selected history\n")
    if depth == "nested":
        nested = unreadable / "archive"
        nested.mkdir()
        (unreadable / "history.jsonl").rename(nested / "history.jsonl")
        unreadable = nested
    before = selection.snapshot()
    native_scandir = os.scandir
    observed = []

    def scandir(path):
        if Path(path) == unreadable:
            observed.append(Path(path))
            raise PermissionError(errno.EACCES, "synthetic unreadable history", str(path))
        return native_scandir(path)

    # Intercept the filesystem boundary under real os.walk/has_history, not
    # has_history itself: os.walk normally swallows this PermissionError.
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        text = warning(project, auth_mode_override="broker", auth_profile_override=None)
    assert observed == [unreadable]
    assert "DIFFERENT history directory" in text
    assert "History availability is unknown" in text
    assert "EMPTY" not in text
    assert selection.snapshot() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="permission refusal requires an unprivileged process")
def test_unreadable_directory_reports_unknown_without_mocking_filesystem(selection):
    project = selection.configure("codex", ["agent-codex-shared"])
    selected = (selection.root / "state" / selection.uid / "data" /
                "agent-codex" / "broker-state" / "default")
    selected.mkdir(parents=True)
    (selected / "history.jsonl").write_text("synthetic retained history\n")
    before = selection.snapshot()
    original_mode = selected.stat().st_mode & 0o777
    try:
        selected.chmod(0)
        with pytest.raises(PermissionError):
            list(selected.iterdir())
        text = warning(project, auth_mode_override="broker", auth_profile_override=None)
    finally:
        selected.chmod(original_mode)
    assert "DIFFERENT history directory" in text
    assert "History availability is unknown" in text
    assert "EMPTY" not in text
    assert selection.snapshot() == before


def separate_stderr_runner():
    # Click <8.2 needs this opt-in; newer Click always captures stderr and
    # removed the constructor option. Support the project's Click >=8.0 range.
    import inspect

    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def invoke_submit(selection, monkeypatch, flags, *, decline=False, forwarded_exit=0):
    calls, families = [], []
    monkeypatch.setattr(_common, "find_project_root", lambda: selection.project)
    monkeypatch.setattr(hpc, "_refuse_unsupported_scheduler", lambda *args: None)
    monkeypatch.setattr(hpc, "_warn_if_jobs_without_dispatcher", lambda *args: None)
    def confirm(project, *, agent_family):
        assert project == selection.project
        families.append(agent_family)
        if decline:
            raise click.Abort()
    monkeypatch.setattr(_common, "confirm_no_other_shared_session", confirm)
    monkeypatch.setattr(hpc.subprocess, "call", lambda argv, **kwargs: calls.append(argv) or forwarded_exit)
    result = separate_stderr_runner().invoke(hpc.submit, flags)
    return result, families, calls


@pytest.mark.parametrize("saved,enabled,flags,expected", [
    ("claude", ["agent-claude-shared"], ["--agent", "codex"], "codex"),
    ("codex", ["agent-codex-shared"], ["--agent", "claude"], "claude"),
    ("claude", ["agent-claude-shared"], ["--auth-mode", "isolated"], None),
    ("claude", ["agent-claude"], ["--agent", "codex", "--auth-mode", "shared"], "codex"),
    ("codex", ["agent-claude-shared", "agent-codex-shared"], [], "codex"),
    ("claude", ["agent-claude-shared", "agent-codex-broker"], ["--agent", "codex"], None),
    ("claude", ["agent-claude-shared"], [], "claude"),
    ("codex", ["agent-codex-shared"], ["--auth-profile", "work"], "codex"),
])
def test_hpc_shared_holder_advisory_uses_effective_selection(
        selection, monkeypatch, saved, enabled, flags, expected):
    selection.configure(saved, enabled)
    before = selection.snapshot()
    result, families, calls = invoke_submit(selection, monkeypatch, flags)
    assert result.exit_code == 0, (result.output, result.exception)
    assert families == ([] if expected is None else [expected])
    assert len(calls) == 1
    for flag in ("--agent", "--auth-mode", "--auth-profile"):
        if flag in flags:
            index = calls[0].index(flag)
            assert calls[0][index + 1] == flags[flags.index(flag) + 1]
    assert selection.snapshot() == before


def test_hpc_decline_keeps_native_confirmation_before_dispatch(selection, monkeypatch):
    selection.configure("claude", ["agent-claude-shared"])
    before = selection.snapshot()
    result, families, calls = invoke_submit(selection, monkeypatch, ["--agent", "codex"], decline=True)
    assert result.exit_code != 0
    assert families == ["codex"]
    assert calls == []
    assert selection.snapshot() == before


def test_hpc_unavailable_auth_variant_retains_actual_shared_family(selection, monkeypatch):
    selection.configure("claude", ["agent-claude-shared"])
    selection.available.remove("agent-claude-broker")
    result, families, calls = invoke_submit(selection, monkeypatch, ["--auth-mode", "broker"])
    assert result.exit_code == 0, result.output
    assert families == ["claude"]
    assert len(calls) == 1


def test_start_forwards_agent_override_to_history_disclosure():
    # The real start callback is large and performs launch/login work. Inspect
    # only its actual call expression to guard this argument plumbing; the
    # behavior above exercises the real selection resolver and warning helper.
    import ast

    from botainer.cli import start
    source = Path(start.__file__)
    tree = ast.parse(source.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "warn_override_moves_history"]
    assert len(calls) == 1
    values = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(values.get("agent_override"), ast.Name)
    assert values["agent_override"].id == "agent_override"


_HPC_ADVISORY_WARNING = (
    "Warning: the shared-credential session check could not be evaluated; "
    "continuing without this advisory."
)


@pytest.mark.parametrize("forwarded_exit", [0, 2])
def test_hpc_unexpected_selection_error_warns_and_preserves_native_result(
        selection, monkeypatch, forwarded_exit):
    selection.configure("claude", ["agent-claude-shared"])
    before = selection.snapshot()
    directories = sorted(p.relative_to(selection.root.parent).as_posix()
                         for p in selection.root.parent.rglob("*") if p.is_dir())
    def fail_selection(*args, **kwargs):
        click.echo("sensitive preview diagnostic", err=True)
        raise RuntimeError("sensitive resolver exception")
    monkeypatch.setattr(composition, "apply_plugin_overrides", fail_selection)
    def no_state_write(*args, **kwargs):
        pytest.fail("advisory failure attempted a state write")
    monkeypatch.setattr(state_dir, "ensure_project_dirs", no_state_write)
    monkeypatch.setattr(state_dir, "write_meta", no_state_write)

    result, families, calls = invoke_submit(
        selection, monkeypatch, ["--agent", "codex"], forwarded_exit=forwarded_exit)

    # The caller reveals only a fixed warning, not arbitrary preview details.
    assert result.stderr == _HPC_ADVISORY_WARNING + "\n"
    assert result.stdout == ""
    assert "sensitive" not in result.stdout + result.stderr
    assert families == []
    # Forwarding and its exit status are observed separately from disclosure.
    # This is a captured call, not a real plugin submission or scheduler job.
    assert calls == [[sys.executable, "-I", "-B", "-m", "botainer.cli.main",
                      "plugin", "hpc-launcher", "submit", "--agent", "codex"]]
    assert result.exit_code == forwarded_exit, result.exception
    assert selection.snapshot() == before
    assert sorted(p.relative_to(selection.root.parent).as_posix()
                  for p in selection.root.parent.rglob("*") if p.is_dir()) == directories


@pytest.mark.parametrize("forwarded_exit", [0, 2])
def test_hpc_expected_selection_refusal_is_quiet_and_preserves_captured_forwarding(
        selection, monkeypatch, forwarded_exit):
    selection.configure("claude", ["agent-claude-shared"])
    before = selection.snapshot()
    def refuse_selection(*args, **kwargs):
        click.echo("preview-only refusal diagnostic", err=True)
        raise Refused(RefusalCategory.CONFIG_INVALID, "preview-only refusal detail")
    monkeypatch.setattr(composition, "apply_plugin_overrides", refuse_selection)

    result, families, calls = invoke_submit(
        selection, monkeypatch, ["--agent", "codex"], forwarded_exit=forwarded_exit)

    assert result.stderr == ""
    assert result.stdout == ""
    assert families == []
    # Only the captured native call and its return code are observed here;
    # no child diagnostics, plugin submission or scheduler job are exercised.
    assert calls == [[sys.executable, "-I", "-B", "-m", "botainer.cli.main",
                      "plugin", "hpc-launcher", "submit", "--agent", "codex"]]
    assert result.exit_code == forwarded_exit, result.exception
    assert selection.snapshot() == before


@pytest.mark.parametrize("answer,continues", [("y", True), ("n", False)])
def test_hpc_successful_preview_preserves_real_shared_session_confirmation(
        selection, monkeypatch, answer, continues):
    from botainer.state import credential_events

    selection.configure("claude", ["agent-claude-shared"])
    before = selection.snapshot()
    monkeypatch.setattr(_common, "find_project_root", lambda: selection.project)
    monkeypatch.setattr(hpc, "_refuse_unsupported_scheduler", lambda *args: None)
    monkeypatch.setattr(hpc, "_warn_if_jobs_without_dispatcher", lambda *args: None)
    holder_reads, calls = [], []
    def holders(paths, *, exclude_uuid, agent_family):
        holder_reads.append((exclude_uuid, agent_family))
        return [{"project_root": "synthetic-other-project"}]
    monkeypatch.setattr(credential_events, "live_shared_holders", holders)
    real_confirm = _common.confirm_no_other_shared_session
    def interactive_confirm(*args, **kwargs):
        # CliRunner's input stream is normally non-interactive. Keep the real
        # holder advisory and Click prompt, with a synthetic terminal/answer.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        return real_confirm(*args, **kwargs)
    monkeypatch.setattr(_common, "confirm_no_other_shared_session", interactive_confirm)
    monkeypatch.setattr(hpc.subprocess, "call", lambda argv, **kwargs: calls.append(argv) or 0)

    result = separate_stderr_runner().invoke(hpc.submit, ["--agent", "codex"], input=answer + "\n")
    # Older Click's result.output omits separately captured stderr.
    output = result.stdout + result.stderr

    assert holder_reads == [(selection.uid, "codex")]
    assert "Another codex session" in result.stderr
    assert "Start anyway and log the other session out?" in output
    assert _HPC_ADVISORY_WARNING not in output
    if continues:
        assert result.exit_code == 0, (output, result.exception)
        assert calls == [[sys.executable, "-I", "-B", "-m", "botainer.cli.main",
                          "plugin", "hpc-launcher", "submit", "--agent", "codex"]]
    else:
        assert result.exit_code != 0
        assert "Not started. The other session keeps the credential." in output
        assert calls == []
    assert selection.snapshot() == before
