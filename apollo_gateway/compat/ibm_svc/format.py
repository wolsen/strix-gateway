# FILE: apollo_gateway/compat/ibm_svc/format.py
"""Output formatting helpers for IBM SVC-compatible output.

IBM SVC wire format rules
-------------------------
List commands (no object name given):
    Tab-delimited table with a header row.  Each subsequent line is one
    object.  Columns match the order of keys in the first dict.

Single-object queries (object name given or returned by a create command):
    Key<delim>value pairs, one per line.  The default delimiter is "!".
    When the caller passes ``-delim X`` the user-supplied character is used
    instead.
"""

from __future__ import annotations

from typing import Any


def format_table(rows: list[dict[str, Any]]) -> str:
    """Format a list of dicts as a tab-delimited table (header + data rows).

    Returns an empty string if *rows* is empty (no header emitted).
    Column order follows the key insertion order of the first dict.
    """
    if not rows:
        return ""
    headers = list(rows[0].keys())
    lines = ["\t".join(str(h) for h in headers)]
    for row in rows:
        lines.append("\t".join(str(row.get(h, "")) for h in headers))
    return "\n".join(lines)


def format_delim(fields: dict[str, Any], delim: str = "!") -> str:
    """Format a single object as ``key<delim>value`` lines (IBM -delim style).

    *delim* defaults to ``"!"``; pass the user-supplied character when the
    command includes ``-delim X``.
    """
    return "\n".join(f"{k}{delim}{v}" for k, v in fields.items())
