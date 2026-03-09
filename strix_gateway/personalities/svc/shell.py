# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Entry point for the strix-svc-shell ForceCommand.

This shell forwards SSH commands to the running gateway over ``/v1/svc/run``
so execution uses the gateway process state (DB + SPDK wiring).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import httpx

from strix_gateway.personalities.svc.audit import parse_ssh_connection

logger = logging.getLogger("strix_gateway.personalities.svc.shell")


async def _main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--subsystem", default="default")
    args, _ = parser.parse_known_args()

    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not cmd:
        print("Interactive sessions are not permitted on this account.", file=sys.stderr)
        return 1

    from strix_gateway.config import settings

    remote_user = os.environ.get("USER", "svc")
    remote_addr, remote_port = parse_ssh_connection()

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{settings.api_base_url}/v1/svc/run",
                json={
                    "array": args.subsystem,
                    "command": cmd,
                    "remote_user": remote_user,
                    "remote_addr": remote_addr,
                    "remote_port": remote_port,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("stdout"):
                sys.stdout.write(data["stdout"])
            if data.get("stderr"):
                sys.stderr.write(data["stderr"])
            return int(data.get("exit_code", 1))
    except httpx.HTTPError as exc:
        print(f"Gateway connection failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("SVC shell execution failed")
        print(f"Gateway execution failed: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
