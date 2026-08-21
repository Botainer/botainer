"""Defense-in-depth: detect likely-credential env vars at compose time.

The launcher refuses to compose a spec that passes a credential to the
container's env. Two paths can leak:

1. `.botainer/config.yaml`'s `env:` block — user may have copy-pasted
   from a different tool that wanted them to inject `ANTHROPIC_API_KEY`.
2. A plugin's pre_session hook contribution — a buggy or malicious
   plugin could inject credentials. (`run_pre_session_hooks` already
   raises on conflicts; this catches first-time injections.)

The right way to give an agent credentials is the credential-proxy
plugin (ephemeral session token) or the per-project mounted credential
file (which the launcher's adapter handles, NOT a config-level env).
Anything else is presumptively a leak.

The patterns are conservative — we err on the side of false-positive
refusal because the cost of refusal is "user gets a clear message" and
the cost of false-negative is "credential leaks into a container the
agent can read."
"""

from __future__ import annotations

import re

from botainer.core.refusal import RefusalCategory, Refused

# Exact env var names that are credentials. (Refuse outright.)
_CREDENTIAL_NAMES: frozenset[str] = frozenset({
    # Anthropic
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
    # OpenAI
    "OPENAI_API_KEY",
    "OPENAI_ORGANIZATION",
    "OPENAI_ORG_ID",
    # AWS
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    # Google Cloud / Generative
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GEMINI_API_KEY",
    # Azure
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_KEY",
    "AZURE_CLIENT_SECRET",
    # GitHub / Git
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    # NPM / package managers
    "NPM_TOKEN",
    "PIP_INDEX_URL_PASSWORD",
    # Hugging Face
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    # Cohere / Mistral / Replicate / etc.
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "REPLICATE_API_TOKEN",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "PERPLEXITY_API_KEY",
    # Generic — refuse cautiously
    "SSH_AUTH_SOCK",   # would link host ssh-agent into the container
})


# Substring patterns: refuse if the env var name matches any of these.
# Case-insensitive. Tasks #157 + #295: prior set required *suffix* form,
# missing BEARER_TOKEN, SECRET_PROD_DB, DB_PASS, OAUTH_*, JWT, SLACK_*,
# DOCKER_PASSWORD, KUBE_TOKEN, etc. Added prefix-form + word-anywhere.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Suffix forms (original set; still valid).
    re.compile(r"_API_KEY$", re.IGNORECASE),
    re.compile(r"_SECRET$", re.IGNORECASE),
    re.compile(r"_SECRET_KEY$", re.IGNORECASE),
    re.compile(r"_PRIVATE_KEY$", re.IGNORECASE),
    re.compile(r"_ACCESS_TOKEN$", re.IGNORECASE),
    re.compile(r"_AUTH_TOKEN$", re.IGNORECASE),
    re.compile(r"_PASSWORD$", re.IGNORECASE),
    re.compile(r"_BEARER$", re.IGNORECASE),
    # Prefix forms (task #157).
    re.compile(r"^BEARER", re.IGNORECASE),
    re.compile(r"^SECRET[_A-Z0-9]", re.IGNORECASE),
    re.compile(r"^TOKEN[_A-Z0-9]?", re.IGNORECASE),
    re.compile(r"^OAUTH", re.IGNORECASE),
    re.compile(r"^JWT", re.IGNORECASE),
    re.compile(r"^DB_(PASS|PWD|SECRET)", re.IGNORECASE),
    re.compile(r"^DOCKER_(PASSWORD|TOKEN|PWD)", re.IGNORECASE),
    re.compile(r"^KUBE_TOKEN", re.IGNORECASE),
    # Word-anywhere (task #295) for vendor-prefixed cases.
    re.compile(r"^SLACK_(TOKEN|BOT_TOKEN|WEBHOOK)", re.IGNORECASE),
    re.compile(r"^STRIPE_", re.IGNORECASE),
    re.compile(r"^TWILIO_(AUTH|TOKEN)", re.IGNORECASE),
    re.compile(r"^SENDGRID_API", re.IGNORECASE),
    re.compile(r"^MAILGUN_API", re.IGNORECASE),
)


# Allowlist: env var names that LOOK credential-y but are safe to pass
# because they're not actually secrets in our context. Keep this short.
_ALLOWLIST: frozenset[str] = frozenset({
    "REQUESTS_CA_BUNDLE",   # path, not a secret
    "SSL_CERT_FILE",        # path, not a secret
    # Empty for now; expand as real false-positives come up.
})


def detect_credential_env_keys(env: dict[str, str]) -> list[str]:
    """Return env-var names that look like credentials.

    Pattern: exact match in _CREDENTIAL_NAMES, OR a regex match in
    _CREDENTIAL_PATTERNS, AND not in _ALLOWLIST.
    """
    matches: list[str] = []
    for name in env:
        if name in _ALLOWLIST:
            continue
        if name in _CREDENTIAL_NAMES:
            matches.append(name)
            continue
        if any(p.search(name) for p in _CREDENTIAL_PATTERNS):
            matches.append(name)
    return matches


def check_env_for_named_credentials(env: dict[str, str], *, source: str) -> None:
    """Raise Refused only on EXACT known-credential names (no heuristic
    patterns).

    AUDIT (H1): for a CURATED env source — the hpc-modules
    host_pre_launch env_file captured from `module load` — the heuristic
    patterns false-reject legitimate module config (e.g. the `^TOKEN` pattern
    flags `TOKENIZERS_PARALLELISM`, a common HuggingFace/ML var). Exact-name
    matching still refuses every real provider credential a modulefile could
    carry (ANTHROPIC_API_KEY, AWS_*, GITHUB_TOKEN, HF_TOKEN, …) without
    breaking ML modules. The full heuristic `check_env_for_leaks` remains in
    force for user-supplied env (cfg.env, pre_session contributions).
    """
    leaked = sorted(n for n in env if n in _CREDENTIAL_NAMES and n not in _ALLOWLIST)
    if not leaked:
        return
    raise Refused(
        RefusalCategory.ENV_VAR_DENIED,
        f"refused: known-credential env var(s) in {source}: {leaked}. "
        f"Module env must never carry secrets; a modulefile setting a provider "
        f"credential is refused. Remove it from the module env.",
    )


def check_env_for_leaks(env: dict[str, str], *, source: str) -> None:
    """Raise Refused if any env var name looks like a credential.

    Args:
        env: env vars to scan.
        source: human-readable description of where these came from
                (e.g., ".botainer/config.yaml `env:`" or "plugin <name>
                pre_session contribution"). Surfaced in the refusal.
    """
    matches = detect_credential_env_keys(env)
    # Broker sentinel exception: a value that is a provably-fake broker sentinel
    # (BROKER-SENTINEL.…NOT-A-REAL-CREDENTIAL) carries NO secret — it is what the
    # credential broker puts in the container INSTEAD of a real token (the real
    # one is injected host-side; the container never sees it). So a
    # credential-shaped NAME holding a sentinel VALUE is not a leak. Real-looking
    # values are still refused. See core/broker_sentinel.
    from botainer.core.broker_sentinel import is_sentinel
    matches = [m for m in matches if not is_sentinel(env.get(m, ""))]
    if not matches:
        return
    raise Refused(
        RefusalCategory.ENV_VAR_DENIED,
        (
            f"refused: credential-shaped env vars in {source}: "
            f"{sorted(matches)}.\n"
            f"  Passing credentials via container env is unsafe — the agent "
            f"can read them.\n"
            f"  → For Anthropic: enable the `agent-claude-proxy` plugin "
            f"(real key stays on host).\n"
            f"  → For per-agent credentials: use the agent plugin's `login` "
            f"command (e.g. `botainer plugin agent-claude login`).\n"
            f"  → If you really need to pass this env (e.g. it's not actually "
            f"a credential), rename it to something not matching the credential "
            f"patterns or file an issue."
        ),
    )
