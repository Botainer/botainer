"""No shipped image may bake key material into an ENV/ARG.

REPLACES A DISABLED CHECK. `plugins/agent-codex/Dockerfile` turns off
BuildKit's `SecretsUsedInArgOrEnv`, which fires on `OPENAI_API_KEY_FILE`
because the NAME contains API_KEY. The value is a path and the `_FILE` suffix
is the convention that keeps secrets OUT of env values — so the rule flags the
secure pattern, on every build, forever.

Silencing a check without replacing it is how a real one gets through later.
BuildKit's skip is file-scoped and would hide a genuine secret added to that
same file. This is the narrower rule we can keep true: the NAME may look
secret-ish, but the VALUE must not be key material, and a `*_FILE` variable
must hold an absolute path.

Covers `.def` as well as `Dockerfile` — apptainer is a first-class peer, and
BuildKit never looks at a `.def` at all, so without this the apptainer image
has no equivalent check whatsoever.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

#: ENV/ARG (Dockerfile) or `export` (.def) assignments, name and value.
_ASSIGN = re.compile(
    r"^\s*(?:ENV|ARG|export)\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)", re.M)

#: Names whose VALUE would be a secret if it were inline.
_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD)", re.I)

#: Values that are obviously key material rather than configuration.
_LOOKS_LIKE_A_SECRET = re.compile(
    r"^(sk-|ghp_|gho_|xox[baprs]-|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,})"
    r"|^[A-Za-z0-9+/]{40,}={0,2}$"
    r"|^[0-9a-f]{40,}$")


def _image_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pat in ("plugins/*/Dockerfile", "plugins/*/*.def", "docker/Dockerfile*"):
        out.extend(sorted(REPO.glob(pat)))
    return out


def test_no_image_bakes_key_material_into_an_env_var():
    files = _image_files()
    assert files, "found no image definitions to scan — the glob has rotted"

    offenders: list[str] = []
    for f in files:
        for m in _ASSIGN.finditer(f.read_text(encoding="utf-8")):
            name, value = m.group(1), m.group(2)
            line = f.read_text(encoding="utf-8")[:m.start()].count("\n") + 1
            rel = f.relative_to(REPO)
            if _LOOKS_LIKE_A_SECRET.match(value.strip('"\'')):
                offenders.append(f"{rel}:{line} {name}= looks like key material")
                continue
            if _SECRETISH.search(name):
                # A secret-ish NAME is allowed only in the _FILE form, and only
                # when it points somewhere — that is the whole reason the
                # BuildKit rule is skipped for these files.
                if not name.endswith("_FILE"):
                    offenders.append(
                        f"{rel}:{line} {name} has a secret-ish name and is not "
                        f"the _FILE form; put the PATH in {name}_FILE instead")
                elif not value.strip('"\'').startswith("/"):
                    offenders.append(
                        f"{rel}:{line} {name} is a _FILE var whose value "
                        f"{value!r} is not an absolute path")

    assert not offenders, (
        "shipped image definition(s) carry secret material or a secret-ish "
        "env var in the wrong form:\n  " + "\n  ".join(offenders)
        + "\n\nThis test exists because plugins/agent-codex/Dockerfile skips "
          "BuildKit's SecretsUsedInArgOrEnv (which fires on the SECURE _FILE "
          "pattern). If that skip ever hides a real secret, this is what "
          "catches it."
    )


def test_the_scanner_would_actually_catch_something(tmp_path):
    """Positive control. Every assertion above passes trivially if the regexes
    match nothing — and a silenced check replaced by a vacuous test is worse
    than the warning it replaced."""
    assert _LOOKS_LIKE_A_SECRET.match("sk-ant-oat01-aaaaaaaaaaaa")
    assert _LOOKS_LIKE_A_SECRET.match("a" * 41)
    assert _SECRETISH.search("OPENAI_API_KEY_FILE")
    assert not _LOOKS_LIKE_A_SECRET.match("/home/agent/.openai/api_key")
    # and the real file's real line is accepted, for the stated reason
    dockerfile = (REPO / "plugins" / "agent-codex" / "Dockerfile").read_text()
    assert "OPENAI_API_KEY_FILE=/home/agent/.openai/api_key" in dockerfile
