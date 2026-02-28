# FILE: apollo_gateway/topology/__init__.py
"""Topology specification package for Apollo Gateway.

Provides YAML/TOML-driven declarative configuration of subsystems, pools,
volumes, hosts, and mappings.  Typical usage::

    from apollo_gateway.topology.load import load_yaml
    from apollo_gateway.topology.validate import validate
    from apollo_gateway.topology.apply import apply_topology

    spec = load_yaml("examples/ci/single_svc.yaml")
    errors = validate(spec)
    if errors:
        raise ValueError("\\n".join(errors))
    summary = await apply_topology(spec, session, spdk_client, settings)
"""
