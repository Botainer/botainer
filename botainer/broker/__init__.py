"""Host-side credential broker (#54 sibling / T0-3 replacement).

The broker makes Claude Code run with its OAuth credential held HOST-SIDE,
never inside the container. The container's client is pointed at a local
socket via ``ANTHROPIC_BASE_URL`` and provisioned with a provably-fake
sentinel (see :mod:`botainer.core.broker_sentinel`); the broker daemon strips
whatever the client sent and injects ``Authorization: Bearer <access>`` on the
outbound leg to the pinned upstream.

Empirically established (broker prototype, 2026-07): Claude Code (Max OAuth)
routes its API traffic through ``ANTHROPIC_BASE_URL``, sending
``Authorization: Bearer <access_token>`` plus ``anthropic-beta:
…oauth-2025-04-20…`` and ``anthropic-version`` headers. A forwarded valid
access token is accepted by the real API. The broker therefore injects the
Bearer access token (NOT ``x-api-key``); the client's own ``anthropic-beta``
/ ``anthropic-version`` headers pass through untouched.

The credential the broker serves is **botainer's own** (written by
``botainer auth login``), never the host ``~/.claude``:

- shared mode:   ``<state_root>/shared-auth/agent-claude/.credentials.json``
- isolated mode: ``<state_root>/state/<uuid>/data/agent-claude/profiles/
  <profile>/.credentials.json``

Modules:

- :mod:`botainer.broker.daemon` — request handling + unix-socket server
  (SSRF pinning, no-redirect, header strip/inject, path allowlist).
- :mod:`botainer.broker.oauth_refresh` — host-side OAuth refresh keystore
  (refresh token never enters the container; rotation adopted).
- :mod:`botainer.broker.credential_source` — the botainer-credential-backed
  keystore the daemon uses (re-reads the file each call; writes rotated
  tokens back host-side).
- :mod:`botainer.broker.daemon_main` — standalone ``python3 -m`` entry point
  the plugin hook spawns (stdlib-only import chain).

This ``__init__`` deliberately imports nothing so that the detached daemon's
import chain stays stdlib-only and cheap.
"""

from __future__ import annotations
