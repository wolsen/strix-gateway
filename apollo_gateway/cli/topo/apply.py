# FILE: apollo_gateway/cli/topo/apply.py
"""Idempotent apply and smoke-test logic for topology files.

The apply order is intentionally deterministic:
    1. subsystems
    2. pools
    3. hosts
    4. volumes
    5. mappings
    6. faults / delays
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apollo_gateway.cli.errors import APIError, ValidationError

if TYPE_CHECKING:
    from apollo_gateway.cli.client import ApolloClient
    from apollo_gateway.cli.topo.models import TopologyFile


# ------------------------------------------------------------------
# Apply
# ------------------------------------------------------------------

def apply_topology(
    client: "ApolloClient",
    topo: "TopologyFile",
    *,
    strict: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Idempotently ensure every resource in *topo* exists.

    Returns a list of human-readable action strings.
    """
    actions: list[str] = []

    # 1) Subsystems
    existing_subs = {s["name"]: s for s in client.list_subsystems()}
    for spec in topo.subsystems:
        if spec.name in existing_subs:
            actions.append(f"subsystem '{spec.name}' already exists")
        else:
            cap = (
                spec.capability_profile.model_dump(exclude_none=True)
                if spec.capability_profile
                else {}
            )
            client.create_subsystem(
                spec.name, spec.persona, spec.protocols, cap
            )
            actions.append(f"created subsystem '{spec.name}'")

    # 2) Pools
    for spec in topo.pools:
        try:
            client.resolve_pool(spec.name, spec.subsystem)
            actions.append(
                f"pool '{spec.name}' already exists in '{spec.subsystem}'"
            )
        except (ValidationError, APIError):
            client.create_pool(
                spec.name,
                spec.subsystem,
                spec.backend,
                spec.size_gb,
                spec.aio_path,
            )
            actions.append(
                f"created pool '{spec.name}' in '{spec.subsystem}'"
            )

    # 3) Hosts
    existing_hosts = {h["name"]: h for h in client.list_hosts()}
    for spec in topo.hosts:
        if spec.name in existing_hosts:
            actions.append(f"host '{spec.name}' already exists")
        else:
            iscsi_list = spec.initiators.get("iscsi", spec.iqns)
            nvme_list = spec.initiators.get("nvme", spec.nqns)
            iqn = iscsi_list[0] if iscsi_list else None
            nqn = nvme_list[0] if nvme_list else None
            client.create_host(spec.name, iqn=iqn, nqn=nqn)
            actions.append(f"created host '{spec.name}'")

    # 4) Volumes
    for spec in topo.volumes:
        subsystem = _subsystem_for_volume(topo, spec.pool)
        try:
            client.resolve_volume(spec.name, subsystem)
            actions.append(
                f"volume '{spec.name}' already exists in '{subsystem}'"
            )
        except (ValidationError, APIError):
            pool = client.resolve_pool(spec.pool, subsystem)
            client.create_volume(spec.name, pool["id"], spec.size_gb)
            actions.append(
                f"created volume '{spec.name}' in pool '{spec.pool}'"
            )

    # 5) Mappings
    for spec in topo.mappings:
        subsystem = _subsystem_for_mapping(topo, spec.volume)
        try:
            client.resolve_mapping(spec.host, spec.volume, subsystem)
            actions.append(
                f"mapping {spec.host}\u2192{spec.volume} already exists"
            )
        except (ValidationError, APIError):
            host = client.resolve_host(spec.host)
            volume = client.resolve_volume(spec.volume, subsystem)
            client.create_mapping(volume["id"], host["id"], spec.protocol)
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
    client: "ApolloClient",
    topo: "TopologyFile",
    actions: list[str],
) -> None:
    for spec in topo.subsystems:
        topo_pool_names = {
            p.name for p in topo.pools if p.subsystem == spec.name
        }
        for lp in client.list_pools(subsystem=spec.name):
            if lp["name"] not in topo_pool_names:
                actions.append(
                    f"STRICT: pool '{lp['name']}' in '{spec.name}' "
                    "not in topology file"
                )

        topo_vol_names: set[str] = set()
        for v in topo.volumes:
            pool_sub = next(
                (p.subsystem for p in topo.pools if p.name == v.pool), None
            )
            if pool_sub == spec.name:
                topo_vol_names.add(v.name)
        for lv in client.list_volumes(subsystem=spec.name):
            if lv["name"] not in topo_vol_names:
                actions.append(
                    f"STRICT: volume '{lv['name']}' in '{spec.name}' "
                    "not in topology file"
                )


# ------------------------------------------------------------------
# Smoke test
# ------------------------------------------------------------------

def smoke_test(
    client: "ApolloClient",
    topo: "TopologyFile",
    *,
    verbose: bool = False,
) -> list[str]:
    """Run existence + connection-info checks for every declared resource.

    Returns a list of check result strings (prefixed with ``✓`` or ``✗``).
    """
    results: list[str] = []

    for spec in topo.subsystems:
        try:
            client.get_subsystem(spec.name)
            results.append(f"\u2713 subsystem '{spec.name}' exists")
        except APIError:
            results.append(f"\u2717 subsystem '{spec.name}' NOT FOUND")

    for spec in topo.pools:
        try:
            client.resolve_pool(spec.name, spec.subsystem)
            results.append(
                f"\u2713 pool '{spec.name}' in '{spec.subsystem}' exists"
            )
        except (ValidationError, APIError):
            results.append(
                f"\u2717 pool '{spec.name}' in '{spec.subsystem}' NOT FOUND"
            )

    for spec in topo.volumes:
        subsystem = _subsystem_for_volume(topo, spec.pool)
        try:
            client.resolve_volume(spec.name, subsystem)
            results.append(
                f"\u2713 volume '{spec.name}' in '{subsystem}' exists"
            )
        except (ValidationError, APIError):
            results.append(
                f"\u2717 volume '{spec.name}' in '{subsystem}' NOT FOUND"
            )

    for spec in topo.mappings:
        subsystem = _subsystem_for_mapping(topo, spec.volume)
        try:
            mapping = client.resolve_mapping(
                spec.host, spec.volume, subsystem
            )
            results.append(
                f"\u2713 mapping {spec.host}\u2192{spec.volume} exists"
            )
            try:
                client.get_connection_info(mapping["id"])
                results.append(
                    f"\u2713 connection-info for "
                    f"{spec.host}\u2192{spec.volume} OK"
                )
            except APIError:
                results.append(
                    f"\u2717 connection-info for "
                    f"{spec.host}\u2192{spec.volume} FAILED"
                )
        except (ValidationError, APIError):
            results.append(
                f"\u2717 mapping {spec.host}\u2192{spec.volume} NOT FOUND"
            )

    return results


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _subsystem_for_volume(topo: "TopologyFile", pool_name: str) -> str:
    """Derive subsystem name from a pool name in the topology."""
    for p in topo.pools:
        if p.name == pool_name:
            return p.subsystem
    return "default"


def _subsystem_for_mapping(topo: "TopologyFile", volume_name: str) -> str:
    """Derive subsystem name from a volume's pool chain."""
    pool_name = next(
        (v.pool for v in topo.volumes if v.name == volume_name), None
    )
    if pool_name:
        return _subsystem_for_volume(topo, pool_name)
    return "default"
