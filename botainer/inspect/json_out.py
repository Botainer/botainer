"""Render the SessionSpec as JSON (deterministic, sorted)."""

from __future__ import annotations

from typing import Any

from botainer.core.spec import SessionSpec


def render(spec: SessionSpec, *, with_protection: bool = False) -> dict[str, Any]:
    """Pydantic's `model_dump` with mode='json' produces serializable types.

    Task #150: with_protection=True embeds the protection view as a
    sibling key.

    Task #229: redact env values that look like credentials before
    serialising — previously this surface leaked raw secrets that
    `--preflight` redacted, an asymmetric defense.
    """
    from botainer.inspect._redact import redact as _redact
    out = spec.model_dump(mode="json")
    # spec.env.values may be the only credential carrier; walk and
    # redact in place (mode='safe' = always '<redacted>', no oracle).
    try:
        env = out.get("env", {}).get("values", {})
        if isinstance(env, dict):
            out["env"]["values"] = {k: _redact(k, v, mode="safe") for k, v in env.items()}
    except Exception:
        pass  # surface conservatively if shape changes — don't break inspect
    if with_protection:
        from botainer.inspect import protection
        out["protection"] = {
            "lines": protection.render(spec).splitlines(),
        }
    return out
