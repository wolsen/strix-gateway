# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Output formatting helpers for HPE 3PAR InForm OS CLI.

3PAR uses fixed-width column-aligned output with a dash separator row
beneath the header — unlike SVC which uses tab or exclamation-mark
delimited output.

Example::

    Id Name   CPG  Size_MB
    -- -----  ---  -------
     0 vol-0  gold   10240
     1 vol-1  gold   20480
"""

from __future__ import annotations

from typing import Any


def format_table(rows: list[dict[str, Any]]) -> str:
    """Format a list of dicts as a padded column-aligned table.

    Returns an empty string when *rows* is empty.
    """
    if not rows:
        return ""

    headers = list(rows[0].keys())

    # Compute max width per column (header vs data).
    widths: dict[str, int] = {}
    for h in headers:
        w = len(str(h))
        for row in rows:
            w = max(w, len(str(row.get(h, ""))))
        widths[h] = w

    # Header row
    hdr = "  ".join(str(h).ljust(widths[h]) for h in headers)
    # Separator row (dashes)
    sep = "  ".join("-" * widths[h] for h in headers)
    # Data rows
    data_lines: list[str] = []
    for row in rows:
        cells = []
        for h in headers:
            val = str(row.get(h, ""))
            cells.append(val.rjust(widths[h]) if _is_numeric_col(rows, h) else val.ljust(widths[h]))
        data_lines.append("  ".join(cells))

    return "\n".join([hdr, sep, *data_lines])


def format_detail(fields: dict[str, Any]) -> str:
    """Format a single object as aligned ``Key : Value`` pairs.

    3PAR ``show*`` detail output uses this format::

        Name : my-vol
        CPG  : gold
        Size : 10240 MiB
    """
    if not fields:
        return ""
    max_key = max(len(str(k)) for k in fields)
    return "\n".join(
        f"{str(k).ljust(max_key)} : {v}" for k, v in fields.items()
    )


def _is_numeric_col(rows: list[dict[str, Any]], key: str) -> bool:
    """Heuristic: right-align columns where all values are numeric."""
    for row in rows:
        val = str(row.get(key, ""))
        if val and not val.replace(".", "").replace("-", "").isdigit():
            return False
    return True
