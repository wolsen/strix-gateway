# FILE: apollo_gateway/compat/ibm_svc/shell.py
"""Entry point for the /usr/local/bin/apollo-svc-shell ForceCommand.

The sshd ForceCommand runs this module for every connection made by the
``svc`` user.  It reads SSH_ORIGINAL_COMMAND from the environment, parses
it, dispatches to the appropriate IBM SVC façade handler, and emits a
structured audit record.

Design for testability
-----------------------
``dispatch()``
    Pure parse-and-dispatch logic.  Tests call this directly with a
    pre-built :class:`~apollo_gateway.compat.ibm_svc.handlers.SvcContext`.

``_audited_dispatch()``
    Wraps ``dispatch()`` with byte-counting streams and audit-record
    emission.  Tests that want to assert on log output use this.

``_main()``
    Full entrypoint: reads OS environment, initialises DB + SPDK, calls
    ``_audited_dispatch()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from apollo_gateway.compat.ibm_svc.audit import (
    InvocationRecord,
    SvcAuditLogger,
    _CountingWriter,
    parse_ssh_connection,
    redact_argv,
)
from apollo_gateway.compat.ibm_svc.errors import SvcError, SvcUnknownCommandError
from apollo_gateway.compat.ibm_svc.handlers import (
    SVCINFO_HANDLERS,
    SVCTASK_HANDLERS,
    SvcContext,
)
from apollo_gateway.compat.ibm_svc.parse import parse_ssh_command

logger = logging.getLogger("apollo_gateway.compat.ibm_svc.shell")


# ---------------------------------------------------------------------------
# Pure dispatcher (no I/O side-effects beyond stdout/stderr + DB/SPDK)
# ---------------------------------------------------------------------------

async def dispatch(cmd_str: str, ctx: SvcContext) -> int:
    """Parse *cmd_str*, call the matching handler, write output, return exit code.

    Parameters
    ----------
    cmd_str:
        Raw value of ``SSH_ORIGINAL_COMMAND`` (already stripped).
    ctx:
        Pre-initialised :class:`~apollo_gateway.compat.ibm_svc.handlers.SvcContext`
        carrying a live ``AsyncSession`` and ``SPDKClient``.

    Returns
    -------
    int
        Exit code: ``0`` on success, ``1`` on any error.
    """
    try:
        pc = parse_ssh_command(cmd_str)
    except SvcError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code

    if pc.verb == "svcinfo":
        table = SVCINFO_HANDLERS
    else:
        table = SVCTASK_HANDLERS

    handler = table.get(pc.subcommand)
    if handler is None:
        err = SvcUnknownCommandError(f"{pc.verb} {pc.subcommand}")
        print(str(err), file=sys.stderr)
        return err.exit_code

    try:
        output = await handler(ctx, pc)  # type: ignore[operator]
        if output:
            print(output)
        return 0
    except SvcError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        logger.exception("Unhandled error in handler %s %s", pc.verb, pc.subcommand)
        print(f"Internal error: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Auditing wrapper
# ---------------------------------------------------------------------------

async def _audited_dispatch(
    cmd_str: str,
    ctx: SvcContext,
    audit: SvcAuditLogger,
    *,
    remote_user: str = "svc",
    remote_addr: Optional[str] = None,
    remote_port: Optional[str] = None,
) -> int:
    """Wrap :func:`dispatch` with byte-counting streams and audit-record emission.

    Parameters
    ----------
    cmd_str:
        Raw ``SSH_ORIGINAL_COMMAND`` string.
    ctx:
        Initialised :class:`~apollo_gateway.compat.ibm_svc.handlers.SvcContext`.
    audit:
        Configured :class:`~apollo_gateway.compat.ibm_svc.audit.SvcAuditLogger`.
    remote_user:
        Authenticated SSH username (from ``USER`` env or ``"svc"`` default).
    remote_addr:
        Client IP address (from ``SSH_CONNECTION``), or ``None``.
    remote_port:
        Client TCP port (from ``SSH_CONNECTION``), or ``None``.

    Returns
    -------
    int
        Exit code from :func:`dispatch`.
    """
    req_id = str(uuid.uuid4())

    # Build the redacted argv for logging
    try:
        raw_argv = shlex.split(cmd_str)
    except ValueError:
        raw_argv = cmd_str.split()
    argv_redacted = redact_argv(raw_argv)

    # Wrap stdout and stderr so we can measure bytes written
    out_ctr = _CountingWriter(sys.stdout)
    err_ctr = _CountingWriter(sys.stderr)
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_ctr, err_ctr  # type: ignore[assignment]

    t0 = time.monotonic()
    try:
        exit_code = await dispatch(cmd_str, ctx)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err

    duration_ms = int((time.monotonic() - t0) * 1000)

    audit.emit(
        InvocationRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            req_id=req_id,
            remote_user=remote_user,
            remote_addr=remote_addr,
            remote_port=remote_port,
            command_raw=cmd_str,
            argv=argv_redacted,
            duration_ms=duration_ms,
            exit_code=exit_code,
            stdout_len=out_ctr.byte_count,
            stderr_len=err_ctr.byte_count,
        )
    )
    return exit_code


# ---------------------------------------------------------------------------
# Real-world entrypoint
# ---------------------------------------------------------------------------

async def _main() -> int:
    """Full entrypoint: read OS environment, initialise context, dispatch.

    Reads:
      - ``SSH_ORIGINAL_COMMAND`` — the command the SSH client tried to run.
      - ``SSH_CONNECTION`` / ``SSH_CLIENT`` — for remote address logging.
      - ``USER`` — authenticated SSH username.
      - Apollo settings via :mod:`apollo_gateway.config`.

    Rejects interactive sessions (no ``SSH_ORIGINAL_COMMAND``) before
    initialising the database or SPDK client.
    """
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    remote_user = os.environ.get("USER", "svc")
    remote_addr, remote_port = parse_ssh_connection()

    audit = SvcAuditLogger()
    audit.configure()

    if not cmd:
        audit.emit(
            InvocationRecord(
                ts=datetime.now(timezone.utc).isoformat(),
                req_id=str(uuid.uuid4()),
                remote_user=remote_user,
                remote_addr=remote_addr,
                remote_port=remote_port,
                command_raw="",
                argv=[],
                duration_ms=0,
                exit_code=1,
                stdout_len=0,
                stderr_len=0,
                error="rejected: no SSH_ORIGINAL_COMMAND",
            )
        )
        print(
            "Interactive sessions are not permitted on this account.",
            file=sys.stderr,
        )
        return 1

    # Lazy imports so tests can patch before importing
    from apollo_gateway.config import settings
    from apollo_gateway.core.db import get_session_factory, init_db
    from apollo_gateway.spdk.rpc import SPDKClient

    await init_db(settings.database_url)
    factory = get_session_factory()
    spdk = SPDKClient(settings.spdk_socket_path)

    async with factory() as session:
        ctx = SvcContext(session=session, spdk=spdk)
        return await _audited_dispatch(
            cmd,
            ctx,
            audit,
            remote_user=remote_user,
            remote_addr=remote_addr,
            remote_port=remote_port,
        )


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
