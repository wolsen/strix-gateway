# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Output formatting for CLI commands.

Supports three modes: table (human-friendly), json (machine-readable),
and yaml (human + machine).
"""

from __future__ import annotations

import json as _json
from enum import Enum
from typing import Any

import yaml as _yaml


class OutputFormat(str, Enum):
    table = "table"
    json = "json"
    yaml = "yaml"


def render(data: Any, fmt: OutputFormat, columns: list[str] | None = None) -> None:
    """Render *data* to stdout in the requested format."""
    if fmt == OutputFormat.json:
        _render_json(data)
    elif fmt == OutputFormat.yaml:
        _render_yaml(data)
    else:
        _render_table(data, columns)


# ------------------------------------------------------------------
# JSON
# ------------------------------------------------------------------

def _render_json(data: Any) -> None:
    print(_json.dumps(data, indent=2, default=str))


# ------------------------------------------------------------------
# YAML
# ------------------------------------------------------------------

def _render_yaml(data: Any) -> None:
    print(_yaml.dump(data, default_flow_style=False, sort_keys=False).rstrip())


# ------------------------------------------------------------------
# Table
# ------------------------------------------------------------------

def _render_table(data: Any, columns: list[str] | None = None) -> None:
    if isinstance(data, dict):
        _render_kv_table(data)
        return
    if not isinstance(data, list):
        print(data)
        return
    if not data:
        print("(no results)")
        return

    try:
        from rich.console import Console
        from rich.table import Table

        _render_rich_table(data, columns)
    except ImportError:
        _render_simple_table(data, columns)


def _render_kv_table(record: dict[str, Any]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(show_header=False, box=None)
        table.add_column("Key", style="bold")
        table.add_column("Value")
        for k, v in record.items():
            table.add_row(str(k), str(v))
        console.print(table)
    except ImportError:
        max_key = max(len(str(k)) for k in record) if record else 0
        for k, v in record.items():
            print(f"{str(k).ljust(max_key)}  {v}")


def _render_rich_table(rows: list[dict[str, Any]], columns: list[str] | None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    cols = columns or list(rows[0].keys())
    table = Table()
    for c in cols:
        table.add_column(c.upper())
    for row in rows:
        table.add_row(*(str(row.get(c, "")) for c in cols))
    console.print(table)


def _render_simple_table(rows: list[dict[str, Any]], columns: list[str] | None) -> None:
    cols = columns or list(rows[0].keys())
    widths = {c: len(c) for c in cols}
    for row in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(row.get(c, ""))))

    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    print(header)
    print("  ".join("-" * widths[c] for c in cols))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in cols))
