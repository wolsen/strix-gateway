# FILE: apollo_gateway/topology/validate.py
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

    # 1. Subsystem names must be unique
    seen_subsystems: set[str] = set()
    for sub in spec.subsystems:
        if sub.name in seen_subsystems:
            errors.append(f"Duplicate subsystem name: '{sub.name}'")
        seen_subsystems.add(sub.name)

    # 2. Pool names must be unique within each subsystem; subsystem must exist
    # key: (subsystem_name, pool_name) → PoolSpec
    pool_map: dict[str, str] = {}   # pool_name → subsystem_name (for volume lookup)
    seen_pool_keys: set[tuple[str, str]] = set()
    for pool in spec.pools:
        if pool.subsystem not in seen_subsystems:
            errors.append(
                f"Pool '{pool.name}' references unknown subsystem '{pool.subsystem}'"
            )
        key = (pool.subsystem, pool.name)
        if key in seen_pool_keys:
            errors.append(
                f"Duplicate pool name '{pool.name}' in subsystem '{pool.subsystem}'"
            )
        seen_pool_keys.add(key)
        # Last writer wins for pool_name → subsystem mapping (duplicates already flagged)
        pool_map[pool.name] = pool.subsystem

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

    # 5. Mapping checks: host, volume must exist; protocol must be enabled by subsystem
    sub_protocols: dict[str, list[str]] = {s.name: s.protocols for s in spec.subsystems}
    for mapping in spec.mappings:
        if mapping.host not in seen_hosts:
            errors.append(
                f"Mapping references unknown host '{mapping.host}'"
            )
        if mapping.volume not in seen_volumes:
            errors.append(
                f"Mapping references unknown volume '{mapping.volume}'"
            )
        else:
            # Check protocol is enabled by the volume's subsystem
            vol_pool = next(
                (v.pool for v in spec.volumes if v.name == mapping.volume), None
            )
            if vol_pool is not None:
                sub_name = pool_map.get(vol_pool)
                if sub_name is not None and sub_name in sub_protocols:
                    enabled = sub_protocols[sub_name]
                    if mapping.protocol not in enabled:
                        errors.append(
                            f"Mapping for volume '{mapping.volume}' uses protocol "
                            f"'{mapping.protocol}' which is not enabled for subsystem "
                            f"'{sub_name}' (enabled: {enabled})"
                        )

    return errors
