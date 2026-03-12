# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Output formatting helpers for IBM SVC-compatible output."""

from __future__ import annotations

from typing import Any


def format_table(rows: list[dict[str, Any]], delim: str = "\t") -> str:
    """Format a list of dicts as a delimited table (header + data rows).

    When ``delim`` is ``"\\t"`` the output matches the SVC no-delim style.
    Pass ``"!"`` (or whatever ``-delim`` flag the caller received) to
    produce the ``-delim``-separated format that the Cinder CLIResponse
    parser expects for ``with_header=True``.
    """
    if not rows:
        return ""
    headers = list(rows[0].keys())
    lines = [delim.join(str(h) for h in headers)]
    for row in rows:
        lines.append(delim.join(str(row.get(h, "")) for h in headers))
    return "\n".join(lines)


def format_delim(fields: dict[str, Any], delim: str = "!") -> str:
    """Format a single object as key<delim>value lines (IBM -delim style)."""
    return "\n".join(f"{k}{delim}{v}" for k, v in fields.items())
