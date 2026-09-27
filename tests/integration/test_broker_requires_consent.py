"""Broker hooks run only when a broker plugin is enabled.

A broker holds a real credential and can make requests with it. Enabling
`agent-claude-broker` or `agent-codex-broker` is the authorization gate for that
behavior. Ordinary credential-file management must not implicitly start one.
Credential-refresh diagnostics require their own explicit consent.

These tests check the import boundary: broker machinery belongs behind the
broker plugin hooks rather than an ordinary launcher's import path."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "botainer"
PLUGINS = REPO / "plugins"

#: Plugins whose whole purpose IS the broker. Enabling one is the consent.
_BROKER_PLUGINS = ("agent-claude-broker", "agent-codex-broker")


def _python_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _imports_broker(path: Path) -> list[str]:
    """Module paths under botainer.broker that `path` imports."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "botainer.broker"):
            hits.append(node.module or "")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("botainer.broker"):
                    hits.append(a.name)
    return hits


def test_launcher_core_never_imports_broker_machinery() -> None:
    """THE load-bearing one.

    If any module under botainer/ (outside botainer/broker/ itself) imports the
    broker, then some ordinary code path can reach a token-spending request
    without the user having enabled anything. That is the failure this whole
    file exists to prevent.
    """
    offenders = {}
    for path in _python_files(CORE):
        rel = path.relative_to(REPO)
        if rel.parts[:2] == ("botainer", "broker"):
            continue                      # the broker importing itself is fine
        hits = _imports_broker(path)
        if hits:
            offenders[str(rel)] = hits
    assert not offenders, (
        f"launcher core imports broker machinery: {offenders}. The broker "
        f"spends the user's subscription and is gated on them ENABLING a broker "
        f"plugin; an import here is a path that bypasses that consent. If you "
        f"need credential FILES, use botainer.state / the auth helpers — moving "
        f"files is fine, making requests with them is not.")


def test_only_broker_plugins_reach_broker_machinery() -> None:
    """Plugins other than the broker ones must not touch it either."""
    offenders = {}
    for path in _python_files(PLUGINS):
        rel = path.relative_to(REPO)
        plugin = rel.parts[1] if len(rel.parts) > 1 else ""
        if plugin in _BROKER_PLUGINS:
            continue
        hits = _imports_broker(path)
        if hits:
            offenders[str(rel)] = hits
    assert not offenders, (
        f"non-broker plugins import broker machinery: {offenders}. Enabling a "
        f"BROKER plugin is the user's consent to spend their subscription; a "
        f"different plugin reaching it routes around that.")


def test_the_broker_daemon_is_spawned_only_by_broker_plugins() -> None:
    """Import is not the only door — `python -m botainer.broker.daemon_main`
    would start it without importing anything."""
    pat = re.compile(r"botainer\.broker\.daemon_main")
    offenders = []
    for root in (CORE, PLUGINS):
        for path in _python_files(root):
            rel = path.relative_to(REPO)
            if rel.parts[:2] == ("botainer", "broker"):
                continue
            if len(rel.parts) > 1 and rel.parts[1] in _BROKER_PLUGINS:
                continue
            if pat.search(path.read_text(encoding="utf-8")):
                offenders.append(str(rel))
    assert not offenders, (
        f"these spawn the broker daemon without being a broker plugin: "
        f"{offenders}")


#: A shipped config MAY enable a broker plugin — `examples/secure.yaml`
#: legitimately does, because the broker really is the strongest credential
#: posture (the container never holds the refresh token). What it may not do is
#: enable it SILENTLY. Someone copying a file called "secure" should not
#: discover afterwards that it spends their subscription.
_REQUIRED_DISCLOSURES = ("SPENDS YOUR SUBSCRIPTION", "ToS GREY ZONE")


@pytest.mark.parametrize("name", _BROKER_PLUGINS)
def test_shipped_configs_never_enable_the_broker_silently(name: str) -> None:
    """Enabling is allowed; enabling without saying what it costs is not.

    Banning the example outright would be the WRONG fix — it would steer users
    away from the most secure credential mode available. The defect found on
    was not that `secure.yaml` uses the broker, but that it did so
    with a one-line comment about the credential staying host-side and nothing
    at all about billing or the ToS question. So the disclosure is what is
    enforced, not the absence of the plugin.
    """
    for pat in ("examples/*.yaml", "cluster_profiles/*.yaml"):
        for path in REPO.glob(pat):
            text = path.read_text(encoding="utf-8")
            if "plugins_enabled" not in text:
                continue
            enabled = any(
                ln.strip().startswith("-") and name in ln
                for ln in text.splitlines())
            if not enabled:
                continue
            missing = [d for d in _REQUIRED_DISCLOSURES if d not in text]
            assert not missing, (
                f"{path.name} enables {name} but does not disclose {missing}. "
                f"Enabling a broker plugin IS the user's consent to spend their "
                f"subscription in a ToS grey zone — a config that switches it "
                f"on without saying so collects that consent under false "
                f"pretences. Add the disclosure, or drop the plugin.")
