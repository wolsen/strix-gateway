# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Idempotent apply and smoke-test logic for topology files.

The apply order is intentionally deterministic:
    1. arrays (+ their endpoints)
    2. pools
    3. hosts
    4. volumes
    5. mappings
    6. faults / delays
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strix_gateway.cli.errors import APIError, ValidationError

if TYPE_CHECKING:
    from strix_gateway.cli.client import StrixClient
    from strix_gateway.cli.topo.models import TopologyFile


# ------------------------------------------------------------------
# Apply
# ------------------------------------------------------------------

def apply_topology(
    client: "StrixClient",
    topo: "TopologyFile",
    *,
    strict: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Idempotently ensure every resource in *topo* exists.

    Returns a list of human-readable action strings.
    """
    actions: list[str] = []

    # 1) Arrays + endpoints
    existing_arrays = {a["name"]: a for a in client.list_arrays()}
    for spec in topo.arrays:
        if spec.name in existing_arrays:
            existing = existing_arrays[spec.name]
            # Update vendor/profile if they differ from the topology spec.
            updates: dict[str, object] = {}
            if spec.vendor and existing.get("vendor") != spec.vendor:
                updates["vendor"] = spec.vendor
            existing_profile = existing.get("profile") or {}
            if spec.profile and existing_profile != spec.profile:
                updates["profile"] = spec.profile
            if updates:
                client.update_array(spec.name, **updates)
                actions.append(f"updated array '{spec.name}': {updates}")
            else:
                actions.append(f"array '{spec.name}' already exists")
        else:
            client.create_array(
                spec.name,
                vendor=spec.vendor,
                profile=spec.profile or None,
            )
            actions.append(f"created array '{spec.name}'")

        # Ensure declared endpoints exist and have correct targets/addresses
        existing_eps = client.list_endpoints(spec.name)
        existing_by_proto = {ep["protocol"]: ep for ep in existing_eps}
        for ep_spec in spec.endpoints:
            existing_ep = existing_by_proto.get(ep_spec.protocol)
            if existing_ep is not None:
                # Check whether targets/addresses/auth need updating
                ep_updates: dict[str, object] = {}
                if ep_spec.targets and existing_ep.get("targets") != ep_spec.targets:
                    ep_updates["targets"] = ep_spec.targets
                if ep_spec.addresses and existing_ep.get("addresses") != ep_spec.addresses:
                    ep_updates["addresses"] = ep_spec.addresses
                if ep_spec.auth and existing_ep.get("auth") != ep_spec.auth:
                    ep_updates["auth"] = ep_spec.auth
                if ep_updates:
                    client.update_endpoint(
                        spec.name,
                        existing_ep["id"],
                        targets=ep_updates.get("targets"),
                        addresses=ep_updates.get("addresses"),
                        auth=ep_updates.get("auth"),
                    )
                    actions.append(
                        f"updated endpoint '{ep_spec.protocol}' on "
                        f"'{spec.name}': {ep_updates}"
                    )
                else:
                    actions.append(
                        f"endpoint '{ep_spec.protocol}' on '{spec.name}' "
                        "already exists"
                    )
            else:
                client.create_endpoint(
                    spec.name,
                    protocol=ep_spec.protocol,
                    targets=ep_spec.targets,
                    addresses=ep_spec.addresses,
                    auth=ep_spec.auth,
                )
                actions.append(
                    f"created endpoint '{ep_spec.protocol}' on "
                    f"'{spec.name}'"
                )

    # 2) Pools
    for spec in topo.pools:
        try:
            client.resolve_pool(spec.name, spec.array)
            actions.append(
                f"pool '{spec.name}' already exists in '{spec.array}'"
            )
        except (ValidationError, APIError):
            client.create_pool(
                spec.name,
                spec.array,
                spec.backend,
                spec.size_gb,
                spec.aio_path,
            )
            actions.append(
                f"created pool '{spec.name}' in '{spec.array}'"
            )

    # 3) Hosts
    existing_hosts = {h["name"]: h for h in client.list_hosts()}
    for spec in topo.hosts:
        if spec.name in existing_hosts:
            actions.append(f"host '{spec.name}' already exists")
        else:
            client.create_host(
                spec.name,
                iqns=spec.iqns or None,
                nqns=spec.nqns or None,
                wwpns=spec.wwpns or None,
            )
            actions.append(f"created host '{spec.name}'")

    # 4) Volumes
    for spec in topo.volumes:
        array = _array_for_volume(topo, spec.pool)
        try:
            client.resolve_volume(spec.name, array)
            actions.append(
                f"volume '{spec.name}' already exists in '{array}'"
            )
        except (ValidationError, APIError):
            pool = client.resolve_pool(spec.pool, array)
            client.create_volume(spec.name, pool["id"], spec.size_gb)
            actions.append(
                f"created volume '{spec.name}' in pool '{spec.pool}'"
            )

    # 5) Mappings
    for spec in topo.mappings:
        array = _array_for_mapping(topo, spec.volume)
        try:
            client.resolve_mapping(spec.host, spec.volume, array)
            actions.append(
                f"mapping {spec.host}\u2192{spec.volume} already exists"
            )
        except (ValidationError, APIError):
            host = client.resolve_host(spec.host)
            volume = client.resolve_volume(spec.volume, array)
            client.create_mapping(
                volume["id"], host["id"], protocol=spec.protocol,
            )
            actions.append(
                f"created mapping {spec.host}\u2192{spec.volume} "
                f"({spec.protocol})"
            )

    # 6) Faults / delays
    for f in topo.faults:
        try:
            client.post(
                "/admin/faults",
                json={
                    "operation": f.operation,
                    "error_message": f.error_message,
                },
            )
            actions.append(f"injected fault on '{f.operation}'")
        except APIError:
            actions.append(
                f"failed to inject fault on '{f.operation}' "
                "(endpoint may not exist)"
            )

    for d in topo.delays:
        try:
            client.post(
                "/admin/delays",
                json={
                    "operation": d.operation,
                    "delay_seconds": d.delay_seconds,
                },
            )
            actions.append(f"injected delay on '{d.operation}'")
        except APIError:
            actions.append(
                f"failed to inject delay on '{d.operation}' "
                "(endpoint may not exist)"
            )

    # Strict mode — report live resources not declared in topology
    if strict:
        _strict_report(client, topo, actions)

    return actions


def _strict_report(
    client: "StrixClient",
    topo: "TopologyFile",
    actions: list[str],
) -> None:
    for spec in topo.arrays:
        topo_pool_names = {
            p.name for p in topo.pools if p.array == spec.name
        }
        for lp in client.list_pools(array=spec.name):
            if lp["name"] not in topo_pool_names:
                actions.append(
                    f"STRICT: pool '{lp['name']}' in '{spec.name}' "
                    "not in topology file"
                )

        topo_vol_names: set[str] = set()
        for v in topo.volumes:
            pool_array = next(
                (p.array for p in topo.pools if p.name == v.pool), None
            )
            if pool_array == spec.name:
                topo_vol_names.add(v.name)
        for lv in client.list_volumes(array=spec.name):
            if lv["name"] not in topo_vol_names:
                actions.append(
                    f"STRICT: volume '{lv['name']}' in '{spec.name}' "
                    "not in topology file"
                )


# ------------------------------------------------------------------
# Smoke test
# ------------------------------------------------------------------

def smoke_test(
    client: "StrixClient",
    topo: "TopologyFile",
    *,
    verbose: bool = False,
) -> list[str]:
    """Run existence checks for every declared resource.

    Returns a list of check result strings (prefixed with ``\u2713`` or ``\u2717``).
    """
    results: list[str] = []

    for spec in topo.arrays:
        try:
            client.get_array(spec.name)
            results.append(f"\u2713 array '{spec.name}' exists")
        except APIError:
            results.append(f"\u2717 array '{spec.name}' NOT FOUND")

    for spec in topo.pools:
        try:
            client.resolve_pool(spec.name, spec.array)
            results.append(
                f"\u2713 pool '{spec.name}' in '{spec.array}' exists"
            )
        except (ValidationError, APIError):
            results.append(
                f"\u2717 pool '{spec.name}' in '{spec.array}' NOT FOUND"
            )

    for spec in topo.volumes:
        array = _array_for_volume(topo, spec.pool)
        try:
            client.resolve_volume(spec.name, array)
            results.append(
                f"\u2713 volume '{spec.name}' in '{array}' exists"
            )
        except (ValidationError, APIError):
            results.append(
                f"\u2717 volume '{spec.name}' in '{array}' NOT FOUND"
            )

    for spec in topo.mappings:
        array = _array_for_mapping(topo, spec.volume)
        try:
            client.resolve_mapping(spec.host, spec.volume, array)
            results.append(
                f"\u2713 mapping {spec.host}\u2192{spec.volume} exists"
            )
        except (ValidationError, APIError):
            results.append(
                f"\u2717 mapping {spec.host}\u2192{spec.volume} NOT FOUND"
            )

    return results


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _array_for_volume(topo: "TopologyFile", pool_name: str) -> str:
    """Derive array name from a pool name in the topology."""
    for p in topo.pools:
        if p.name == pool_name:
            return p.array
    return "default"


def _array_for_mapping(topo: "TopologyFile", volume_name: str) -> str:
    """Derive array name from a volume's pool chain."""
    pool_name = next(
        (v.pool for v in topo.volumes if v.name == volume_name), None
    )
    if pool_name:
        return _array_for_volume(topo, pool_name)
    return "default"
