"""Typed refusal categories.

The launcher *refuses* rather than silently downgrading. Every refusal carries a
typed category from this enumeration so callers can match on it and tests can
assert the exact reason.

Add new categories at the bottom; never remove or rename (test fixtures rely on
the names being stable strings).
"""

from __future__ import annotations

from enum import Enum


class RefusalCategory(str, Enum):
    # Config / schema
    CONFIG_MISSING = "config-missing"
    CONFIG_INVALID = "config-invalid"
    CONFIG_SCHEMA_MISMATCH = "config-schema-mismatch"
    IMAGE_INVALID = "image-invalid"  # AC7: image ref is flag/shell-hostile
    POLICY_INVALID = "policy-invalid"
    POLICY_MISSING = "policy-missing"

    # Identity
    IDENTITY_CHANGE_REFUSED = "identity-change-refused"
    IDENTITY_AMBIGUOUS = "identity-ambiguous"
    PROJECT_ID_TAMPERED = "project-id-tampered"

    # Mounts
    MOUNT_TARGET_DENIED = "mount-target-denied"
    MOUNT_TARGET_OFF_ALLOWLIST = "mount-target-off-allowlist"
    MOUNT_SOURCE_DENIED = "mount-source-denied"
    MOUNT_CONFLICT = "mount-conflict"
    MOUNT_PATH_NOT_NORMALIZED = "mount-path-not-normalized"
    MOUNT_PATH_NULL_BIND_VIOLATED = "mount-path-null-bind-violated"
    MOUNT_READBACK_MISMATCH = "mount-readback-mismatch"
    MOUNT_READBACK_MISSING = "mount-readback-missing"

    # Capabilities
    CAPABILITY_DENIED_BY_POLICY = "capability-denied-by-policy"
    CAPABILITY_VALUE_INVALID = "capability-value-invalid"
    CAPABILITY_UNKNOWN = "capability-unknown"
    KERNEL_CAP_KEEP_NOT_ALLOWED = "kernel-cap-keep-not-allowed"
    API_ONLY_REQUIRES_ENDPOINTS = "api-only-requires-endpoints"
    # NETWORK_SETUP_FAILED removed along with the iptables
    # path it described (see DN-024 D4 + #176). When endpoint-ip-
    # allowlist enforcement returns, this category can come back —
    # but only along with its raise sites.
    RUNTIME_CANNOT_ENFORCE = "runtime-cannot-enforce"
    HOST_PORT_DENIED = "host-port-denied"
    ENV_VAR_DENIED = "env-var-denied"
    PORT_FORWARD_INVALID = "port-forward-invalid"
    PORT_FORWARD_CONFLICT = "port-forward-conflict"

    # Host storage. A full disk or an exhausted quota is not a bug in the
    # user's config and not a stack trace — it is a condition with a specific
    # remedy, and it needs a category so it can carry one.
    STATE_WRITE_FAILED = "state-write-failed"

    # MY_BOTAINER points somewhere the launcher will not keep credentials.
    # Its own category because the remedy is specific and NOT "pick a
    # different root": move the bulky COMPONENTS instead and leave the
    # credentials on private storage.
    STATE_ROOT_NOT_ALLOWED = "state-root-not-allowed"

    # The scheduler rejected a job. Distinct from a botainer refusal: nothing is
    # wrong with the request, Slurm just said no (bad account, closed partition,
    # over a limit). It needs a category because the reason comes from OUTSIDE
    # botainer and must reach both the user and the agent as something
    # actionable, rather than "CalledProcessError".
    JOB_SUBMIT_REJECTED = "job-submit-rejected"

    # Plugins
    PLUGIN_MANIFEST_INVALID = "plugin-manifest-invalid"
    PLUGIN_NAME_RESERVED = "plugin-name-reserved"
    PLUGIN_TIER_NOT_ALLOWED = "plugin-tier-not-allowed"
    PLUGIN_CONTRIBUTION_MALFORMED = "plugin-contribution-malformed"
    PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE = "plugin-contribution-out-of-envelope"
    PLUGIN_HOOK_FAILED = "plugin-hook-failed"
    PLUGIN_HOST_HOOKS_REQUIRE_CONSENT = "plugin-host-hooks-require-consent"
    PLUGIN_DECLARATIVE_REQUIRED = "plugin-declarative-required"
    PLUGIN_DEPENDENCY_UNRESOLVED = "plugin-dependency-unresolved"
    PLUGIN_IMAGE_DIGEST_MISMATCH = "plugin-image-digest-mismatch"
    CROSS_PLUGIN_MOUNT_CONFLICT = "cross-plugin-mount-conflict"

    # Sidecars / host_helpers
    SIDECAR_REFUSED_NO_DOWNGRADE = "sidecar-refused-no-downgrade"
    HOST_HELPER_REQUIRES_CONSENT = "host-helper-requires-consent"

    # Self-tests / preflight
    PREFLIGHT_FAILED = "preflight-failed"
    SELFTEST_FAILED = "selftest-failed"
    READBACK_FAILED = "readback-failed"

    # Runtime
    RUNTIME_NOT_AVAILABLE = "runtime-not-available"
    RUNTIME_LAUNCH_FAILED = "runtime-launch-failed"
    UNSUPPORTED_RUNTIME_FEATURE = "unsupported-runtime-feature"

    # Tamper / integrity
    TAMPER_DETECTED = "tamper-detected"
    PROVENANCE_MISMATCH = "provenance-mismatch"

    # Credential broker (botainer/broker/ — real token held host-side,
    # injected on the outbound leg; the container only ever sees a sentinel)
    BROKER_CREDENTIAL_UNAVAILABLE = "broker-credential-unavailable"
    BROKER_REFRESH_FAILED = "broker-refresh-failed"


class Refused(Exception):
    """The launcher refused to proceed. Categories are typed."""

    def __init__(self, category: RefusalCategory, message: str = "") -> None:
        self.category = category
        super().__init__(message)

    def __str__(self) -> str:
        return f"[{self.category.value}] {super().__str__()}"
