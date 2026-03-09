# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Load topology files in YAML or TOML format."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from strix_gateway.cli.errors import ValidationError
from strix_gateway.cli.topo.models import TopologyFile


def load_topology(path: str) -> TopologyFile:
    """Parse a topology file and return a validated :class:`TopologyFile`.

    Supported extensions: ``.yaml``, ``.yml``, ``.toml``.

    Raises :class:`~strix_gateway.cli.errors.ValidationError` on I/O or
    schema errors.
    """
    p = Path(path)
    if not p.exists():
        raise ValidationError(f"File not found: {path}")

    suffix = p.suffix.lower()

    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(p.read_text())
    elif suffix == ".toml":
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
    else:
        raise ValidationError(
            f"Unsupported file format '{suffix}'. Use .yaml, .yml, or .toml"
        )

    if not isinstance(data, dict):
        raise ValidationError("Topology file must be a YAML/TOML mapping at top level")

    try:
        return TopologyFile.model_validate(data)
    except Exception as exc:
        raise ValidationError(f"Topology schema error: {exc}") from exc


def load_capability_file(path: str) -> dict:
    """Load a capability profile override file (YAML or TOML) and return a raw dict."""
    p = Path(path)
    if not p.exists():
        raise ValidationError(f"File not found: {path}")

    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return yaml.safe_load(p.read_text()) or {}
    elif suffix == ".toml":
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    else:
        raise ValidationError(
            f"Unsupported file format '{suffix}'. Use .yaml, .yml, or .toml"
        )
