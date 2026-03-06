# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Apollo Gateway CLI — Typer application.

Entrypoint: ``apollo`` console script (see pyproject.toml).
"""

from __future__ import annotations

import functools
import sys
import traceback as _tb
from typing import Optional

import typer

from apollo_gateway.cli.client import ApolloClient
from apollo_gateway.cli.errors import APIError, CLIError, ValidationError
from apollo_gateway.cli.output import OutputFormat, render

# =====================================================================
# Typer app hierarchy
# =====================================================================

app = typer.Typer(
    name="apollo",
    help="Apollo Gateway CLI — virtual storage device controller by Lunacy Systems",
    no_args_is_help=True,
)

array_app = typer.Typer(help="Manage storage arrays", no_args_is_help=True)
endpoint_app = typer.Typer(help="Manage transport endpoints on arrays", no_args_is_help=True)
pool_app = typer.Typer(help="Manage storage pools", no_args_is_help=True)
volume_app = typer.Typer(help="Manage volumes", no_args_is_help=True)
host_app = typer.Typer(help="Manage hosts (initiator endpoints)", no_args_is_help=True)
map_app = typer.Typer(help="Manage volume-to-host mappings", no_args_is_help=True)
svc_app = typer.Typer(help="IBM SVC façade commands", no_args_is_help=True)

app.add_typer(array_app, name="array")
app.add_typer(endpoint_app, name="endpoint")
app.add_typer(pool_app, name="pool")
app.add_typer(volume_app, name="volume")
app.add_typer(host_app, name="host")
app.add_typer(map_app, name="map")
app.add_typer(svc_app, name="svc")


# =====================================================================
# Global state  (populated by the top-level callback)
# =====================================================================

class _State:
    url: str = "http://localhost:8080"
    output: OutputFormat = OutputFormat.table
    verbose: bool = False
    quiet: bool = False
    timeout: int = 30


_state = _State()


def _client() -> ApolloClient:
    """Build a new :class:`ApolloClient` from current global state."""
    return ApolloClient(_state.url, timeout=_state.timeout)


# =====================================================================
# Error-handling decorator
# =====================================================================

def _handle(func):  # noqa: C901
    """Wrap a Typer command with uniform CLI-error → exit-code mapping."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValidationError as exc:
            if not _state.quiet:
                typer.echo(f"Validation error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except APIError as exc:
            if not _state.quiet:
                typer.echo(f"API error: {exc}", err=True)
            raise typer.Exit(2) from exc
        except CLIError as exc:
            if not _state.quiet:
                typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(exc.exit_code) from exc
        except typer.Exit:
            raise
        except Exception as exc:
            if not _state.quiet:
                typer.echo(f"Unexpected error: {exc}", err=True)
            if _state.verbose:
                _tb.print_exc()
            raise typer.Exit(3) from exc

    return wrapper


# =====================================================================
# Top-level callback (global options)
# =====================================================================

@app.callback()
def main_callback(
    url: str = typer.Option(
        "",
        "--url",
        envvar="APOLLO_URL",
        help="Apollo Gateway API base URL",
    ),
    output: OutputFormat = typer.Option(
        OutputFormat.table,
        "--output",
        "-o",
        help="Output format: table, json, yaml",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress informational output"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose / debug output"),
    timeout: int = typer.Option(30, "--timeout", help="HTTP request timeout in seconds"),
):
    """Apollo Gateway CLI — manage arrays, pools, volumes, hosts, and mappings."""
    _state.url = url or "http://localhost:8080"
    _state.output = output
    _state.quiet = quiet
    _state.verbose = verbose
    _state.timeout = timeout


# =====================================================================
# status
# =====================================================================

@app.command()
@_handle
def status(
    array: Optional[str] = typer.Option(
        None, "--array", "-a", help="Filter by array name"
    ),
):
    """Show API reachability and array summary."""
    c = _client()

    # Probe API health
    try:
        c.healthz()
    except Exception:
        typer.echo("API unreachable", err=True)
        raise typer.Exit(2)

    arrays = c.list_arrays()
    if array:
        arrays = [a for a in arrays if a["name"] == array]
        if not arrays:
            typer.echo(f"Array '{array}' not found", err=True)
            raise typer.Exit(1)

    summary: list[dict] = []
    for a in arrays:
        pools = c.list_pools(array=a["name"])
        volumes = c.list_volumes(array=a["name"])
        mappings = c.list_mappings(array=a["name"])
        summary.append(
            {
                "name": a["name"],
                "vendor": a.get("vendor", ""),
                "pools": len(pools),
                "volumes": len(volumes),
                "mappings": len(mappings),
            }
        )

    if not _state.quiet:
        typer.echo("API: reachable\n")
    render(
        summary,
        _state.output,
        columns=["name", "vendor", "pools", "volumes", "mappings"],
    )


# =====================================================================
# array
# =====================================================================

@array_app.command("ls")
@_handle
def array_ls():
    """List all arrays."""
    c = _client()
    arrays = c.list_arrays()
    rows = [
        {
            "name": a["name"],
            "vendor": a.get("vendor", ""),
        }
        for a in arrays
    ]
    render(rows, _state.output, columns=["name", "vendor"])


@array_app.command("show")
@_handle
def array_show(name: str = typer.Argument(..., help="Array name or ID")):
    """Show full details for an array."""
    c = _client()
    arr = c.get_array(name)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(arr, fmt)


@array_app.command("create")
@_handle
def array_create(
    name: str = typer.Argument(..., help="Array name"),
    vendor: str = typer.Option("generic", "--vendor", help="Vendor / persona type"),
):
    """Create a new array."""
    c = _client()
    result = c.create_array(name, vendor)
    if not _state.quiet:
        typer.echo(f"Created array '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@array_app.command("rm")
@_handle
def array_rm(
    name: str = typer.Argument(..., help="Array name or ID"),
    force: bool = typer.Option(False, "--force", help="Force deletion"),
):
    """Delete an array."""
    c = _client()
    c.delete_array(name, force=force)
    if not _state.quiet:
        typer.echo(f"Deleted array '{name}'")


@array_app.command("capabilities")
@_handle
def array_capabilities(
    name: str = typer.Argument(..., help="Array name or ID"),
):
    """Show the effective capability profile for an array."""
    c = _client()
    caps = c.get_capabilities(name)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(caps, fmt)


@array_app.command("set-capabilities")
@_handle
def array_set_capabilities(
    name: str = typer.Argument(..., help="Array name or ID"),
    file: str = typer.Option(
        ..., "-f", "--file", help="Capability profile file (YAML or TOML)"
    ),
    merge: bool = typer.Option(
        False, "--merge", help="Deep-merge into existing profile instead of replacing"
    ),
):
    """Update the capability profile for an array."""
    from apollo_gateway.cli.topo.load import load_capability_file

    data = load_capability_file(file)
    c = _client()

    if merge:
        current = c.get_capabilities(name)
        existing = current.get("effective_profile", {})
        data = _deep_merge(existing, data)

    result = c.update_array(name, profile=data)
    if not _state.quiet:
        typer.echo(f"Updated capabilities for '{name}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# =====================================================================
# endpoint
# =====================================================================

@endpoint_app.command("ls")
@_handle
def endpoint_ls(
    array: str = typer.Option(..., "--array", "-a", help="Array name or ID"),
):
    """List transport endpoints on an array."""
    c = _client()
    eps = c.list_endpoints(array)
    rows = [
        {
            "id": e["id"],
            "protocol": e.get("protocol", ""),
        }
        for e in eps
    ]
    render(rows, _state.output, columns=["id", "protocol"])


@endpoint_app.command("create")
@_handle
def endpoint_create(
    array: str = typer.Option(..., "--array", "-a", help="Array name or ID"),
    protocol: str = typer.Option(..., "--protocol", "-p", help="Protocol: iscsi, nvmeof_tcp, fc"),
):
    """Create a transport endpoint on an array."""
    c = _client()
    result = c.create_endpoint(array, protocol)
    if not _state.quiet:
        typer.echo(f"Created {protocol} endpoint '{result['id']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@endpoint_app.command("rm")
@_handle
def endpoint_rm(
    array: str = typer.Option(..., "--array", "-a", help="Array name or ID"),
    endpoint_id: str = typer.Argument(..., help="Endpoint ID"),
):
    """Delete a transport endpoint."""
    c = _client()
    c.delete_endpoint(array, endpoint_id)
    if not _state.quiet:
        typer.echo(f"Deleted endpoint '{endpoint_id}'")


# =====================================================================
# pool
# =====================================================================

@pool_app.command("ls")
@_handle
def pool_ls(
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """List pools in an array."""
    c = _client()
    pools = c.list_pools(array=array)
    rows = [
        {
            "name": p["name"],
            "backend": p.get("backend_type", ""),
            "size_gb": round(p["size_mb"] / 1024, 1) if p.get("size_mb") else "",
        }
        for p in pools
    ]
    render(rows, _state.output, columns=["name", "backend", "size_gb"])


@pool_app.command("show")
@_handle
def pool_show(
    pool: str = typer.Argument(..., help="Pool name"),
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """Show pool details."""
    c = _client()
    p = c.resolve_pool(pool, array)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(p, fmt)


@pool_app.command("create")
@_handle
def pool_create(
    pool: str = typer.Argument(..., help="Pool name"),
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
    backend: str = typer.Option(..., "--backend", "-b", help="Backend type: malloc or aio"),
    size_gb: float = typer.Option(..., "--size-gb", help="Pool size in GB"),
    aio_path: Optional[str] = typer.Option(None, "--aio-path", help="File path for AIO backend"),
):
    """Create a new storage pool."""
    c = _client()
    result = c.create_pool(pool, array, backend, size_gb, aio_path)
    if not _state.quiet:
        typer.echo(f"Created pool '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@pool_app.command("rm")
@_handle
def pool_rm(
    pool: str = typer.Argument(..., help="Pool name"),
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """Delete a pool."""
    c = _client()
    p = c.resolve_pool(pool, array)
    c.delete_pool(p["id"])
    if not _state.quiet:
        typer.echo(f"Deleted pool '{pool}'")


# =====================================================================
# volume
# =====================================================================

@volume_app.command("ls")
@_handle
def volume_ls(
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """List volumes in an array."""
    c = _client()
    volumes = c.list_volumes(array=array)
    rows = [
        {
            "name": v["name"],
            "size_gb": v.get("size_gb", ""),
            "status": v.get("status", ""),
            "pool": v.get("pool_id", ""),
        }
        for v in volumes
    ]
    render(rows, _state.output, columns=["name", "size_gb", "status", "pool"])


@volume_app.command("show")
@_handle
def volume_show(
    name: str = typer.Argument(..., help="Volume name"),
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """Show volume details."""
    c = _client()
    v = c.resolve_volume(name, array)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(v, fmt)


@volume_app.command("create")
@_handle
def volume_create(
    name: str = typer.Argument(..., help="Volume name"),
    pool: str = typer.Option(..., "--pool", "-p", help="Pool name"),
    size_gb: float = typer.Option(..., "--size-gb", help="Volume size in GB"),
    array: Optional[str] = typer.Option(
        None,
        "--array",
        "-a",
        help="Array name (inferred from pool when omitted)",
    ),
):
    """Create a new volume."""
    c = _client()

    if array:
        p = c.resolve_pool(pool, array)
    else:
        # Scan all pools to find the named one
        all_pools = c.list_pools()
        p = next((x for x in all_pools if x["name"] == pool), None)
        if p is None:
            raise ValidationError(f"Pool '{pool}' not found in any array")

    result = c.create_volume(name, p["id"], size_gb)
    if not _state.quiet:
        typer.echo(f"Created volume '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@volume_app.command("rm")
@_handle
def volume_rm(
    name: str = typer.Argument(..., help="Volume name"),
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """Delete a volume."""
    c = _client()
    v = c.resolve_volume(name, array)
    c.delete_volume(v["id"])
    if not _state.quiet:
        typer.echo(f"Deleted volume '{name}'")


@volume_app.command("extend")
@_handle
def volume_extend(
    name: str = typer.Argument(..., help="Volume name"),
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
    size_gb: float = typer.Option(..., "--size-gb", help="New total size in GB"),
):
    """Extend (grow) a volume."""
    c = _client()
    v = c.resolve_volume(name, array)
    result = c.extend_volume(v["id"], size_gb)
    if not _state.quiet:
        typer.echo(f"Extended volume '{name}' to {size_gb} GB")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


# =====================================================================
# host
# =====================================================================

@host_app.command("ls")
@_handle
def host_ls():
    """List all hosts."""
    c = _client()
    hosts = c.list_hosts()
    rows = [
        {
            "name": h["name"],
            "iqns": ",".join(h.get("iqns") or []),
            "nqns": ",".join(h.get("nqns") or []),
            "wwpns": ",".join(h.get("wwpns") or []),
        }
        for h in hosts
    ]
    render(rows, _state.output, columns=["name", "iqns", "nqns", "wwpns"])


@host_app.command("show")
@_handle
def host_show(name: str = typer.Argument(..., help="Host name")):
    """Show host details."""
    c = _client()
    h = c.resolve_host(name)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(h, fmt)


@host_app.command("create")
@_handle
def host_create(name: str = typer.Argument(..., help="Host name")):
    """Create a new host."""
    c = _client()
    result = c.create_host(name)
    if not _state.quiet:
        typer.echo(f"Created host '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@host_app.command("rm")
@_handle
def host_rm(name: str = typer.Argument(..., help="Host name")):
    """Delete a host."""
    c = _client()
    h = c.resolve_host(name)
    c.delete_host(h["id"])
    if not _state.quiet:
        typer.echo(f"Deleted host '{name}'")


@host_app.command("add-initiator")
@_handle
def host_add_initiator(
    name: str = typer.Argument(..., help="Host name"),
    iscsi_iqn: Optional[str] = typer.Option(None, "--iscsi-iqn", help="iSCSI initiator IQN to add"),
    nvme_nqn: Optional[str] = typer.Option(None, "--nvme-nqn", help="NVMe-oF host NQN to add"),
    fc_wwpn: Optional[str] = typer.Option(None, "--fc-wwpn", help="FC WWPN to add"),
):
    """Add an initiator to a host."""
    if not iscsi_iqn and not nvme_nqn and not fc_wwpn:
        raise ValidationError("Provide at least one of --iscsi-iqn, --nvme-nqn, or --fc-wwpn")
    c = _client()
    h = c.resolve_host(name)
    iqns: list = list(h.get("iqns") or [])
    nqns: list = list(h.get("nqns") or [])
    wwpns: list = list(h.get("wwpns") or [])
    if iscsi_iqn and iscsi_iqn not in iqns:
        iqns.append(iscsi_iqn)
    if nvme_nqn and nvme_nqn not in nqns:
        nqns.append(nvme_nqn)
    if fc_wwpn and fc_wwpn not in wwpns:
        wwpns.append(fc_wwpn)
    c.update_host(h["id"], iqns=iqns, nqns=nqns, wwpns=wwpns)
    if not _state.quiet:
        typer.echo(f"Updated host '{name}'")


@host_app.command("rm-initiator")
@_handle
def host_rm_initiator(
    name: str = typer.Argument(..., help="Host name"),
    iscsi_iqn: Optional[str] = typer.Option(None, "--iscsi-iqn", help="iSCSI IQN to remove"),
    nvme_nqn: Optional[str] = typer.Option(None, "--nvme-nqn", help="NVMe NQN to remove"),
    fc_wwpn: Optional[str] = typer.Option(None, "--fc-wwpn", help="FC WWPN to remove"),
):
    """Remove an initiator from a host."""
    if not iscsi_iqn and not nvme_nqn and not fc_wwpn:
        raise ValidationError("Provide at least one of --iscsi-iqn, --nvme-nqn, or --fc-wwpn")
    c = _client()
    h = c.resolve_host(name)
    iqns: list = list(h.get("iqns") or [])
    nqns: list = list(h.get("nqns") or [])
    wwpns: list = list(h.get("wwpns") or [])
    if iscsi_iqn and iscsi_iqn in iqns:
        iqns.remove(iscsi_iqn)
    if nvme_nqn and nvme_nqn in nqns:
        nqns.remove(nvme_nqn)
    if fc_wwpn and fc_wwpn in wwpns:
        wwpns.remove(fc_wwpn)
    c.update_host(h["id"], iqns=iqns, nqns=nqns, wwpns=wwpns)
    if not _state.quiet:
        typer.echo(f"Removed initiator(s) from host '{name}'")


@host_app.command("attachments")
@_handle
def host_attachments(
    name: str = typer.Argument(..., help="Host name"),
):
    """Show storage attachments for a host."""
    c = _client()
    h = c.resolve_host(name)
    data = c.get_host_attachments(h["id"])
    attachments = data.get("attachments", [])
    if not attachments:
        if not _state.quiet:
            typer.echo("No attachments")
        return
    rows = [
        {
            "volume": a.get("volume_name", ""),
            "persona_protocol": a.get("persona", {}).get("protocol", ""),
            "persona_targets": ",".join(a.get("persona", {}).get("targets", [])),
            "underlay_protocol": a.get("underlay", {}).get("protocol", ""),
            "lun": a.get("lun_id") or "",
        }
        for a in attachments
    ]
    render(
        rows,
        _state.output,
        columns=["volume", "persona_protocol", "persona_targets", "underlay_protocol", "lun"],
    )


# =====================================================================
# map
# =====================================================================

@map_app.command("ls")
@_handle
def map_ls(
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
):
    """List mappings in an array."""
    c = _client()
    mappings = c.list_mappings(array=array)
    rows = [
        {
            "host": m.get("host_id", ""),
            "volume": m.get("volume_id", ""),
            "persona_ep": m.get("persona_endpoint_id", ""),
            "underlay_ep": m.get("underlay_endpoint_id", ""),
            "lun": m.get("lun_id") or "",
            "desired_state": m.get("desired_state", ""),
        }
        for m in mappings
    ]
    render(rows, _state.output, columns=["host", "volume", "persona_ep", "underlay_ep", "lun", "desired_state"])


@map_app.command("create")
@_handle
def map_create(
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
    host: str = typer.Option(..., "--host", help="Host name"),
    volume: str = typer.Option(..., "--volume", help="Volume name"),
    persona_endpoint: str = typer.Option(
        ..., "--persona-endpoint", help="Persona endpoint name or ID"
    ),
    underlay_endpoint: str = typer.Option(
        ..., "--underlay-endpoint", help="Underlay endpoint name or ID"
    ),
):
    """Create a volume-to-host mapping."""
    c = _client()
    h = c.resolve_host(host)
    v = c.resolve_volume(volume, array)
    result = c.create_mapping(
        volume_id=v["id"],
        host_id=h["id"],
        persona_endpoint_id=persona_endpoint,
        underlay_endpoint_id=underlay_endpoint,
    )
    if not _state.quiet:
        typer.echo(f"Created mapping {host}\u2192{volume}")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@map_app.command("rm")
@_handle
def map_rm(
    array: str = typer.Option(..., "--array", "-a", help="Array name"),
    host: str = typer.Option(..., "--host", help="Host name"),
    volume: str = typer.Option(..., "--volume", help="Volume name"),
):
    """Delete a mapping."""
    c = _client()
    m = c.resolve_mapping(host, volume, array)
    c.delete_mapping(m["id"])
    if not _state.quiet:
        typer.echo(f"Deleted mapping {host}\u2192{volume}")


# =====================================================================
# validate / apply / smoke
# =====================================================================

@app.command("validate")
@_handle
def validate_cmd(
    file: str = typer.Option(..., "-f", "--file", help="Topology file (YAML or TOML)"),
):
    """Validate a topology file (schema + cross-references)."""
    from apollo_gateway.cli.topo.load import load_topology
    from apollo_gateway.cli.topo.validate import validate_topology

    topo = load_topology(file)
    errors = validate_topology(topo)
    if errors:
        for e in errors:
            typer.echo(f"  \u2717 {e}", err=True)
        raise typer.Exit(1)
    if not _state.quiet:
        typer.echo("\u2713 Topology is valid")


@app.command("apply")
@_handle
def apply_cmd(
    file: str = typer.Option(..., "-f", "--file", help="Topology file (YAML or TOML)"),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Report live resources not present in the topology file",
    ),
):
    """Apply a topology file idempotently (create missing resources)."""
    from apollo_gateway.cli.topo.apply import apply_topology
    from apollo_gateway.cli.topo.load import load_topology
    from apollo_gateway.cli.topo.validate import validate_topology

    topo = load_topology(file)
    errors = validate_topology(topo)
    if errors:
        for e in errors:
            typer.echo(f"  \u2717 {e}", err=True)
        raise typer.Exit(1)

    c = _client()
    actions = apply_topology(c, topo, strict=strict, verbose=_state.verbose)
    for a in actions:
        typer.echo(f"  {a}")
    if not _state.quiet:
        typer.echo(f"\u2713 Apply complete ({len(actions)} actions)")


@app.command("smoke")
@_handle
def smoke_cmd(
    file: str = typer.Option(..., "-f", "--file", help="Topology file (YAML or TOML)"),
):
    """Run smoke tests: validate then check every resource exists."""
    from apollo_gateway.cli.topo.apply import smoke_test
    from apollo_gateway.cli.topo.load import load_topology
    from apollo_gateway.cli.topo.validate import validate_topology

    topo = load_topology(file)
    errors = validate_topology(topo)
    if errors:
        for e in errors:
            typer.echo(f"  \u2717 {e}", err=True)
        raise typer.Exit(1)

    c = _client()
    results = smoke_test(c, topo, verbose=_state.verbose)
    failed = any("\u2717" in r for r in results)
    for r in results:
        typer.echo(f"  {r}")
    if failed:
        raise typer.Exit(2)
    if not _state.quiet:
        typer.echo(f"\u2713 All smoke tests passed ({len(results)} checks)")


# =====================================================================
# Console-script entrypoint
# =====================================================================

def cli() -> None:
    """``apollo`` console-script entrypoint."""
    app()


if __name__ == "__main__":
    cli()
