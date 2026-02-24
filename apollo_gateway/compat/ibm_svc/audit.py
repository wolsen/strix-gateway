# FILE: apollo_gateway/compat/ibm_svc/audit.py
"""Structured audit logging for the IBM SVC SSH façade.

Every SSH invocation produces one record written to two files:

  /var/log/apollo/ibm_svc_cli.jsonl  — JSON Lines (machine-readable)
  /var/log/apollo/ibm_svc_cli.log    — human-readable text

Records are also emitted at DEBUG level on the diagnostic logger
``"apollo_gateway.compat.ibm_svc"`` so they appear in container stdout
when the log level is DEBUG.

Sensitive-value redaction
--------------------------
argv[] is sanitised before logging: any flag whose name (case-insensitive,
leading dashes stripped) matches a known sensitive name has its immediately
following value replaced with ``"***"``.  The raw ``command_raw`` field is
never touched so operators can investigate syntax problems without losing
context, but the redacted ``argv`` is the only place values appear.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import TextIOBase
from pathlib import Path
from typing import IO, Optional, TextIO

_diag = logging.getLogger("apollo_gateway.compat.ibm_svc")

# ---------------------------------------------------------------------------
# Sensitive-flag redaction
# ---------------------------------------------------------------------------

#: Flag names (case-insensitive, without leading dashes) whose value should
#: be replaced with ``"***"`` in logged argv lists.
SENSITIVE_FLAGS: frozenset[str] = frozenset({
    "password",
    "passwd",
    "secret",
    "chapsecret",
    "chap_secret",
    "chappassword",
    "chap_password",
    "apikey",
    "api_key",
    "token",
    "authkey",
    "auth_key",
    "key",
    "privatekey",
    "private_key",
})


def redact_argv(argv: list[str]) -> list[str]:
    """Return a copy of *argv* with values after sensitive flags replaced by ``"***"``.

    Rules
    -----
    * A sensitive flag is any element that starts with one or more ``-``
      characters and whose stripped name (lowercase) is in :data:`SENSITIVE_FLAGS`.
    * The element immediately following a sensitive flag is replaced with
      ``"***"``, provided it does not itself start with ``-`` (i.e. it looks
      like a value, not another flag).
    * Boolean (value-less) sensitive flags are left as-is.
    * All other elements are copied verbatim.

    Examples
    --------
    >>> redact_argv(["mkvdisk", "-password", "secret123", "-name", "vol"])
    ['mkvdisk', '-password', '***', '-name', 'vol']
    >>> redact_argv(["login", "-nopassword"])   # boolean flag, unchanged
    ['login', '-nopassword']
    """
    result: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        stripped = tok.lstrip("-").lower()
        if tok.startswith("-") and stripped in SENSITIVE_FLAGS:
            result.append(tok)
            # Redact the following value if present and looks like a value
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                result.append("***")
                i += 2
            else:
                i += 1
        else:
            result.append(tok)
            i += 1
    return result


# ---------------------------------------------------------------------------
# SSH connection metadata
# ---------------------------------------------------------------------------

def parse_ssh_connection() -> tuple[Optional[str], Optional[str]]:
    """Return ``(remote_addr, remote_port)`` from the SSH environment.

    sshd sets ``SSH_CONNECTION`` to ``"raddr rport laddr lport"`` when a
    session is established.  Falls back to the older ``SSH_CLIENT`` variable
    (``"raddr rport lport"``) if ``SSH_CONNECTION`` is absent.

    Returns ``(None, None)`` when called outside an SSH session (e.g. in
    unit tests).
    """
    conn = os.environ.get("SSH_CONNECTION", "").strip()
    if conn:
        parts = conn.split()
        if len(parts) >= 2:
            return parts[0], parts[1]

    client = os.environ.get("SSH_CLIENT", "").strip()
    if client:
        parts = client.split()
        if len(parts) >= 2:
            return parts[0], parts[1]

    return None, None


# ---------------------------------------------------------------------------
# Invocation record
# ---------------------------------------------------------------------------

@dataclass
class InvocationRecord:
    """One structured audit record per SSH command invocation.

    All fields are JSON-serialisable by default.  ``error`` is ``None``
    for successful and expected-error outcomes; it is set only for events
    that never reached the dispatcher (e.g. rejected interactive sessions).
    """

    ts: str                          # ISO-8601 timestamp with UTC offset
    req_id: str                      # UUID4 string, unique per invocation
    remote_user: str                 # authenticated SSH username
    remote_addr: Optional[str]       # client IP (null outside SSH)
    remote_port: Optional[str]       # client TCP port (null outside SSH)
    command_raw: str                 # verbatim SSH_ORIGINAL_COMMAND
    argv: list[str]                  # shlex-split command with sensitive values redacted
    duration_ms: int                 # wall-clock milliseconds for dispatch()
    exit_code: int                   # 0 = success, 1 = error
    stdout_len: int                  # bytes written to stdout
    stderr_len: int                  # bytes written to stderr
    error: Optional[str] = field(default=None)
    # ^ set for pre-dispatch rejections (no command, auth failure, etc.)


# ---------------------------------------------------------------------------
# stdout / stderr byte counter
# ---------------------------------------------------------------------------

class _CountingWriter:
    """Transparent wrapper around a text stream that counts UTF-8 bytes written.

    Delegates all writes to the underlying stream while accumulating the byte
    length of every string passed to ``write()``.  Intended to wrap
    ``sys.stdout`` and ``sys.stderr`` temporarily during dispatch so the audit
    record can include accurate ``stdout_len`` / ``stderr_len`` values.
    """

    def __init__(self, underlying: TextIO) -> None:
        self._underlying = underlying
        self.byte_count: int = 0

    # TextIO interface -------------------------------------------------------

    def write(self, s: str) -> int:
        result = self._underlying.write(s)
        self.byte_count += len(s.encode("utf-8", errors="replace"))
        return result

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._underlying.flush()

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._underlying, "encoding", "utf-8")

    @property
    def errors(self) -> Optional[str]:
        return getattr(self._underlying, "errors", None)

    @property
    def closed(self) -> bool:
        return self._underlying.closed

    def fileno(self) -> int:
        # Raise UnsupportedOperation if the underlying stream has no fileno.
        return self._underlying.fileno()


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------

def _format_human(record: InvocationRecord) -> str:
    """Format *record* as a single human-readable log line."""
    addr = (
        f"{record.remote_addr}:{record.remote_port}"
        if record.remote_addr
        else "unknown"
    )
    short_id = record.req_id[:8]
    cmd_display = record.command_raw[:120] or "(no command)"
    parts = [
        record.ts,
        f"[{short_id}]",
        f"{record.remote_user}@{addr}",
        f'"{cmd_display}"',
        f"exit={record.exit_code}",
        f"{record.duration_ms}ms",
        f"out={record.stdout_len}B",
        f"err={record.stderr_len}B",
    ]
    if record.error:
        parts.insert(3, f"ERROR={record.error}")
    return " ".join(parts)


class SvcAuditLogger:
    """Appends :class:`InvocationRecord` objects to a JSON Lines file and a
    human-readable text file.

    Usage::

        audit = SvcAuditLogger()
        audit.configure()           # uses /var/log/apollo by default
        audit.emit(record)

    In tests, pass a ``tmp_path``::

        audit.configure(log_dir=tmp_path)

    If the log directory cannot be created (e.g. read-only filesystem), the
    logger degrades gracefully: it logs a diagnostic warning and skips writes.
    """

    LOG_DIR_DEFAULT: Path = Path("/var/log/apollo")
    JSONL_NAME: str = "ibm_svc_cli.jsonl"
    TEXT_NAME: str = "ibm_svc_cli.log"

    def __init__(self) -> None:
        self._jsonl_path: Optional[Path] = None
        self._text_path: Optional[Path] = None
        self._ready: bool = False

    def configure(self, log_dir: Optional[Path] = None) -> None:
        """Prepare log file paths, creating the directory if needed.

        Safe to call multiple times; subsequent calls reconfigure the paths.
        """
        base = log_dir if log_dir is not None else self.LOG_DIR_DEFAULT
        try:
            base.mkdir(parents=True, exist_ok=True)
            self._jsonl_path = base / self.JSONL_NAME
            self._text_path = base / self.TEXT_NAME
            self._ready = True
            _diag.debug("Audit logging configured → %s", base)
        except OSError as exc:
            _diag.warning(
                "Cannot create audit log directory %s: %s — audit logging disabled",
                base,
                exc,
            )
            self._ready = False

    def emit(self, record: InvocationRecord) -> None:
        """Append *record* to both log files and emit a DEBUG diagnostic log."""
        # Always emit to diagnostic logger regardless of file availability
        _diag.debug(
            "audit req_id=%s exit=%d dur=%dms %r",
            record.req_id,
            record.exit_code,
            record.duration_ms,
            record.command_raw,
        )

        if not self._ready:
            return

        obj = dataclasses.asdict(record)
        json_line = json.dumps(obj, separators=(",", ":"), default=str)
        human_line = _format_human(record)

        self._append(self._jsonl_path, json_line + "\n")   # type: ignore[arg-type]
        self._append(self._text_path, human_line + "\n")   # type: ignore[arg-type]

    def _append(self, path: Path, text: str) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            _diag.warning("Audit write error to %s: %s", path, exc)
