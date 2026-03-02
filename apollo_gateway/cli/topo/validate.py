# FILE: apollo_gateway/cli/topo/validate.py
"""Cross-reference and constraint validation for topology files."""

from __future__ import annotations

from apollo_gateway.cli.topo.models import TopologyFile


_VALID_PROTOCOLS = {"iscsi", "nvmeof_tcp"}


def validate_topology(topo: TopologyFile) -> list[str]:
    """Validate a parsed topology and return a list of error strings.

    An empty list means the topology is valid.
    """
    errors: list[str] = []

    subsystem_names: set[str] = set()
    subsystem_map: dict[str, object] = {}
    pool_names: set[str] = set()
    pool_subsystem: dict[str, str] = {}
    host_names: set[str] = set()
    volume_names: set[str] = set()
    volume_pool: dict[str, str] = {}

    # ---- Subsystems (uniqueness + valid protocols) -------------------
    for s in topo.subsystems:
        if s.name in subsystem_names:
            errors.append(f"Duplicate subsystem name: '{s.name}'")
        subsystem_names.add(s.name)
        subsystem_map[s.name] = s
        for p in s.protocols:
            if p not in _VALID_PROTOCOLS:
                errors.append(
                    f"Subsystem '{s.name}': unknown protocol '{p}'"
                )

    # ---- Pools (reference subsystem, unique within subsystem) --------
    pools_per_subsystem: dict[str, set[str]] = {}
    for p in topo.pools:
        if p.subsystem not in subsystem_names:
            errors.append(
                f"Pool '{p.name}' references unknown subsystem '{p.subsystem}'"
            )
        sub_pools = pools_per_subsystem.setdefault(p.subsystem, set())
        if p.name in sub_pools:
            errors.append(
                f"Duplicate pool name '{p.name}' in subsystem '{p.subsystem}'"
            )
        sub_pools.add(p.name)
        pool_names.add(p.name)
        pool_subsystem[p.name] = p.subsystem

    # ---- Hosts (uniqueness) ------------------------------------------
    for h in topo.hosts:
        if h.name in host_names:
            errors.append(f"Duplicate host name: '{h.name}'")
        host_names.add(h.name)

    # ---- Volumes (reference pool, unique within subsystem) -----------
    vols_per_subsystem: dict[str, set[str]] = {}
    for v in topo.volumes:
        if v.pool not in pool_names:
            errors.append(
                f"Volume '{v.name}' references unknown pool '{v.pool}'"
            )
        else:
            sub = pool_subsystem[v.pool]
            sub_vols = vols_per_subsystem.setdefault(sub, set())
            if v.name in sub_vols:
                errors.append(
                    f"Duplicate volume name '{v.name}' in subsystem '{sub}'"
                )
            sub_vols.add(v.name)
        volume_names.add(v.name)
        volume_pool[v.name] = v.pool

    # ---- Mappings (references + protocol enablement) -----------------
    for m in topo.mappings:
        if m.host not in host_names:
            errors.append(f"Mapping references unknown host '{m.host}'")
        if m.volume not in volume_names:
            errors.append(f"Mapping references unknown volume '{m.volume}'")
        else:
            pname = volume_pool.get(m.volume)
            sub_name = pool_subsystem.get(pname, "") if pname else ""
            sub_spec = subsystem_map.get(sub_name)
            if sub_spec and m.protocol not in sub_spec.protocols:
                errors.append(
                    f"Mapping protocol '{m.protocol}' is not enabled for "
                    f"subsystem '{sub_name}' (volume '{m.volume}')"
                )

    # ---- Capability constraints --------------------------------------
    for s in topo.subsystems:
        if not s.capability_profile:
            continue
        feats = s.capability_profile.features

        # thin provisioning constraint
        if feats.get("thin_provisioning") is False:
            for v in topo.volumes:
                pname = v.pool
                vol_sub = pool_subsystem.get(pname)
                if vol_sub == s.name and v.thin is True:
                    errors.append(
                        f"Volume '{v.name}': thin provisioning requested but "
                        f"disabled in subsystem '{s.name}' capability profile"
                    )

        # snapshots constraint (future-proof)
        if feats.get("snapshots") is False:
            # No snapshot spec in topology yet; placeholder for when
            # snapshot references are added to topology files.
            pass

    return errors
