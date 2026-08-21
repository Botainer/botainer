"""Would host-side refresh actually work? Modelled before building it.

EF-1 is now measured: a refresh mints a new refresh token AND the
old one is rejected with HTTP 400. Exactly one refresh token is valid at a time.
So `tests/unit/test_auth_concurrency_model.py` — which had to model both worlds
— can now be made concrete for the one we actually live in.

THE PROPOSED DESIGN:
  - the HOST holds the refresh token and is the only thing that ever refreshes;
  - before each launch it mints a fresh ACCESS token and writes it into THAT
    PROJECT'S OWN credential file, with a full lifetime ahead of it;
  - the container never needs to refresh, so it never holds the durable secret;
  - access tokens are bearer tokens, so concurrent sessions each hold their own
    and none of them interfere.

WHAT THIS FILE IS FOR. Not to prove the design "works" in the abstract — it is
to find the requirements that are easy to leave out and expensive to discover
later. It found one immediately: **the host must serialise its own refreshes**.
Two simultaneous launches both read the stored refresh token, both call the
endpoint, and under confirmed invalidation the second write clobbers a token the
server has already retired — reintroducing exactly the bug being fixed, one
layer up. A lock is not tidiness here, it is the whole property, and the test
below fails without it.

`HostTokenService` is written as the shape the real implementation should take,
so building it later is wiring rather than design.
"""
from __future__ import annotations

import threading

import pytest


class FakeAuthServer:
    """Confirmed semantics: rotate AND invalidate."""

    def __init__(self) -> None:
        self._n = 0
        self.valid_refresh = {"R0"}
        self.issued_access: set[str] = set()
        self.refresh_calls = 0

    def refresh(self, token: str) -> tuple[str, str]:
        self.refresh_calls += 1
        if token not in self.valid_refresh:
            raise PermissionError(f"{token} rejected (400)")
        self._n += 1
        new_r, new_a = f"R{self._n}", f"A{self._n}"
        self.valid_refresh.discard(token)      # EF-1: the old one dies
        self.valid_refresh.add(new_r)
        self.issued_access.add(new_a)
        return new_a, new_r

    def call_api(self, access: str) -> bool:
        return access in self.issued_access


class HostTokenService:
    """Sole holder of the refresh token. Hands out access tokens only.

    `serialise=False` exists to demonstrate that the lock is load-bearing —
    production must never construct it that way.
    """

    def __init__(self, server: FakeAuthServer, *, serialise: bool = True) -> None:
        self._server = server
        self._refresh = "R0"
        self._lock = threading.Lock() if serialise else None

    def _mint(self) -> str:
        access, new_refresh = self._server.refresh(self._refresh)
        self._refresh = new_refresh
        return access

    def access_token_for_launch(self) -> str:
        if self._lock is None:
            return self._mint()
        with self._lock:
            return self._mint()

    # What a container is ever given. Deliberately not the refresh token.
    def credential_for_container(self) -> dict:
        return {"claudeAiOauth": {
            "accessToken": self.access_token_for_launch(),
            "expiresAt": 9_999_999_999_000,
        }}


# --------------------------------------------------------------------------
# The properties the design is claimed to have.
# --------------------------------------------------------------------------

def test_concurrent_sessions_all_work() -> None:
    """The whole point. Five sessions launched together, none interfering."""
    server = FakeAuthServer()
    host = HostTokenService(server)
    tokens = [host.access_token_for_launch() for _ in range(5)]
    assert len(set(tokens)) == 5
    for t in tokens:
        assert server.call_api(t), "an earlier session's token stopped working"


def test_a_container_never_receives_the_refresh_token() -> None:
    """The security half, and the reason this beats today's design even
    ignoring concurrency: a prompt-injected agent can only take something that
    expires on its own."""
    server = FakeAuthServer()
    host = HostTokenService(server)
    for _ in range(3):
        blk = host.credential_for_container()["claudeAiOauth"]
        assert "refreshToken" not in blk
        assert not (set(blk.values()) & server.valid_refresh)


def test_the_host_must_serialise_its_own_refreshes() -> None:
    """THE requirement this model existed to find.

    Two launches that both read the stored refresh token before either writes
    back will both call the endpoint. Under confirmed invalidation the first
    call retires the token the second is still holding — so the second either
    fails outright, or succeeds and leaves the service holding a token the
    server has already killed. Either way the bug is reintroduced one layer up.

    Simulated deterministically rather than with real threads: a race that only
    shows up under load is a flaky test, and the interleaving is the point.
    """
    server = FakeAuthServer()
    host = HostTokenService(server, serialise=False)

    # Both launches read the same starting token — the classic lost update.
    stale = host._refresh
    host.access_token_for_launch()             # launch A refreshes, R0 -> R1
    host._refresh = stale                      # launch B still holds R0

    with pytest.raises(PermissionError):
        host.access_token_for_launch()         # B presents the retired token


def test_serialised_launches_never_present_a_retired_token() -> None:
    """The same interleaving, with the lock in place, under real threads."""
    server = FakeAuthServer()
    host = HostTokenService(server)
    results: list = []
    barrier = threading.Barrier(8)

    def launch() -> None:
        barrier.wait()
        try:
            results.append(host.access_token_for_launch())
        except PermissionError as exc:         # must never happen
            results.append(exc)

    threads = [threading.Thread(target=launch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert not any(isinstance(r, Exception) for r in results), (
        "a launch presented a retired refresh token despite the lock")
    assert len(set(results)) == 8, "two launches got the same access token"
    for tok in results:
        assert server.call_api(tok)


def test_no_shared_directory_is_needed_at_all() -> None:
    """Each project gets its own file, written by the host.

    This is the part that removes a whole class of risk rather than guarding
    it: with no shared bind there is no cross-project surface — no settings.json
    carrying hooks, no plugins/ directory, no shell-snapshots that get sourced,
    no claude.json with other services' MCP credentials, and no shared
    conversation history. The allowlist that currently polices that bind stops
    being necessary.
    """
    server = FakeAuthServer()
    host = HostTokenService(server)
    projects = {name: host.credential_for_container() for name in ("a", "b", "c")}

    seen = [p["claudeAiOauth"]["accessToken"] for p in projects.values()]
    assert len(set(seen)) == 3, "projects were handed the same credential"
    for p in projects.values():
        assert set(p["claudeAiOauth"]) == {"accessToken", "expiresAt"}, (
            "the container's credential file carries more than it needs")


def test_the_host_refreshes_once_per_launch_not_once_per_api_call() -> None:
    """Cost sanity: this design must not multiply token-endpoint traffic.

    One refresh per launch, not one per request — otherwise we would be
    hammering the endpoint and inviting rate limiting.
    """
    server = FakeAuthServer()
    host = HostTokenService(server)
    for _ in range(4):
        tok = host.access_token_for_launch()
        for _ in range(50):                     # a session's worth of calls
            assert server.call_api(tok)
    assert server.refresh_calls == 4


# --------------------------------------------------------------------------
# The dependency this design still carries.
# --------------------------------------------------------------------------

def test_a_session_outliving_its_access_token_is_the_open_question() -> None:
    """EF-3, unmeasured: does Claude Code re-read the credential mid-session?

    The container holds NO refresh token, so when its access token expires it
    cannot renew on its own. Either the host tops the file up and Claude Code
    notices (sessions of any length work), or it cached at startup and the
    session dies at that point.

    Encoded as a test so the dependency cannot be forgotten while building: the
    model asserts what MUST be true for a long session to survive, which is
    that something outside the container replaces the token.
    """
    server = FakeAuthServer()
    host = HostTokenService(server)
    session_token = host.access_token_for_launch()
    assert server.call_api(session_token)

    # Simulate expiry: the container has nothing to renew with.
    server.issued_access.discard(session_token)
    assert not server.call_api(session_token)

    # Only a host-side top-up can rescue it. Whether the agent PICKS UP that
    # new file is EF-3 and is not answerable here.
    topped_up = host.access_token_for_launch()
    assert server.call_api(topped_up)
