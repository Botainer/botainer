"""git plugin — guarded-mode pre_session hook (AUDIT H6).

The plugin shipped with `hooks: []`, so its advertised guarded mode was a
no-op while the default config claimed `.git/hooks/` was protected. These tests
exercise the real hook end-to-end (subprocess) and pin the manifest/hook
envelope agreement, so "guarded mode protects the host" is enforced by a test
per CLAUDE.md's principles-are-tests rule.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
GIT_PLUGIN = REPO / "plugins" / "git"
HOOK = GIT_PLUGIN / "hooks" / "pre_session.py"


def _run_hook(project_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        capture_output=True, text=True,
        env={**os.environ, "BOTAINER_PROJECT_ROOT": str(project_root)},
    )


def _make_repo(tmp_path: Path, *, mode: str | None = "guarded",
               git_config: str | None = "[core]\n\trepositoryformatversion = 0\n",
               with_hooks: bool = True) -> Path:
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    cfg: dict = {"plugins": {"git": {}}}
    if mode is not None:
        cfg["plugins"]["git"]["mode"] = mode
    (proj / ".botainer" / "config.yaml").write_text(json.dumps(cfg))
    if git_config is not None or with_hooks:
        (proj / ".git").mkdir()
    if with_hooks:
        (proj / ".git" / "hooks").mkdir()
    if git_config is not None:
        (proj / ".git" / "config").write_text(git_config)
    return proj


def test_guarded_emits_readonly_overlays(tmp_path: Path) -> None:
    """Guarded mode + a git repo → ro overlays on BOTH .git/hooks and
    .git/config (the latter closes the runtime core.hooksPath-injection
    bypass)."""
    proj = _make_repo(tmp_path)
    r = _run_hook(proj)
    assert r.returncode == 0, r.stderr
    binds = json.loads(r.stdout)["binds"]
    by_target = {b["target"]: b for b in binds}
    assert by_target["/workspace/.git/hooks"]["mode"] == "ro"
    assert by_target["/workspace/.git/config"]["mode"] == "ro"
    assert by_target["/workspace/.git/hooks"]["source"] == str(proj / ".git" / "hooks")


@pytest.mark.parametrize("bad_config", [
    "[core]\n\thooksPath = /tmp/evil-hooks\n",
    "[core]\n\tsshCommand = /tmp/evil ssh\n",
    "[core]\n\tfsmonitor = /tmp/evil-monitor\n",
    "[core]\n\thookspath = hooks2\n",   # case-insensitive key, in-project path still refused
    "[core]\n\tpager = /tmp/evil\n",
    "[core]\n\teditor = /tmp/evil\n",
    "[core]\n\taskpass = /tmp/evil\n",
    "[sequence]\n\teditor = /tmp/evil\n",
    # adversarial-review HIGH: [include]/[includeIf] pull in an agent-writable
    # external file that can set core.hooksPath — bypasses both refusal+overlay.
    "[include]\n\tpath = ../evil.inc\n",
    '[includeIf "gitdir:/x"]\n\tpath = evil.inc\n',
    # adversarial-review MEDIUM: other host-exec keys in a pre-existing config.
    "[credential]\n\thelper = !/tmp/evil\n",
    '[credential "https://x"]\n\thelper = !/tmp/evil\n',
    "[gpg]\n\tprogram = /tmp/evil\n",
    '[filter "lfs"]\n\tclean = /tmp/evil\n',
    '[filter "lfs"]\n\tprocess = /tmp/evil\n',
    '[diff "x"]\n\tcommand = /tmp/evil\n',
    '[merge "x"]\n\tdriver = /tmp/evil\n',
    "[alias]\n\tst = !/tmp/evil.sh\n",   # shell-command alias
])
def test_guarded_refuses_dangerous_git_config(tmp_path: Path, bad_config: str) -> None:
    """A git-shareable repo whose .git/config already sets a host-code-exec key
    (or pulls one in via [include]) is refused (rc 2) — the overlay alone can't
    neutralize a pre-existing redirect, and the agent could write the included
    file."""
    proj = _make_repo(tmp_path, git_config=bad_config)
    r = _run_hook(proj)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "refusing" in r.stderr


def test_guarded_refuses_worktree_gitdir_pointer(tmp_path: Path) -> None:
    """adversarial-review HIGH: when .git is a FILE (worktree/submodule gitdir
    pointer) the real hooks live elsewhere and the overlay can't protect them.
    Guarded mode must fail CLOSED, not silently provide no protection."""
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        json.dumps({"plugins": {"git": {"mode": "guarded"}}})
    )
    (proj / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    r = _run_hook(proj)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "gitdir pointer" in r.stderr


@pytest.mark.parametrize("ok_config", [
    "[core]\n\tfsmonitor = false\n",   # boolean fsmonitor is safe (no program)
    "[core]\n\tfsmonitor = true\n",
    '[core]\n\tfsmonitor = "false"\n',
    "[core]\n\trepositoryformatversion = 0\n",
    "[alias]\n\tst = status\n",        # non-shell alias is safe
    '[remote "origin"]\n\turl = https://example.com/x.git\n',  # benign subsection
    "[branch \"main\"]\n\tremote = origin\n",
])
def test_guarded_allows_safe_git_config(tmp_path: Path, ok_config: str) -> None:
    """Safe config (incl. a boolean fsmonitor that merely disables/enables the
    builtin) is NOT false-rejected."""
    proj = _make_repo(tmp_path, git_config=ok_config)
    r = _run_hook(proj)
    assert r.returncode == 0, (r.returncode, r.stderr)


def test_off_mode_contributes_nothing(tmp_path: Path) -> None:
    proj = _make_repo(tmp_path, mode="off",
                      git_config="[core]\n\thooksPath = /tmp/evil\n")
    r = _run_hook(proj)
    assert r.returncode == 0, r.stderr
    # off mode opts out entirely — no overlays AND no refusal (user owns risk).
    assert json.loads(r.stdout)["binds"] == []


def test_non_git_project_contributes_nothing(tmp_path: Path) -> None:
    proj = _make_repo(tmp_path, git_config=None, with_hooks=False)
    # remove the .git dir entirely
    r = _run_hook(proj)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["binds"] == []


def test_hook_targets_are_within_manifest_envelope() -> None:
    """The targets the hook emits MUST be within the manifest's declared
    mount_target_prefixes, or composition's fail-closed envelope check would
    refuse the very overlay guarded mode depends on. Guards against drift
    between the hook and the manifest."""
    manifest = yaml.safe_load((GIT_PLUGIN / "botainer-plugin.yaml").read_text())
    prefixes = manifest["contributes"]["mount_target_prefixes"]
    for target in ("/workspace/.git/hooks", "/workspace/.git/config"):
        assert any(
            target == p or target.startswith(p.rstrip("/") + "/") for p in prefixes
        ), f"{target} not within manifest envelope {prefixes}"


def test_git_plugin_scanner_matches_shared_module() -> None:
    """DRIFT GUARD (sharp-edges re-audit): the git plugin keeps its OWN
    copy of the dangerous-key sets (it must stay self-contained — the hook may run
    under a python without botainer importable). This test fails if that copy ever
    diverges from the shared botainer.core.gitconfig_scan the HPC job path uses, so
    the two audited parsers can't silently drift apart."""
    import importlib.util

    from botainer.core import gitconfig_scan as shared
    spec = importlib.util.spec_from_file_location("git_presession_dg", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._DANGEROUS_KEYS == shared.DANGEROUS_KEYS
    assert mod._DANGEROUS_SUBSECTION_KEYS == shared.DANGEROUS_SUBSECTION_KEYS
    assert mod._DANGEROUS_SECTIONS == shared.DANGEROUS_SECTIONS
    assert mod._FSMONITOR_SAFE_VALUES == shared._FSMONITOR_SAFE_VALUES
