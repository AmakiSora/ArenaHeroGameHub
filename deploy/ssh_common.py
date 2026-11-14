#!/usr/bin/env python3
"""Shared SSH plumbing for the ArenaGame deploy scripts.

Why this module exists (OPTIMIZATION_REVIEW.md #4):

* ``paramiko.AutoAddPolicy()`` accepted *any* host key, so anything on the
  deploy path (coffee-shop Wi-Fi, a poisoned resolver, a compromised jump host)
  could impersonate the VPS and harvest ``ARENA_HERO_API_KEY`` and
  ``DASHBOARD_TOKEN``.  The server key is now pinned in
  ``deploy/.ssh_known_hosts`` and ``RejectPolicy`` refuses everything else.
* A legitimate brand-new server is still reachable through explicit
  trust-on-first-use: ``DEPLOY_TRUST_NEW_HOST=1`` prints the SHA256
  fingerprint, appends the key to ``deploy/.ssh_known_hosts`` and retries once.
  Without that variable the deploy refuses and prints what to do.
* Key auth (``DEPLOY_KEY_PATH`` + optional ``DEPLOY_KEY_PASSPHRASE``) is
  preferred over password auth, which keeps working unchanged for hosts that
  only allow a password.
* ``_mask()`` keeps tokens out of the terminal scrollback and CI logs.

Both ``deploy/deploy.py`` and ``deploy/deploy_source_only.py`` use this module
so the two entry points cannot drift apart on host-key handling.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import sys
from pathlib import Path

import paramiko

#: Pinned server keys.  Host-specific, therefore gitignored — but it must
#: survive between deploys so a second deploy does not need TOFU again.
KNOWN_HOSTS_PATH = Path(__file__).resolve().parent / ".ssh_known_hosts"

#: Set to "1" to accept an unknown server key once (trust on first use).
TRUST_NEW_HOST_ENV = "DEPLOY_TRUST_NEW_HOST"


def _mask(value: str) -> str:
    """Render a secret for the console: first 3 + last 2 characters only."""
    if not value:
        return "<unset>"
    if len(value) <= 5:
        return "*" * len(value)
    return f"{value[:3]}...{value[-2:]}"


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint of a host key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def trusts_new_host() -> bool:
    """True when DEPLOY_TRUST_NEW_HOST is set to a truthy value."""
    return os.environ.get(TRUST_NEW_HOST_ENV, "").strip().lower() in {"1", "true", "yes"}


def _known_host_name(host: str, port: int) -> str:
    """Return the known_hosts host field (``[host]:port`` for non-22 ports)."""
    return host if port == 22 else f"[{host}]:{port}"


def _probe_remote_key(host: str, port: int, timeout: float = 15.0) -> paramiko.PKey:
    """Fetch the key the server actually offers, without authenticating."""
    sock = socket.create_connection((host, port), timeout=timeout)
    transport: paramiko.Transport | None = None
    try:
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    finally:
        if transport is not None:
            transport.close()
        else:
            sock.close()


def _pin_host_key(key: paramiko.PKey, host: str, port: int, known_path: Path) -> None:
    """Append a trusted key to the known_hosts file in the exact paramiko format."""
    entry = f"{_known_host_name(host, port)} {key.get_name()} {key.get_base64()}\n"
    known_path.parent.mkdir(parents=True, exist_ok=True)
    with open(known_path, "a", encoding="utf-8") as fh:
        fh.write(entry)


def _refuse_unknown_host(host: str, port: int, key: paramiko.PKey, known_path: Path) -> None:
    print(
        f"ERROR: the SSH host key of {host}:{port} is not pinned.",
        file=sys.stderr,
    )
    print(f"  offered fingerprint: {fingerprint(key)}", file=sys.stderr)
    print(
        "  Compare it with the server's own key, e.g. on the VPS run:",
        file=sys.stderr,
    )
    print("    ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub", file=sys.stderr)
    print("  Only if it matches, trust it once with:", file=sys.stderr)
    print(f'    $env:{TRUST_NEW_HOST_ENV} = "1"    # PowerShell', file=sys.stderr)
    print(f"    export {TRUST_NEW_HOST_ENV}=1      # bash", file=sys.stderr)
    print(f"  or add it by hand to {known_path}:", file=sys.stderr)
    print(
        f"    {_known_host_name(host, port)} {key.get_name()} {key.get_base64()}",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _trust_new_host(host: str, port: int, key: paramiko.PKey, known_path: Path) -> None:
    print(f"  DEPLOY_TRUST_NEW_HOST=1: trusting new host key {fingerprint(key)}")
    _pin_host_key(key, host, port, known_path)
    print(f"  pinned in {known_path}")


def connect_ssh(
    host: str,
    port: int,
    username: str,
    *,
    password: str = "",
    key_path: str = "",
    key_passphrase: str = "",
    known_hosts_path: Path | str | None = None,
    timeout: float = 15.0,
) -> paramiko.SSHClient:
    """Connect with a pinned host key, key auth preferred over password auth.

    Exits the process with a readable message instead of silently trusting an
    unknown or changed server key.
    """
    known_path = Path(known_hosts_path) if known_hosts_path else KNOWN_HOSTS_PATH

    client = paramiko.SSHClient()
    if known_path.exists():
        client.get_host_keys().load(str(known_path))
        print(f"  pinned host keys: {known_path} ({len(client.get_host_keys())} entries)")
    else:
        print(f"  pinned host keys: {known_path} does not exist yet (first deploy?)")
    # RejectPolicy + the loaded keys = strict verification. NEVER AutoAddPolicy.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    def _connect() -> None:
        kwargs: dict[str, object] = {
            "port": port,
            "username": username,
            "timeout": timeout,
            # Do not silently fall back to ssh-agent or ~/.ssh/id_* keys: the
            # auth method is exactly what .env.deploy asked for.
            "allow_agent": False,
            "look_for_keys": False,
        }
        if key_path:
            kwargs["key_filename"] = key_path
            if key_passphrase:
                kwargs["passphrase"] = key_passphrase
        else:
            kwargs["password"] = password
        client.connect(host, **kwargs)

    try:
        _connect()
    except paramiko.BadHostKeyException as exc:
        expected = getattr(exc, "expected_key", None)
        offered = getattr(exc, "key", None)
        print(f"ERROR: host key MISMATCH for {host}:{port}", file=sys.stderr)
        print(f"  pinned : {fingerprint(expected) if expected else '?'}", file=sys.stderr)
        print(f"  offered: {fingerprint(offered) if offered else '?'}", file=sys.stderr)
        print(
            "  Either the server was rebuilt or this is a man-in-the-middle.\n"
            f"  Verify out-of-band, then remove the stale line from {known_path}.",
            file=sys.stderr,
        )
        client.close()
        raise SystemExit(1) from exc
    except paramiko.AuthenticationException as exc:
        print(f"ERROR: SSH authentication failed for {username}@{host}:{port}", file=sys.stderr)
        if key_path:
            print(
                f"  check DEPLOY_KEY_PATH={key_path} and DEPLOY_KEY_PASSPHRASE",
                file=sys.stderr,
            )
        else:
            print(
                "  check DEPLOY_PASSWORD (or switch to key auth with DEPLOY_KEY_PATH)",
                file=sys.stderr,
            )
        client.close()
        raise SystemExit(1) from exc
    except paramiko.SSHException as exc:
        # Most likely RejectPolicy refusing an unpinned server key. Probe the
        # offered key so the user gets a fingerprint instead of a bare traceback.
        try:
            offered = _probe_remote_key(host, port, timeout=timeout)
        except OSError:
            print(f"ERROR: cannot reach {host}:{port}: {exc}", file=sys.stderr)
            client.close()
            raise SystemExit(1) from exc
        if not trusts_new_host():
            _refuse_unknown_host(host, port, offered, known_path)  # raises SystemExit
        _trust_new_host(host, port, offered, known_path)
        client.get_host_keys().add(_known_host_name(host, port), offered.get_name(), offered)
        try:
            _connect()
        except (paramiko.SSHException, OSError) as retry_exc:
            print(f"ERROR: still cannot connect after pinning: {retry_exc}", file=sys.stderr)
            client.close()
            raise SystemExit(1) from retry_exc
    print(f"  authenticated as {username} ({'key ' + key_path if key_path else 'password'})")
    return client
