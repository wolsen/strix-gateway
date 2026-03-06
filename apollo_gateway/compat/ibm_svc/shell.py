# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Entry point for the /usr/local/bin/apollo-svc-shell ForceCommand.

The sshd ForceCommand runs this module for every connection made by the
``svc`` user.  It reads SSH_ORIGINAL_COMMAND from the environment, forwards
the command to the running gateway via ``POST /v1/svc/run``, and streams
the result back through sshd.

``_main()``
    Thin HTTP-client entrypoint: reads OS environment, posts to the gateway
    API, streams stdout/stderr back through the SSH channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from apollo_gateway.compat.ibm_svc.audit import parse_ssh_connection

logger = logging.getLogger("apollo_gateway.compat.ibm_svc.shell")


# ---------------------------------------------------------------------------
# Real-world entrypoint
# ---------------------------------------------------------------------------

async def _main() -> int:
    """ForceCommand entrypoint: forward SSH_ORIGINAL_COMMAND to the gateway API.

    Reads:
      - ``SSH_ORIGINAL_COMMAND`` — the command the SSH client tried to run.
      - ``SSH_CONNECTION`` / ``SSH_CLIENT`` — for remote address logging.
      - ``USER`` — authenticated SSH username.
      - ``--subsystem <name>`` from ``sys.argv`` — virtual subsystem name
        (injected by sshd ``ForceCommand``), defaults to ``"default"``.
      - ``APOLLO_API_BASE_URL`` (via :class:`~apollo_gateway.config.Settings`) —
        base URL of the running gateway (default ``http://localhost:8080``).

    Rejects interactive sessions (no ``SSH_ORIGINAL_COMMAND``) immediately,
    without touching the database or SPDK.  All other commands are forwarded
    to ``POST /v1/svc/run`` on the running gateway.
    """
    import argparse

    import httpx

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--subsystem", default="default")
    args, _ = parser.parse_known_args()
    subsystem_name = args.subsystem

    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    remote_user = os.environ.get("USER", "svc")
    remote_addr, remote_port = parse_ssh_connection()

    if not cmd:
        print(
            "Interactive sessions are not permitted on this account.",
            file=sys.stderr,
        )
        return 1

    from apollo_gateway.config import settings

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{settings.api_base_url}/v1/svc/run",
                json={
                    "subsystem": subsystem_name,
                    "command": cmd,
                    "remote_user": remote_user,
                    "remote_addr": remote_addr,
                    "remote_port": remote_port,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        print(f"Gateway connection failed: {exc}", file=sys.stderr)
        return 1

    if data.get("stdout"):
        sys.stdout.write(data["stdout"])
    if data.get("stderr"):
        sys.stderr.write(data["stderr"])
    return data["exit_code"]


def main() -> None:
    """Synchronous wrapper called by the ``apollo-svc-shell`` script."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
