"""Regression test: login-container bind & env surface.

Pins the contract documented in docs/CAPABILITY-SURFACE.md §2a
(and the docker analog) as code. Same shape as
test_capability_surface_matches_inventory.py — exists because the
umbrella-bind disaster proved that "principle in prose,
nothing in tests" reliably ships regressions.

Imported via importlib because plugin dir names have dashes.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_LOGIN = REPO_ROOT / "plugins" / "agent-claude-shared" / "hooks" / "login.py"
ISOLATED_LOGIN = REPO_ROOT / "plugins" / "agent-claude" / "hooks" / "login.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SHARED = _load_module("login_shared_under_test", SHARED_LOGIN)
ISOLATED = _load_module("login_isolated_under_test", ISOLATED_LOGIN)


# ─────────────────────── apptainer argv: bind surface ───────────────────────


def test_shared_apptainer_argv_has_single_bind_to_out(tmp_path: Path) -> None:
    creds_dir = tmp_path / "shared-auth" / "agent-claude"
    sif = tmp_path / "images" / "botainer-agent-claude.sif"
    argv = SHARED.build_apptainer_argv("/usr/bin/apptainer", sif, creds_dir)

    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert binds == [f"{creds_dir}:/out"], (
        f"Expected exactly one --bind to /out, got: {binds}"
    )


def test_isolated_apptainer_argv_has_single_bind_to_out(tmp_path: Path) -> None:
    creds_dir = tmp_path / "state" / "uuid" / "data" / "agent-claude" / "profiles" / "default"
    sif = tmp_path / "images" / "botainer-agent-claude.sif"
    argv = ISOLATED.build_apptainer_argv("/usr/bin/apptainer", sif, creds_dir)

    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert binds == [f"{creds_dir}:/out"]


def test_apptainer_argv_never_binds_state_root_umbrella(tmp_path: Path) -> None:
    """The disaster: a bind that exposed all projects'
    state. Check that no bind has the state_root as source."""
    state_root = tmp_path / ".botainer"
    creds_dir_shared = state_root / "shared-auth" / "agent-claude"
    creds_dir_isolated = (
        state_root / "state" / "uuid" / "data" / "agent-claude" / "profiles" / "default"
    )
    sif = state_root / "images" / "botainer-agent-claude.sif"

    for mod, creds in [
        (SHARED, creds_dir_shared),
        (ISOLATED, creds_dir_isolated),
    ]:
        argv = mod.build_apptainer_argv("/usr/bin/apptainer", sif, creds)
        for i, a in enumerate(argv):
            if a == "--bind":
                source = argv[i + 1].split(":", 1)[0]
                assert source != str(state_root), (
                    f"Umbrella bind detected in {mod.__name__}: --bind {source}"
                )
                # Source must be strictly under state_root (not state_root itself
                # nor a sibling); strictly creds_dir.
                assert source == str(creds), (
                    f"Unexpected bind source in {mod.__name__}: {source} != {creds}"
                )


def test_apptainer_argv_has_containall_and_cleanenv(tmp_path: Path) -> None:
    argv = SHARED.build_apptainer_argv(
        "/usr/bin/apptainer",
        tmp_path / "sif",
        tmp_path / "creds",
    )
    assert "--containall" in argv
    assert "--cleanenv" in argv


def test_apptainer_argv_wraps_claude_with_umask(tmp_path: Path) -> None:
    """Sharp-edges H1: rely on shell umask, not entrypoint $UMASK."""
    argv = SHARED.build_apptainer_argv(
        "/usr/bin/apptainer",
        tmp_path / "sif",
        tmp_path / "creds",
    )
    # Last three args are: sh -c 'umask 0077 && exec claude'
    assert argv[-3:] == ["sh", "-c", "umask 0077 && exec claude"]


def test_apptainer_argv_does_not_pass_net_flag(tmp_path: Path) -> None:
    """We rely on apptainer's default of host network sharing for the
    OAuth callback; CAPABILITY-SURFACE.md §2a documents this. If a
    future commit adds --net=none or --network, the OAuth callback
    will silently fail. Pin it."""
    argv = SHARED.build_apptainer_argv(
        "/usr/bin/apptainer",
        tmp_path / "sif",
        tmp_path / "creds",
    )
    assert "--net" not in argv
    assert "--network" not in argv


# ─────────────────────── apptainer subenv: --cleanenv contract ───────────────────────


def test_apptainer_subenv_strips_appearance_env_prefixes() -> None:
    """The point of --cleanenv is explicit env propagation. A parent
    shell with APPTAINERENV_FOO set must NOT leak FOO into the
    container. Reviewer adversarial #1."""
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "APPTAINERENV_LEAKED": "should-not-appear",
        "APPTAINERENV_ALSO_LEAKED": "nope",
        "SINGULARITYENV_LEGACY_LEAK": "nope",
        "USER": "user",
    }
    sub_env = SHARED.build_apptainer_subenv(parent)
    leaks = [k for k in sub_env if k.startswith(("APPTAINERENV_", "SINGULARITYENV_"))]
    # Only the two we explicitly set; nothing inherited.
    assert set(leaks) == {
        "APPTAINERENV_CLAUDE_CONFIG_DIR",
        "SINGULARITYENV_CLAUDE_CONFIG_DIR",
    }, f"Leaked vars: {leaks}"
    assert sub_env["APPTAINERENV_CLAUDE_CONFIG_DIR"] == "/out"
    assert sub_env["SINGULARITYENV_CLAUDE_CONFIG_DIR"] == "/out"


def test_apptainer_subenv_preserves_non_appearance_env() -> None:
    """Non-*ENV_* parent env (PATH, HOME) must propagate so apptainer
    itself can find its config dirs and the user's home cache."""
    parent = {"PATH": "/usr/bin", "HOME": "/home/x", "USER": "x"}
    sub_env = SHARED.build_apptainer_subenv(parent)
    assert sub_env["PATH"] == "/usr/bin"
    assert sub_env["HOME"] == "/home/x"
    assert sub_env["USER"] == "x"


# ─────────────────────── docker argv: bind surface ───────────────────────


def test_docker_argv_has_single_volume_to_out(tmp_path: Path) -> None:
    creds_dir = tmp_path / "shared-auth" / "agent-claude"
    argv = SHARED.build_docker_argv("botainer/agent-claude:0.1", creds_dir, 54545, 54549)
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert vols == [f"{creds_dir}:/out"]


def test_docker_argv_publishes_only_loopback(tmp_path: Path) -> None:
    """Multi-tenant safety: docker -p binds must always be 127.0.0.1,
    never 0.0.0.0. Sharp-edges H2 cross-tenant concern."""
    argv = SHARED.build_docker_argv(
        "botainer/agent-claude:0.1",
        tmp_path,
        54545,
        54549,
    )
    pubs = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert pubs == ["127.0.0.1:54545-54549:54545-54549"]
    for p in pubs:
        assert p.startswith("127.0.0.1:"), f"Non-loopback publish: {p}"


def test_docker_argv_wraps_claude_with_umask(tmp_path: Path) -> None:
    argv = SHARED.build_docker_argv("img", tmp_path, 54545, 54549)
    # Updated: `sh` moved from a positional argument to
    # `--entrypoint sh`. It had to — the image's ENTRYPOINT is
    # `agent-claude-entrypoint`, ending in `exec claude "$@"`, so the old form
    # ran `claude sh -c "umask 0077 && exec claude"` and handed the login
    # command to the AGENT instead of executing it. This test asserted the
    # command STRING was right, which it always was; what was wrong was who
    # would run it. See tests/unit/test_login_entrypoint_override.py.
    assert argv[argv.index("--entrypoint") + 1] == "sh"
    assert argv[-2:] == ["-c", "umask 0077 && exec claude"]


def test_docker_argv_uses_rm_and_it(tmp_path: Path) -> None:
    """One-shot, interactive: ephemeral and accepts user input."""
    argv = SHARED.build_docker_argv("img", tmp_path, 54545, 54549)
    assert "--rm" in argv
    assert "-it" in argv


def test_docker_login_argv_runs_as_host_user(tmp_path: Path) -> None:
    """Solidity-check finding (Docker-only laptop path): the login container
    MUST run as the host user (`--user uid:gid`), matching the session adapter
    (adapters/docker.py). Without it the credential lands owned by the image's
    fixed UID 1001 and the host user can't read it → shared-mode start refuses,
    isolated-mode silently re-prompts. Both plugin variants must carry it."""
    import os
    expect = f"{os.getuid()}:{os.getgid()}"
    for mod in (SHARED, ISOLATED):
        argv = mod.build_docker_argv("img", tmp_path, 54545, 54549)
        assert "--user" in argv, f"{mod.__name__} login argv missing --user"
        assert argv[argv.index("--user") + 1] == expect


# ─────────────────────── parity: shared vs isolated ───────────────────────


def test_shared_and_isolated_share_argv_builders_shape(tmp_path: Path) -> None:
    """Both plugins must produce structurally identical argvs (modulo
    the creds_dir). Drift between the two has happened before; pin
    it here so future edits to one side without the other fail loudly."""
    creds = tmp_path / "creds"
    sif = tmp_path / "sif"
    shared_argv = SHARED.build_apptainer_argv("/usr/bin/apptainer", sif, creds)
    isolated_argv = ISOLATED.build_apptainer_argv("/usr/bin/apptainer", sif, creds)
    assert shared_argv == isolated_argv

    shared_docker = SHARED.build_docker_argv("img", creds, 54545, 54549)
    isolated_docker = ISOLATED.build_docker_argv("img", creds, 54545, 54549)
    assert shared_docker == isolated_docker


# ─────────────────────── M2: .sif ownership + symlink-refuse ───────────────────────


def test_resolve_apptainer_sif_refuses_symlink(tmp_path: Path) -> None:
    """M2 (sharp-edges M2, deferred). apptainer exec's the .sif's
    entrypoint as the calling user, so the image processes the OAuth token.
    A .sif that's a symlink to /tmp/attacker.sif is exactly the swap-attack
    surface — _resolve_apptainer_sif must refuse it."""
    state_root = tmp_path
    (state_root / "images").mkdir()
    real = state_root / "real.sif"
    real.write_bytes(b"fake .sif data")
    sif_link = state_root / "images" / "botainer-agent-claude.sif"
    sif_link.symlink_to(real)
    for mod in (SHARED, ISOLATED):
        assert mod._resolve_apptainer_sif(state_root) is None, mod.__name__


def test_resolve_apptainer_sif_refuses_group_writable(tmp_path: Path) -> None:
    """A group- or world-writable .sif can be swapped by any co-tenant
    between rebuild and login — refuse so the auth flow never processes a
    file an attacker could replace."""
    import os
    state_root = tmp_path
    (state_root / "images").mkdir()
    sif = state_root / "images" / "botainer-agent-claude.sif"
    sif.write_bytes(b"fake .sif data")
    os.chmod(sif, 0o664)  # group-writable
    for mod in (SHARED, ISOLATED):
        assert mod._resolve_apptainer_sif(state_root) is None, mod.__name__


def test_resolve_apptainer_sif_accepts_owner_only_mode(tmp_path: Path) -> None:
    """The ordinary happy path: a real, owner-owned, owner-only-writable .sif
    resolves normally so the .sif guards don't break legitimate flows."""
    import os
    state_root = tmp_path
    (state_root / "images").mkdir()
    sif = state_root / "images" / "botainer-agent-claude.sif"
    sif.write_bytes(b"fake .sif data")
    os.chmod(sif, 0o600)
    for mod in (SHARED, ISOLATED):
        assert mod._resolve_apptainer_sif(state_root) == sif, mod.__name__


# ─────────────────────── M3: MY_BOTAINER validation ───────────────────────


def test_validated_state_root_refuses_invalid_chars(monkeypatch, tmp_path: Path) -> None:
    """M3 (sharp-edges M1, deferred). The hook's MY_BOTAINER lookup
    must apply the same character + traversal validation the launcher
    (botainer/state/dir.py) does. A direct hook invocation (not via
    `botainer auth login`) was previously unvalidated — closing the
    sibling-path asymmetry."""
    import pytest
    for mod in (SHARED, ISOLATED):
        monkeypatch.setenv("MY_BOTAINER", "/tmp/x; rm -rf /")
        with pytest.raises(SystemExit, match="invalid characters"):
            mod._validated_state_root()
        monkeypatch.setenv("MY_BOTAINER", "/home/user/../etc/.botainer")
        with pytest.raises(SystemExit, match="path traversal"):
            mod._validated_state_root()


def test_validated_state_root_accepts_normal_paths(monkeypatch, tmp_path: Path) -> None:
    """Normal $HOME-rooted MY_BOTAINER passes — no false-positive on the
    common case."""
    for mod in (SHARED, ISOLATED):
        monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "ok"))
        assert mod._validated_state_root() == tmp_path / "ok"


def test_validated_state_root_matches_launcher_regex() -> None:
    """The hook's _VALID_STATE_DIR_PATTERN regex source must equal the
    launcher's (botainer/state/dir.py). Drift = the validation has a
    different allowlist on the two paths, which IS the asymmetry pattern."""
    from botainer.state.dir import _VALID_STATE_DIR_PATTERN as LAUNCHER_RE
    for mod in (SHARED, ISOLATED):
        assert mod._VALID_STATE_DIR_PATTERN.pattern == LAUNCHER_RE.pattern, (
            f"{mod.__name__} _VALID_STATE_DIR_PATTERN drifted from "
            f"botainer/state/dir.py — the two paths must enforce identical "
            f"MY_BOTAINER syntax (M3 / sharp-edges M1)."
        )


# ─────────────────────── M4: ancestor-symlink refusal ───────────────────────


def test_refuse_symlink_in_ancestry_blocks_mkdir_redirect(
    tmp_path: Path,
) -> None:
    """M4 (sharp-edges M4, deferred). An attacker who controls a
    parent dir as a symlink could redirect where Path.mkdir(parents=True)
    lands. The hook now lstat-checks every ancestor before mkdir and refuses
    on any symlink discovered."""
    import pytest
    state_root = tmp_path / "state"
    state_root.mkdir()
    # Attacker symlinks an intermediate dir to /etc.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (state_root / "shared-auth").symlink_to(elsewhere)
    target = state_root / "shared-auth" / "agent-claude"
    for mod in (SHARED, ISOLATED):
        with pytest.raises(SystemExit, match="symlink"):
            mod._refuse_symlink_in_ancestry(target, state_root)


def test_refuse_symlink_in_ancestry_normal_path_passes(tmp_path: Path) -> None:
    """The ordinary happy path: no symlinks in the ancestry, no refusal."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    target = state_root / "shared-auth" / "agent-claude"
    for mod in (SHARED, ISOLATED):
        # Should not raise.
        mod._refuse_symlink_in_ancestry(target, state_root)


# ─────────────────────── M7: --no-home on apptainer ───────────────────────


def test_apptainer_argv_carries_no_home(tmp_path: Path) -> None:
    """M7 (adversarial #3, deferred). Some apptainer versions
    still bind $HOME under --containall unless --no-home is explicit. The
    capability-surface "no other mounts" promise for the login container
    depends on it — pin it so a refactor can't quietly drop it."""
    for mod in (SHARED, ISOLATED):
        argv = mod.build_apptainer_argv(
            "/usr/bin/apptainer", tmp_path / "sif", tmp_path / "creds",
        )
        assert "--no-home" in argv, f"{mod.__name__}: missing --no-home (M7)"


# ─────────── credential-diagnostic safety (no leak, no crash) ───────────


def test_login_hooks_never_echo_bulk_credential_bytes() -> None:
    """Task #299 + sibling-drift fix: neither login hook may echo
    a large slice of the credential file on the not-a-credential path. The
    isolated variant was fixed to print length + a 4-char sanitized head;
    the shared variant kept `blob[:100]` (up to 100 bytes of a live OAuth
    token into stderr/scrollback) until this was closed. Source-level drift
    gate so the leak can't creep back into either variant."""
    for path in (ISOLATED_LOGIN, SHARED_LOGIN):
        src = path.read_text()
        assert "blob[:100]" not in src, (
            f"{path} echoes blob[:100] — up to 100 bytes of the credential "
            f"file. Use the length + sanitized-4-char-head diagnostic."
        )
        # The prior 'safe' fix called a bytes-decode on the str head, which
        # crashes with AttributeError (read_text returns str). Neither variant
        # may decode the blob.
        assert "blob[:4].decode" not in src and "blob.decode" not in src, (
            f"{path} decodes the str blob — AttributeError crash on the "
            f"not-a-credential path."
        )


def test_login_credential_head_sanitizer_is_safe() -> None:
    """The shared sanitize expression: printable 4-char head, non-printables
    → '?', never raises on a str (the read_text return type)."""
    def head_safe(blob: str) -> str:
        return "".join(c if c.isprintable() else "?" for c in blob[:4])
    assert head_safe("oat-abcdef") == "oat-"
    assert head_safe("\x00\x01ab") == "??ab"
    assert head_safe("") == ""
    # Must not raise on a str (the reported crash was .decode on a str).
    assert isinstance(head_safe("some-partial-token"), str)
