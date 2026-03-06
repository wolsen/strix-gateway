# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Cross-reference and constraint validation for topology files."""

from __future__ import annotations

from apollo_gateway.cli.topo.models import TopologyFile


_VALID_PROTOCOLS = {"iscsi", "nvmeof_tcp", "fc"}


def validate_topology(topo: TopologyFile) -> list[str]:
    """Validate a parsed topology and return a list of error strings.

    An empty list means the topology is valid.
    """
    errors: list[str] = []

    array_names: set[str] = set()
    pool_names: set[str] = set()
    pool_array: dict[str, str] = {}
    host_names: set[str] = set()
    volume_names: set[str] = set()
    volume_pool: dict[str, str] = {}

    # ---- Arrays (uniqueness + endpoint protocols) --------------------
    for a in topo.arrays:
        if a.name in array_names:
            errors.append(f"Duplicate array name: '{a.name}'")
        array_names.add(a.name)
        for ep in a.endpoints:
            if ep.protocol not in _VALID_PROTOCOLS:
                errors.append(
                    f"Array '{a.name}': unknown endpoint protocol "
                    f"'{ep.protocol}'"
                )

    # ---- Pools (reference array, unique within array) ----------------
    pools_per_array: dict[str, set[str]] = {}
    for p in topo.pools:
        if p.array not in array_names:
            errors.append(
                f"Pool '{p.name}' references unknown array '{p.array}'"
            )
        arr_pools = pools_per_array.setdefault(p.array, set())
        if p.name in arr_pools:
            errors.append(
                f"Duplicate pool name '{p.name}' in array '{p.array}'"
            )
        arr_pools.add(p.name)
        pool_names.add(p.name)
        pool_array[p.name] = p.array

    # ---- Hosts (uniqueness) ------------------------------------------
    for h in topo.hosts:
        if h.name in host_names:
            errors.append(f"Duplicate host name: '{h.name}'")
        host_names.add(h.name)

    # ---- Volumes (reference pool, unique within array) ---------------
    vols_per_array: dict[str, set[str]] = {}
    for v in topo.volumes:
        if v.pool not in pool_names:
            errors.append(
                f"Volume '{v.name}' references unknown pool '{v.pool}'"
            )
        else:
            arr = pool_array[v.pool]
            arr_vols = vols_per_array.setdefault(arr, set())
            if v.name in arr_vols:
                errors.append(
                    f"Duplicate volume name '{v.name}' in array '{arr}'"
                )
            arr_vols.add(v.name)
        volume_names.add(v.name)
        volume_pool[v.name] = v.pool

    # ---- Mappings (host + volume references) -------------------------
    for m in topo.mappings:
        if m.host not in host_names:
            errors.append(f"Mapping references unknown host '{m.host}'")
        if m.volume not in volume_names:
            errors.append(f"Mapping references unknown volume '{m.volume}'")

    return errors
