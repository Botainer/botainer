"""Task #241: EnvSpec.values must be immutable.

Was a plain dict despite ConfigDict(frozen=True). Pydantic's frozen=True
only blocks attribute reassignment, not mutation of mutable values. A
caller could do spec.env.values["LD_PRELOAD"] = "/tmp/evil.so" after
composition completed.

Fix: model_post_init wraps the dict in MappingProxyType. field_serializer
unwraps it back to a dict for JSON output.
"""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from botainer.core.spec import EnvSpec


def test_envspec_values_is_mappingproxy() -> None:
    env = EnvSpec(values={"FOO": "bar"})
    assert isinstance(env.values, MappingProxyType)


def test_envspec_values_mutation_rejected() -> None:
    env = EnvSpec(values={"FOO": "bar"})
    with pytest.raises(TypeError):
        env.values["BAZ"] = "qux"  # type: ignore[index]


def test_envspec_values_readable_like_dict() -> None:
    env = EnvSpec(values={"FOO": "bar"})
    assert env.values["FOO"] == "bar"
    assert dict(env.values) == {"FOO": "bar"}
    assert "FOO" in env.values
    assert list(env.values.keys()) == ["FOO"]


def test_envspec_serializes_back_to_dict() -> None:
    env = EnvSpec(values={"FOO": "bar"})
    j = env.model_dump_json()
    parsed = json.loads(j)
    assert parsed == {"values": {"FOO": "bar"}}


def test_envspec_model_dump_returns_plain_dict() -> None:
    env = EnvSpec(values={"FOO": "bar"})
    d = env.model_dump()
    assert d["values"] == {"FOO": "bar"}
    # The serialized form must be regular dict (so downstream consumers can
    # mutate their own copy without affecting the spec).
    assert not isinstance(d["values"], MappingProxyType)
