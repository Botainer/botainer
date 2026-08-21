"""Hostile config matrix.

Each scenario: a config (project + maybe plugin + maybe extra) that an attacker
or naive user might write, and the typed refusal we expect.

Per design 26-test-design.md Tier 5: malicious-config matrix.

#128 — was: each row asserted a SUBSTRING of `str(exc)` (e.g.
`"mount-source-denied" in str(exc)`). Substring matching is fragile:
a future refusal whose message happens to contain "mount-source-denied"
in a docstring or remediation hint passes the test even if the wrong
category fired. Now: each row pins the exact `RefusalCategory` enum
value via `exc.value.category == ExpectedCategory`. Renames of human-
readable messages don't break the test; wrong-category bugs do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core.refusal import RefusalCategory


def _bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_body: str) -> Path:
    from tests.conftest import TEST_IMAGE_REF

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    # Append image so we don't trip CONFIG_MISSING before the hostile-config
    # check we're actually testing (image check is early in composition).
    # If the test wants to assert config-schema errors, it can override by
    # including a malformed image in config_body.
    needs_image = "image:" not in config_body and "config-schema-mismatch" not in config_body
    body = config_body + (f"\nimage: {TEST_IMAGE_REF}\n" if needs_image else "")
    (proj / ".botainer" / "config.yaml").write_text(body)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    return proj


HOSTILE_CASES: list[tuple[str, str, RefusalCategory]] = [
    # (name, config-yaml, expected RefusalCategory)
    (
        "mount-shadow-via-extra",
        "agent: claude\nmounts:\n  extra:\n    - source: /etc/shadow\n      target: /scratch/shadow\n      mode: ro\n",
        RefusalCategory.MOUNT_SOURCE_DENIED,
    ),
    (
        "mount-docker-sock",
        "agent: claude\nmounts:\n  extra:\n    - source: /var/run/docker.sock\n      target: /var/run/docker.sock\n      mode: rw\n",
        RefusalCategory.MOUNT_TARGET_DENIED,
    ),
    (
        "mount-arbitrary-host-via-extra",
        "agent: claude\nmounts:\n  extra:\n    - source: /home\n      target: /elsewhere\n      mode: rw\n",
        RefusalCategory.MOUNT_TARGET_OFF_ALLOWLIST,
    ),
    (
        "env-denylisted-ld-preload",
        "agent: claude\nenv:\n  LD_PRELOAD: /tmp/evil.so\n",
        RefusalCategory.ENV_VAR_DENIED,
    ),
    (
        "env-denylisted-pythonpath",
        "agent: claude\nenv:\n  PYTHONPATH: /tmp\n",
        RefusalCategory.ENV_VAR_DENIED,
    ),
    (
        "kernel-cap-keep",
        "agent: claude\ncaps:\n  kernel:\n    keep: [SYS_ADMIN]\n",
        RefusalCategory.KERNEL_CAP_KEEP_NOT_ALLOWED,
    ),
    (
        "endpoint-allowlist-without-endpoints",
        "agent: claude\nnetwork:\n  mode: endpoint-ip-allowlist\n  endpoints: []\n",
        RefusalCategory.API_ONLY_REQUIRES_ENDPOINTS,
    ),
    (
        "bogus-field-in-config",
        "agent: claude\nbogus_top_level: true\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
    (
        "mount-target-with-newline",
        'agent: claude\nmounts:\n  extra:\n    - source: /a\n      target: "/data\\nfoo"\n      mode: ro\n',
        RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
    ),
    (
        "mount-target-with-dotdot",
        "agent: claude\nmounts:\n  extra:\n    - source: /a\n      target: /data/../etc\n      mode: ro\n",
        RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
    ),
    (
        "mount-target-with-comma-injection",
        'agent: claude\nmounts:\n  extra:\n    - source: /a\n      target: "/data,readonly=false"\n      mode: ro\n',
        RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
    ),
    (
        "mount-relative-target",
        "agent: claude\nmounts:\n  extra:\n    - source: /a\n      target: data/foo\n      mode: ro\n",
        RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
    ),
    # AC7 hole-hunt: the CRITICAL image-flag-injection bug.
    # `image: "--privileged"` was accepted unvalidated and rendered into
    # `docker run` with no `--` separator → a privileged container that
    # defeats --cap-drop ALL. Now refused at compose with IMAGE_INVALID.
    (
        "image-flag-injection-privileged",
        "agent: claude\nimage: \"--privileged\"\n",
        RefusalCategory.IMAGE_INVALID,
    ),
    (
        "image-flag-injection-mount",
        "agent: claude\nimage: \"-v=/etc:/host:ro\"\n",
        RefusalCategory.IMAGE_INVALID,
    ),
    (
        "image-whitespace-injection",
        "agent: claude\nimage: \"ubuntu:24.04 --privileged\"\n",
        RefusalCategory.IMAGE_INVALID,
    ),
    # AC7 validator-parity audit: negative regressions the
    # audit flagged as missing (validators existed, no hostile test pinned
    # them). Each pins the exact RefusalCategory at compose.
    (
        # MountExtra._validate_mode: a typo'd mode must refuse, not silently
        # downgrade to ro (#174). load_config wraps the ValidationError.
        "mount-mode-typo-rww",
        "agent: claude\nmounts:\n  extra:\n    - source: /data\n      target: /data\n      mode: rww\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
    (
        # network.mode enum now validated at PARSE (config.py NetworkConfig
        # field_validator, readiness audit #12) — caught earlier than the
        # former compose-time capability check, so it's a schema mismatch.
        "network-mode-bogus",
        "agent: claude\nnetwork:\n  mode: bogus\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
    (
        # ProjectConfig.agent traversal: config-vs-CLI asymmetry — the CLI
        # --agent is plugin-checked, the config field was not. A '..' would
        # traverse out of <state>/images in _resolve_apptainer_sif_path.
        "agent-path-traversal",
        "agent: ../../../etc/passwd\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
    (
        # AUDIT (C1 sibling): ProjectConfig.profile had no
        # validator, yet profile becomes the path component profiles/<profile>
        # used as an apptainer --bind source + on-host mkdir. A '..' traverses
        # to an arbitrary host dir bound RW into the container. Same class as
        # agent above; closed at the type level.
        "profile-path-traversal",
        "profile: ../../../etc\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
    (
        # AUDIT (LOW): ProjectConfig.agent's leading-'-' branch
        # (the resolved name reaches argv → flag injection) had no negative
        # regression — a refactor could drop it silently. Pin it.
        "agent-leading-dash",
        "agent: -evil\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
    (
        # AUDIT (LOW): ProjectConfig.agent's whitespace/control-char
        # branch had no negative regression. Pin a space variant.
        "agent-whitespace",
        "agent: 'cl aude'\n",
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    ),
]


@pytest.mark.parametrize("name,config,expected", HOSTILE_CASES)
def test_hostile_config_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    config: str,
    expected: RefusalCategory,
) -> None:
    """#128 fix: pin the RefusalCategory enum, not a stringified
    substring. Catches wrong-category-but-matching-message bugs."""
    proj = _bootstrap(monkeypatch, tmp_path, config)
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert exc.value.category == expected, (
        f"case={name!r}: expected {expected}, got {exc.value.category} — "
        f"message: {exc.value!s}"
    )
