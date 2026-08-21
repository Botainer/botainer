"""`botainer auth rotation-probe` — does a refresh KILL the old refresh token?

THE QUESTION, and why it is the only one left. We measured  that a
refresh returns a NEW refresh token. We did NOT measure whether the OLD one
stops working, and those are different claims — many OAuth servers rotate while
leaving the previous token usable, precisely so a client that dies mid-refresh
is not locked out. The whole architecture turns on this one bit:

    old tokens stay valid  -> today's design is fine; concurrent sessions work;
                              only the symlink bug needs fixing.
    old tokens are killed  -> mount mode cannot do concurrency at any effort,
                              and the host must become the sole refresher.

`tests/unit/test_auth_concurrency_model.py` models both worlds and shows the
designs diverging on exactly this bit. This command settles it by observation,
which is the only thing that can.

WHY IT IS NOT A BROKER, and does not import one. It makes two token-endpoint
calls and exits. It never proxies API traffic, never runs a daemon, and never
holds a credential beyond the call. The broker-consent rule (user)
gates the thing that SPENDS a subscription by serving requests; this spends
nothing but two refreshes. It still asks first, because the user's wording was
explicit that key rotation counts: *"if there's anything else you're doing that
uses a broker (e.g., to rotate keys), make sure the user agrees."*

The endpoint and client_id are PINNED here, never read from config, for the same
reason the broker pins them: the durable refresh token is sent to that URL, so a
hostile `.botainer/config.yaml` must not be able to redirect it.

SAFETY, because this deliberately rotates a live credential:
  - a backup is written BEFORE any network call, and its path is printed;
  - every successful refresh is persisted IMMEDIATELY and atomically, so the
    newest working token is always on disk before the next call is attempted;
  - the only unavoidable window is a crash between a 200 response and the write
    to disk, which is one fsync wide and is stated plainly rather than hidden.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.state import dir as state_dir

#: PINNED. See module docstring — the refresh token is sent here, so this must
#: never come from config. Same values the broker plugin pins.
_TOKEN_ENDPOINT = "https://api.anthropic.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _post_refresh(refresh_token: str, timeout: float = 20.0) -> tuple[int, dict]:
    """One refresh call. Returns (status, body). Never raises on HTTP status."""
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_ENDPOINT, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, body


def _write_credential_atomically(path: Path, doc: dict) -> None:
    """Persist before doing anything else. A rotated token that never reaches
    disk is a locked-out user."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cred-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib_suppress():
            os.unlink(tmp)
        raise


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _apply(doc: dict, body: dict) -> dict:
    blk = doc.setdefault("claudeAiOauth", {})
    blk["accessToken"] = body.get("access_token", blk.get("accessToken"))
    if body.get("refresh_token"):
        blk["refreshToken"] = body["refresh_token"]
    if body.get("expires_in"):
        blk["expiresAt"] = int((time.time() + float(body["expires_in"])) * 1000)
    return doc


@click.command("rotation-probe")
@click.option("--agent", default="agent-claude", show_default=True)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@handle_refusals
def rotation_probe(agent: str, yes: bool) -> None:
    """Settle whether a refresh INVALIDATES the previous refresh token.

    Makes two live calls to the token endpoint with your credential. Rotates it
    (twice, if old tokens turn out to still work). Always writes the newest
    working token back before continuing.
    """
    from botainer.cli.auth_doctor import _oauth_block, _read_json

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root
    cred = root / "shared-auth" / agent / ".credentials.json"
    if not cred.exists():
        click.secho(f"refused: no credential at {cred}", fg="red", err=True)
        click.echo("  Run `botainer auth login` first.")
        raise SystemExit(2)
    doc = _read_json(cred) or {}
    r0 = _oauth_block(doc).get("refreshToken") or ""
    if not r0:
        click.secho("refused: that credential holds no refresh token.",
                    fg="red", err=True)
        raise SystemExit(2)

    click.secho("This probe uses your REAL credential.", fg="yellow", bold=True)
    click.echo(f"  It sends up to two refresh requests to {_TOKEN_ENDPOINT}")
    click.echo("  and ROTATES your token (the new one is saved each time).")
    click.echo("  It does not proxy API traffic and starts no daemon.")
    click.echo("\n  Worst case: if it crashes between a response and the disk")
    click.echo("  write, you re-run `botainer auth login`. A backup is made.")
    if not yes and not click.confirm("\nProceed?", default=False):
        click.echo("aborted — nothing was sent.")
        return

    backup = root / f"rotation-probe-backup-{int(time.time())}.json"
    backup.write_text(json.dumps(doc), encoding="utf-8")
    click.echo(f"\nbackup: {backup}")

    # ---- call 1: rotate, and PERSIST before doing anything else ----
    click.echo("call 1: refreshing with the current token ...")
    status, body = _post_refresh(r0)
    if status != 200 or not body.get("access_token"):
        click.secho(f"\nrefresh FAILED (status {status}).", fg="red", bold=True)
        click.echo(f"  {str(body)[:300]}")
        click.echo("\n  Nothing was rotated; your credential is untouched.")
        click.echo("  If this says the token is invalid, you are simply logged")
        click.echo("  out — run `botainer auth login`.")
        raise SystemExit(1)
    r1 = body.get("refresh_token") or ""
    _write_credential_atomically(cred, _apply(doc, body))
    click.secho("  ok — new token saved.", fg="green")

    if not r1:
        click.secho("\nRESULT: NO ROTATION AT ALL.", fg="green", bold=True)
        click.echo("  The server returned no new refresh token, so the original")
        click.echo("  stays in use. Multi-holder is not a problem at all.")
        _record(root, "no-rotation")
        return
    if r1 == r0:
        click.secho("\nRESULT: NO ROTATION.", fg="green", bold=True)
        click.echo("  The same refresh token came back. Multi-holder is fine.")
        _record(root, "no-rotation")
        return

    # ---- call 2: THE question — is the OLD token still accepted? ----
    click.echo("call 2: retrying with the OLD token ...")
    status2, body2 = _post_refresh(r0)
    if status2 == 200 and body2.get("access_token"):
        # It worked, so we now hold a THIRD token; persist it immediately or the
        # one we saved a moment ago may itself have just been superseded.
        _write_credential_atomically(cred, _apply(_read_json(cred) or {}, body2))
        click.secho("\nRESULT: ROTATION WITHOUT INVALIDATION.", fg="green", bold=True)
        click.echo(
            "  A refresh mints a new token, but the OLD one still works.\n"
            "  => Multiple holders can coexist. Concurrent sessions are fine,\n"
            "     and the only real defect is the symlink bug that silently\n"
            "     splits a project off from the shared store.")
        _record(root, "rotation-without-invalidation")
        return

    click.secho("\nRESULT: ROTATION WITH INVALIDATION.", fg="red", bold=True)
    click.echo(f"  The old token was rejected (status {status2}).")
    click.echo(
        "  => Exactly one holder can be valid at a time. Concurrent sessions\n"
        "     in mount mode CANNOT work at any effort, because each container\n"
        "     refreshing kills the others. The host must become the sole\n"
        "     refresher and hand containers access tokens only.")
    _record(root, "rotation-with-invalidation")


def _record(root: Path, verdict: str) -> None:
    """Persist the finding WITH ITS DATE, so drift is detectable later.

    An external service's behaviour is not a fact we own — it can change with
    no notice and no version number. Recording when we last observed it is the
    difference between "we know" and "we knew once".
    """
    path = root / "external-facts.json"
    try:
        facts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        facts = {}
    facts.setdefault("oauth_refresh_behaviour", []).append({
        "verdict": verdict,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": _TOKEN_ENDPOINT,
    })
    path.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    click.echo(f"\nrecorded in {path}")
    click.echo("  Re-run periodically: this is someone else's server and the")
    click.echo("  answer can change without warning.")
