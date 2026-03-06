# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Cross-reference validation for Apollo Gateway topology specifications.

:func:`validate` performs structural checks across all sections of a
:class:`~apollo_gateway.topology.schema.TopologySpec` and returns a list of
human-readable error strings.  An empty list means the spec is valid.

Example::

    spec = load_yaml("examples/ci/single_svc.yaml")
    errors = validate(spec)
    if errors:
        for err in errors:
            print(f"  ERROR: {err}")
"""

from __future__ import annotations

from apollo_gateway.topology.schema import TopologySpec


def validate(spec: TopologySpec) -> list[str]:
    """Validate a :class:`~apollo_gateway.topology.schema.TopologySpec`.

    Returns
    -------
    list[str]
        List of error messages.  Empty list means the spec is valid.
    """
    errors: list[str] = []

    # 1. Array names must be unique
    seen_arrays: set[str] = set()
    for arr in spec.arrays:
        if arr.name in seen_arrays:
            errors.append(f"Duplicate array name: '{arr.name}'")
        seen_arrays.add(arr.name)

    # 2. Pool names must be unique within each array; array must exist
    pool_map: dict[str, str] = {}   # pool_name → array_name (for volume lookup)
    seen_pool_keys: set[tuple[str, str]] = set()
    for pool in spec.pools:
        if pool.array not in seen_arrays:
            errors.append(
                f"Pool '{pool.name}' references unknown array '{pool.array}'"
            )
        key = (pool.array, pool.name)
        if key in seen_pool_keys:
            errors.append(
                f"Duplicate pool name '{pool.name}' in array '{pool.array}'"
            )
        seen_pool_keys.add(key)
        pool_map[pool.name] = pool.array

    # 3. Host names must be unique
    seen_hosts: set[str] = set()
    for host in spec.hosts:
        if host.name in seen_hosts:
            errors.append(f"Duplicate host name: '{host.name}'")
        seen_hosts.add(host.name)

    # 4. Volume names must be unique; pool must exist
    seen_volumes: set[str] = set()
    for vol in spec.volumes:
        if vol.name in seen_volumes:
            errors.append(f"Duplicate volume name: '{vol.name}'")
        seen_volumes.add(vol.name)
        if vol.pool not in pool_map:
            errors.append(
                f"Volume '{vol.name}' references unknown pool '{vol.pool}'"
            )

    # 5. Mapping checks: host and volume must exist
    for mapping in spec.mappings:
        if mapping.host not in seen_hosts:
            errors.append(
                f"Mapping references unknown host '{mapping.host}'"
            )
        if mapping.volume not in seen_volumes:
            errors.append(
                f"Mapping references unknown volume '{mapping.volume}'"
            )

    return errors
