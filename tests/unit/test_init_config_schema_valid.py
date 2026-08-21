"""Regression: `botainer init` must write a config that the plugin schemas
ACCEPT. A template key outside a plugin's config_schema (additionalProperties:
false) makes the next `botainer start`/`hpc submit` refuse with "unknown config
key". Grace host-test: the apptainer template wrote
`plugins.hpc-launcher.max_concurrent_jobs`, which the schema didn't list.

This validates the TEMPLATE↔SCHEMA contract for every plugin the template
configures, so the class of drift can't recur silently."""

from __future__ import annotations

import yaml

from botainer.core.config import (
    CONFIG_FILENAME,
    HOST_MANAGED_DIR,
    write_initial_config,
)


def _plugin_schemas() -> dict[str, dict]:
    from botainer.plugins.lifecycle import list_installed
    out: dict[str, dict] = {}
    for p in list_installed():
        man = yaml.safe_load((p.plugin_dir / "botainer-plugin.yaml").read_text()) or {}
        out[p.name] = man.get("config_schema") or {}
    return out


def _assert_config_keys_valid(cfg: dict, schemas: dict[str, dict]) -> None:
    for pname, pconf in (cfg.get("plugins") or {}).items():
        if not isinstance(pconf, dict):
            continue
        schema = schemas.get(pname) or {}
        if schema.get("additionalProperties") is False and "properties" in schema:
            allowed = set(schema["properties"].keys())
            unknown = set(pconf.keys()) - allowed
            assert not unknown, (
                f"`botainer init` template writes plugins.{pname} key(s) {unknown} "
                f"not in that plugin's config_schema (allowed: {sorted(allowed)}). "
                f"`start`/`submit` would refuse this config."
            )


def test_init_apptainer_config_is_schema_valid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    write_initial_config(proj, agent="claude", force=True, runtime="apptainer")
    cfg = yaml.safe_load((proj / HOST_MANAGED_DIR / CONFIG_FILENAME).read_text())
    _assert_config_keys_valid(cfg, _plugin_schemas())


def test_init_docker_config_is_schema_valid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    write_initial_config(proj, agent="claude", force=True, runtime="docker")
    cfg = yaml.safe_load((proj / HOST_MANAGED_DIR / CONFIG_FILENAME).read_text())
    _assert_config_keys_valid(cfg, _plugin_schemas())
