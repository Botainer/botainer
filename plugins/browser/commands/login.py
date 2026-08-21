#!/usr/bin/env python3
"""`botainer plugin browser login [URL]` — credential handoff (no viewer).

Prints how to capture a site login on YOUR OWN machine and hand the resulting
session to the agent's browser — without the live viewer and without typing your
password inside the untrusted container. Rendering only; the capture runs on your
host, the plumbing is `plugins.browser.storage_state` + the pre_session hook."""
from __future__ import annotations

import sys

_DEFAULT_PATH = ".botainer/browser-auth.json"


def render(url: str | None, out_path: str = _DEFAULT_PATH) -> str:
    """The user-facing instructions. Pure so it's testable."""
    site = url or "<the-site-url>"
    return (
        "Log a site in for the agent — CREDENTIAL HANDOFF (no viewer, no password\n"
        "typed inside the container).\n\n"
        "1. On YOUR OWN machine, capture the login in your own browser:\n"
        f"     npx playwright codegen --save-storage={out_path} {site}\n"
        "   Log in normally (password, SSO, 2FA), then close the window. This\n"
        "   writes a session file; your password stays on your machine.\n\n"
        "2. Point the browser plugin at it in .botainer/config.yaml:\n"
        "     plugins:\n"
        "       browser:\n"
        f"         storage_state: {out_path}\n\n"
        "3. Start (or restart) the session — the agent's browser starts logged in.\n\n"
        "Notes:\n"
        f"  • Keep {out_path} SECRET (it's a live session) and out of git — add it\n"
        "    to .gitignore. Prefer a dedicated/limited account + a short session.\n"
        "  • The agent gets the logged-in session either way; this just keeps your\n"
        "    PASSWORD off the untrusted container (an in-container login is\n"
        "    keyloggable). It is NOT more access than logging in via the viewer.\n"
        "  • Some sites bind a session to the browser (device/DPoP/mTLS) and won't\n"
        "    transfer. If the agent isn't logged in, use the live viewer to log in\n"
        "    manually instead (`botainer plugin browser watch`, viewer: true)."
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    url = next((a for a in argv if not a.startswith("-")), None)
    print(render(url))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
