"""Step B (#216; the PATH-clobber acknowledged risk, internal design note
DN-005) — in-container PREPEND trampoline tests.

The trampoline is the docker/apptainer adapter's solution to the
`--env-file`-under-`--cleanenv` clobber: instead of SETting PATH/LD_LIBRARY_
PATH/… (which would replace the container's /opt/conda/bin), pass the
module-load values as `_BOTAINER_PREPEND_<NAME>` helper env vars and let a
shell trampoline PREPEND them at exec time inside the container — preserving
the container's own entries (the agent binary's dir).

These tests cover (a) composition's split of an env_file into scalar-only +
path-list prepends when the BOTAINER_USE_INNER_PREPEND opt-in is set,
(b) the apptainer + docker adapter argv shape with prepends present, and
(c) the trampoline string's behavior on hostile shell-metacharacter values
(structural — no shell is actually invoked here; the value is passed through
literally so a real run would treat it as a string PATH entry, not as code).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from botainer.core.spec import (
    HookSpec,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    SessionSpec,
)


def _make_session_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "sessions" / "s1").mkdir(parents=True)
    return state


def test_split_pulls_path_list_vars_out_of_env_file(tmp_path: Path) -> None:
    """The split helper writes a scalar-only sibling file and returns the
    path-list vars as (var, value) pairs. The original file is left
    untouched on disk (composition picks the new path instead)."""
    from botainer.core.composition import _split_module_env_for_inner_prepend
    state = _make_session_state(tmp_path)
    ef = state / "sessions" / "s1" / "module-env.env"
    ef.write_text(
        "PATH=/apps/python/3.11/bin\n"
        "LD_LIBRARY_PATH=/apps/python/3.11/lib\n"
        "CUDA_HOME=/apps/cuda/12.3\n"
        "JAVA_HOME=/apps/java/21\n"
        "TOKENIZERS_PARALLELISM=false\n"
    )
    scalar_path, prepends = _split_module_env_for_inner_prepend(ef, state)
    assert scalar_path is not None
    assert scalar_path.exists()
    assert ef.exists()  # original untouched
    scalar_text = scalar_path.read_text()
    assert "PATH=" not in scalar_text
    assert "LD_LIBRARY_PATH=" not in scalar_text
    assert "CUDA_HOME=/apps/cuda/12.3" in scalar_text
    assert "JAVA_HOME=/apps/java/21" in scalar_text
    assert "TOKENIZERS_PARALLELISM=false" in scalar_text
    by_name = dict(prepends)
    assert by_name["PATH"] == "/apps/python/3.11/bin"
    assert by_name["LD_LIBRARY_PATH"] == "/apps/python/3.11/lib"
    assert set(by_name) == {"PATH", "LD_LIBRARY_PATH"}


def test_split_returns_no_scalar_file_when_env_file_is_path_list_only(
    tmp_path: Path,
) -> None:
    """If the env_file has NO scalars after removing path-list vars, no
    scalar file is written and the caller drops the env_file entirely
    (the prepends carry all the data)."""
    from botainer.core.composition import _split_module_env_for_inner_prepend
    state = _make_session_state(tmp_path)
    ef = state / "sessions" / "s1" / "path-only.env"
    ef.write_text("PATH=/apps/x/bin\nLD_LIBRARY_PATH=/apps/x/lib\n")
    scalar_path, prepends = _split_module_env_for_inner_prepend(ef, state)
    assert scalar_path is None
    assert len(prepends) == 2


def test_split_returns_none_when_no_path_list_vars(tmp_path: Path) -> None:
    """Scalar-only env_file → split is a no-op (returns None, []); caller
    keeps the original env_file unchanged."""
    from botainer.core.composition import _split_module_env_for_inner_prepend
    state = _make_session_state(tmp_path)
    ef = state / "sessions" / "s1" / "scalar.env"
    ef.write_text("CUDA_HOME=/apps/cuda\nJAVA_HOME=/apps/java\n")
    scalar_path, prepends = _split_module_env_for_inner_prepend(ef, state)
    assert scalar_path is None
    assert prepends == []


def _base_spec_with_wraps(
    state: Path, runtime: str, prepends: tuple[tuple[str, str], ...]
) -> SessionSpec:
    # apptainer adapter refuses NetworkMode.NONE (the default) because it
    # can't actually enforce namespace isolation; INTERNET is fine for argv-
    # shape tests where the spec doesn't reach a real runtime.
    return SessionSpec(
        session_id="s1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state),
        runtime=runtime,
        image="img" if runtime != "apptainer" else "/abs/image.sif",
        network=NetworkSpec(mode=NetworkMode.INTERNET),
        entrypoint_wraps=(
            ("/usr/local/bin/agent-claude-entrypoint", "claude"),
        ),
        module_env_path_prepends=prepends,
    )


def test_apptainer_argv_carries_prepend_env_and_trampoline(tmp_path: Path) -> None:
    """The apptainer adapter emits `--env _BOTAINER_PREPEND_<NAME>=<val>` for
    each prepend and wraps the entrypoint in `bash -c '<trampoline>' bash <wraps>`."""
    from botainer.adapters.apptainer import ApptainerAdapter
    from botainer.adapters._inner_prepend import _inner_prepend_trampoline_script
    state = _make_session_state(tmp_path)
    spec = _base_spec_with_wraps(
        state, "apptainer",
        (("PATH", "/apps/python/3.11/bin"), ("LD_LIBRARY_PATH", "/apps/x/lib")),
    )
    argv = ApptainerAdapter().render_argv(spec)
    # --env helper vars present (one per prepend), preserving order.
    assert "--env" in argv
    assert "_BOTAINER_PREPEND_PATH=/apps/python/3.11/bin" in argv
    assert "_BOTAINER_PREPEND_LD_LIBRARY_PATH=/apps/x/lib" in argv
    # The argv ends with: bash -c '<trampoline>' bash <wrap0> <wrap1>
    trampoline = _inner_prepend_trampoline_script()
    image_idx = argv.index(spec.image)
    assert argv[image_idx + 1] == "bash"
    assert argv[image_idx + 2] == "-c"
    assert argv[image_idx + 3] == trampoline
    assert argv[image_idx + 4] == "bash"
    assert argv[image_idx + 5] == "/usr/local/bin/agent-claude-entrypoint"
    assert argv[image_idx + 6] == "claude"


def test_apptainer_argv_unchanged_when_no_prepends(tmp_path: Path) -> None:
    """Without module_env_path_prepends, the apptainer adapter renders the
    legacy shape (no bash-trampoline, no _BOTAINER_PREPEND_* envs)."""
    from botainer.adapters.apptainer import ApptainerAdapter
    state = _make_session_state(tmp_path)
    spec = _base_spec_with_wraps(state, "apptainer", ())
    argv = ApptainerAdapter().render_argv(spec)
    assert not any(a.startswith("_BOTAINER_PREPEND_") for a in argv)
    # No trampoline marker.
    assert all(not (a == "-c" and "exec \"$@\"" in argv[i + 1])
               for i, a in enumerate(argv[:-1]))


def test_docker_argv_carries_prepend_env_and_overrides_entrypoint_with_bash(
    tmp_path: Path,
) -> None:
    """The docker adapter emits `-e _BOTAINER_PREPEND_<NAME>=<val>` for each
    prepend and overrides --entrypoint with `bash`, with the original wraps
    becoming the post-image command tail."""
    from botainer.adapters.docker import DockerAdapter
    from botainer.adapters._inner_prepend import _inner_prepend_trampoline_script
    state = _make_session_state(tmp_path)
    spec = _base_spec_with_wraps(
        state, "docker", (("PATH", "/apps/python/3.11/bin"),),
    )
    argv = DockerAdapter().render_argv(spec)
    assert "-e" in argv
    assert "_BOTAINER_PREPEND_PATH=/apps/python/3.11/bin" in argv
    # --entrypoint is bash; the original wrap is in the command tail.
    ep_idx = argv.index("--entrypoint")
    assert argv[ep_idx + 1] == "bash"
    image_idx = argv.index(spec.image)
    trampoline = _inner_prepend_trampoline_script()
    assert argv[image_idx + 1] == "-c"
    assert argv[image_idx + 2] == trampoline
    assert argv[image_idx + 3] == "bash"
    assert argv[image_idx + 4] == "/usr/local/bin/agent-claude-entrypoint"
    assert argv[image_idx + 5] == "claude"


def test_docker_argv_unchanged_when_no_prepends(tmp_path: Path) -> None:
    """Without prepends, docker keeps the original --entrypoint = wrap[0] shape."""
    from botainer.adapters.docker import DockerAdapter
    state = _make_session_state(tmp_path)
    spec = _base_spec_with_wraps(state, "docker", ())
    argv = DockerAdapter().render_argv(spec)
    assert not any(a.startswith("_BOTAINER_PREPEND_") for a in argv)
    ep_idx = argv.index("--entrypoint")
    # Entrypoint is the original wrap[0], NOT bash.
    assert argv[ep_idx + 1] == "/usr/local/bin/agent-claude-entrypoint"


def test_trampoline_value_with_shell_metacharacters_is_passed_literally(
    tmp_path: Path,
) -> None:
    """Hostile-inject test: a module value containing $(rm -rf /) or backticks
    or `;cmd` must arrive in the container as a LITERAL STRING (one argv
    element via apptainer --env / docker -e — neither shell-interprets the
    value). The trampoline's fully-quoted `"$pv"` expansion then treats it
    as a literal string when PREPENDing onto PATH.

    We don't actually exec the trampoline (no container here); we assert the
    value reaches the argv as a single literal token. This is the structural
    guarantee — a real container run would see PATH=<literal-junk>:$PATH and
    `claude` would fail to launch (acceptable; the failure is loud and not
    code execution)."""
    from botainer.adapters.apptainer import ApptainerAdapter
    state = _make_session_state(tmp_path)
    hostile = "$(rm -rf /);`whoami`;/legit/bin"
    spec = _base_spec_with_wraps(
        state, "apptainer", (("PATH", hostile),),
    )
    argv = ApptainerAdapter().render_argv(spec)
    # The hostile value must be a single argv element after --env, unmodified.
    assert f"_BOTAINER_PREPEND_PATH={hostile}" in argv
    # And NOT split across multiple argv elements (no naive shell-split bug).
    occurrences = [
        i for i, a in enumerate(argv)
        if a == f"_BOTAINER_PREPEND_PATH={hostile}"
    ]
    assert len(occurrences) == 1


def test_trampoline_matches_sbatch_path_list_vars(tmp_path: Path) -> None:
    """The trampoline's PATH-list var set MUST match
    botainer.hpc.module_binds.PATH_LIST_VARS (the sbatch-flow inner PREPEND
    uses that frozenset). Drift = silent asymmetry between the two runtimes,
    which is exactly the HPC-parity rule's failure mode."""
    from botainer.adapters._inner_prepend import _inner_prepend_trampoline_script
    from botainer.hpc.module_binds import PATH_LIST_VARS
    script = _inner_prepend_trampoline_script()
    # Parse the `for v in NAME NAME NAME; do` part.
    import re as _re
    m = _re.search(r"for v in ([\w ]+); do", script)
    assert m is not None, script
    trampoline_vars = set(m.group(1).split())
    assert trampoline_vars == set(PATH_LIST_VARS), (
        f"trampoline path-list vars drifted from sbatch's PATH_LIST_VARS: "
        f"trampoline={trampoline_vars}, sbatch={set(PATH_LIST_VARS)}"
    )


def test_compose_with_opt_in_splits_env_file_and_carries_prepends(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end at composition: with BOTAINER_USE_INNER_PREPEND=1, a hook
    that contributes an env_file with PATH-list vars is SPLIT — the spec's
    env_files entry points to a scalar-only file (PATH removed) and
    module_env_path_prepends carries the PATH value."""
    from botainer.core import composition
    monkeypatch.setenv("BOTAINER_USE_INNER_PREPEND", "1")
    monkeypatch.delenv("BOTAINER_ALLOW_PATH_CLOBBER", raising=False)

    state = _make_session_state(tmp_path)
    ef = state / "sessions" / "s1" / "module-env.env"
    ef.write_text("PATH=/apps/python/3.11/bin\nCUDA_HOME=/apps/cuda/12.3\n")
    hook = tmp_path / "hook.py"
    hook.write_text(
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps({"
        '"version": "plugin-contribution-v1",'
        '"kind": "host_pre_launch",'
        f'"env_file": {str(ef)!r},'
        "}))"
    )
    hook.chmod(0o755)

    spec = SessionSpec(
        session_id="s1abc",
        project_uuid="u",
        project_root="/p",
        state_dir=str(state),
        runtime="docker",
        image="img",
        hooks=(
            HookSpec(plugin="hpc-modules", when="host_pre_launch", script_path=str(hook)),
        ),
    )
    from botainer.state import session_record as sr
    sr.write(state / "sessions" / "s1", sr.from_spec(spec))

    out = composition.run_host_pre_launch_hooks(spec)
    # PATH was extracted into prepends.
    assert ("PATH", "/apps/python/3.11/bin") in out.module_env_path_prepends
    # The env_file in the spec now points to a *-scalar-only.env containing
    # CUDA_HOME but not PATH.
    assert any(p.endswith("-scalar-only.env") for p in out.env_files)
    scalar_paths = [p for p in out.env_files if p.endswith("-scalar-only.env")]
    scalar_text = Path(scalar_paths[0]).read_text()
    assert "PATH=" not in scalar_text
    assert "CUDA_HOME=/apps/cuda/12.3" in scalar_text
    # And the ORIGINAL env_file path is NOT in spec.env_files (replaced).
    assert str(ef.resolve()) not in out.env_files
