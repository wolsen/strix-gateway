# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Topology specification package for Strix Gateway.

Provides YAML/TOML-driven declarative configuration of arrays, endpoints,
pools, volumes, hosts, and mappings.  Typical usage::

    from strix_gateway.topology.load import load_yaml
    from strix_gateway.topology.validate import validate
    from strix_gateway.topology.apply import apply_topology

    spec = load_yaml("examples/ci/single_svc.yaml")
    errors = validate(spec)
    if errors:
        raise ValueError("\\n".join(errors))
    summary = await apply_topology(spec, session, spdk_client, settings)
"""
