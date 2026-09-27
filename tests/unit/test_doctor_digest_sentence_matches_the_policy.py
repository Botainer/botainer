"""`doctor` tells users what the launchers will do. This is what makes that true.

`doctor`'s `_WHO_CHECKS_THE_DIGEST` predicts launcher behaviour, and its own
standing comment forbade that after the prediction shipped wrong twice. The
prohibition is now conditional on a pin — so the pin has to hold, and the first
two attempts at one did not:

  * `grep -A3 … | grep -q 'enforce=_ENFORCE_SIF_PROVENANCE.get(source, True)'`
    stayed GREEN when the call was replaced by `pass` with the old line left
    behind as a `# TODO restore:` comment, and FIRED when the identical call was
    spread over four lines. A gate that passes a deletion and fails a no-op
    refactor is worse than no gate: the refactor teaches people to delete it.
  * `grep -qE '"config-image": True'` was evaded by `"config-image":True`, by
    single quotes, and — the one that matters — by DELETING the key, which flips
    the behaviour to refusal via the fail-closed default while the grep sees
    nothing.

Both were caught by a refuting review that patched a copy of the tree and drove
the real CLI. So the pin asserts BEHAVIOUR and then asserts the sentence against
it, which is the only thing that ties the words to the code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.cli.doctor import _WHO_CHECKS_THE_DIGEST
from botainer.core import composition
from botainer.core.refusal import Refused


def _resolve(monkeypatch, tmp_path, source: str):
    """Drive the wrapper with a chosen SOURCE and a .sif that cannot match.

    No marker fixture is needed for the refusing half — `_verify_…` is called
    with a path that does not exist, so the enforcing policy reaches its OSError
    branch and the non-enforcing one does not raise. What is being pinned here is
    which POLICY the source selects, not the hashing itself (that lives in
    tests/integration/test_apptainer_image_resolution.py).
    """
    calls: list[tuple[str, bool]] = []

    def _spy(agent_name, sif_path, *, enforce=True):
        calls.append((str(sif_path), enforce))
        if enforce:
            raise Refused(composition.RefusalCategory.IMAGE_INVALID,
                          "spy refusal")

    monkeypatch.setattr(composition, "_resolve_session_image_unverified",
                        lambda *a, **k: (str(tmp_path / "some.sif"), source))
    monkeypatch.setattr(composition, "_verify_apptainer_sif_provenance", _spy)

    class _Cfg:
        image = None
        agent = "claude"
        plugins_enabled: list[str] = []
    try:
        composition._resolve_session_image(_Cfg(), runtime="apptainer")
        raised = False
    except Refused:
        raised = True
    assert len(calls) == 1, "the wrapper did not verify exactly once"
    return calls[0][1], raised


def test_a_config_image_DISCLOSES_and_the_sentence_says_warns(monkeypatch, tmp_path):
    """THE TIE. If the policy flips, this fails until the sentence is rewritten.

    Deleting the dict key does not evade it either: the default is True, which
    lands in the enforcing branch, and the sentence still says "warns".
    """
    enforce, raised = _resolve(monkeypatch, tmp_path, "config-image")
    assert enforce is False and not raised, (
        "a top-level `image:` now REFUSES. That may be the right answer — it is "
        "the maintainer's open decision — but `doctor` still tells users it "
        "'warns loudly and runs it anyway'. Rewrite _WHO_CHECKS_THE_DIGEST in "
        "the same commit as the policy change.")
    assert "warns" in _WHO_CHECKS_THE_DIGEST
    assert "EXCEPT" in _WHO_CHECKS_THE_DIGEST, (
        "the sentence no longer carves out the `image:` case it describes")


@pytest.mark.parametrize("source", ["override", "plugin-sif",
                                    "a-source-invented-next-year"])
def test_every_other_source_REFUSES_and_the_sentence_says_refuses(
        monkeypatch, tmp_path, source):
    """The other half, including an unregistered source: fail closed.

    `hpc submit` routes through `override` and the agent's own image is
    `plugin-sif`; the sentence promises a refusal for both. The invented source
    is the fail-closed default, which a grep on the dict could never see.
    """
    enforce, raised = _resolve(monkeypatch, tmp_path, source)
    assert enforce is True and raised, (
        f"source {source!r} no longer refuses a mismatch, and `doctor` promises "
        f"that it does")
    assert "refuses a mismatch" in _WHO_CHECKS_THE_DIGEST


def test_the_sentence_does_not_promise_what_doctor_cannot_know(monkeypatch):
    """The standing rule, kept: no promise beyond what this file pins.

    `doctor` hashes the file IT found. If a project's `image:` points elsewhere
    the launcher hashes a different file, and the sentence has to say so — that
    is measured fact about doctor, not a prediction about code it does not run.
    """
    assert "unless an `image:` points somewhere else" in _WHO_CHECKS_THE_DIGEST, (
        "doctor once again implies it hashed the file the launcher will use")
    for verdict in ("safe", "safer", "secure", "protects", "guaranteed"):
        assert verdict not in _WHO_CHECKS_THE_DIGEST.lower(), (
            f"{verdict!r}: a one-liner states facts and points; it never renders "
            f"a safety verdict")


def test_the_child_job_resolver_is_covered_by_the_same_sentence_or_named(
) -> None:
    """The sentence names two launchers. A third path exists — say where it is.

    `cli/hpc.py::_resolve_child_image` serves the dispatcher and the warm pool,
    and it was found composing a caged job around a replaced image while both
    named launchers refused it. It now applies the same policy dict. If someone
    gives it its own rule, this fails — because then the sentence covers two of
    three paths again and does not say so.
    """
    import inspect as _inspect
    from botainer.cli import hpc as hpcmod

    src = _inspect.getsource(hpcmod._resolve_child_image)
    assert "_ENFORCE_SIF_PROVENANCE" in src and "_verify_apptainer_sif_provenance" in src, (
        "dispatched jobs and warm-pool workers no longer share the session's "
        "image-provenance policy, so what `doctor` says is true of `start` and "
        "`hpc submit` is no longer true of the jobs they spawn")
