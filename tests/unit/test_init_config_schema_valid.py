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


def _uncomment_template_example(text: str, marker: str) -> str:
    """Pull a commented-out example block out of the written config.

    The template writes examples as `  #   <yaml>` — two spaces, a hash, three
    spaces, then the YAML verbatim. Stripping that exact prefix is what a user
    does by hand, so the test does the same thing rather than a fuzzy reparse.
    """
    prefix = "  #   "
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith(prefix) and ln[len(prefix):].startswith(marker))
    # CONTIGUOUS only. The template has other commented examples further down
    # (a `ports:` block, for one), so running to end-of-file splices unrelated
    # YAML together and the test fails for a reason that has nothing to do with
    # the example under test.
    body: list[str] = []
    for ln in lines[start:]:
        if not ln.startswith(prefix):
            break
        body.append(ln[len(prefix):])
    return "\n".join("  " + ln for ln in body if ln.strip())


def test_the_commented_mounts_example_is_valid_if_you_uncomment_it(tmp_path,
                                                                   monkeypatch) -> None:
    """The `mounts.extra` example must WORK, not merely be present.

    `extra: []` names the knob but not its shape, so the template shows a real
    four-field entry. A commented example nobody executes is exactly the kind
    of thing that rots into a lie — a user uncomments it, `start` refuses, and
    the file that was meant to help is the thing that misled them. So: uncomment
    it here and put it through the same model `start` uses.
    """
    from botainer.core.config import MountsConfig

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    write_initial_config(proj, agent="claude", force=True, runtime="docker")
    text = (proj / HOST_MANAGED_DIR / CONFIG_FILENAME).read_text()

    parsed = yaml.safe_load(_uncomment_template_example(text, "extra:"))
    mounts = MountsConfig(**parsed)

    assert len(mounts.extra) == 1, "the example should show exactly one entry"
    entry = mounts.extra[0]
    assert entry.source.startswith("/"), "source must be an absolute host path"
    assert entry.target.startswith("/"), "target must be an absolute container path"
    assert entry.mode in {"ro", "rw"}
    assert entry.reason, "the example should demonstrate `reason`, not leave it blank"

    # Schema-valid is not enough: an example whose TARGET the default policy
    # refuses would send the user straight into a refusal. The comment tells
    # them the target is the end that usually bites, so it had better not bite
    # here.
    from botainer.core.policy import MountsPolicy
    allowed = MountsPolicy().extra_targets_allowlist
    assert any(entry.target == p or entry.target.startswith(p.rstrip("/") + "/")
               for p in allowed), (
        f"the template's example target {entry.target!r} is not under any default "
        f"allowed prefix {allowed} — uncommenting it would be refused"
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
