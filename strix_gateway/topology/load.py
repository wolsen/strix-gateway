# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Loaders for Strix Gateway topology specification files.

Supports YAML (``.yaml`` / ``.yml``) and TOML (``.toml``) formats.

Example::

    from strix_gateway.topology.load import load_yaml, load_toml

    spec = load_yaml("examples/ci/single_svc.yaml")
    spec = load_toml("examples/ci/single_svc.toml")
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Union

import yaml

from strix_gateway.topology.schema import TopologySpec


def load_yaml(path: Union[str, Path]) -> TopologySpec:
    """Load a topology spec from a YAML file.

    Parameters
    ----------
    path:
        Path to the ``.yaml`` / ``.yml`` file.

    Returns
    -------
    TopologySpec
        Validated topology specification.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return TopologySpec.model_validate(data)


def load_toml(path: Union[str, Path]) -> TopologySpec:
    """Load a topology spec from a TOML file.

    Parameters
    ----------
    path:
        Path to the ``.toml`` file.

    Returns
    -------
    TopologySpec
        Validated topology specification.
    """
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return TopologySpec.model_validate(data)
