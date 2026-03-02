# FILE: apollo_gateway/cli/main.py
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

subsystem_app = typer.Typer(help="Manage virtual storage subsystems", no_args_is_help=True)
pool_app = typer.Typer(help="Manage storage pools", no_args_is_help=True)
volume_app = typer.Typer(help="Manage volumes", no_args_is_help=True)
host_app = typer.Typer(help="Manage hosts (initiator endpoints)", no_args_is_help=True)
map_app = typer.Typer(help="Manage volume-to-host mappings", no_args_is_help=True)
svc_app = typer.Typer(help="IBM SVC façade commands", no_args_is_help=True)

app.add_typer(subsystem_app, name="subsystem")
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
    """Apollo Gateway CLI — manage subsystems, pools, volumes, hosts, and mappings."""
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
    subsystem: Optional[str] = typer.Option(
        None, "--subsystem", "-s", help="Filter by subsystem name"
    ),
):
    """Show API reachability and subsystem summary."""
    c = _client()

    # Probe API health
    try:
        c.healthz()
    except Exception:
        typer.echo("API unreachable", err=True)
        raise typer.Exit(2)

    subs = c.list_subsystems()
    if subsystem:
        subs = [s for s in subs if s["name"] == subsystem]
        if not subs:
            typer.echo(f"Subsystem '{subsystem}' not found", err=True)
            raise typer.Exit(1)

    summary: list[dict] = []
    for s in subs:
        pools = c.list_pools(subsystem=s["name"])
        volumes = c.list_volumes(subsystem=s["name"])
        mappings = c.list_mappings(subsystem=s["name"])
        summary.append(
            {
                "name": s["name"],
                "persona": s["persona"],
                "protocols": ", ".join(s.get("protocols_enabled", [])),
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
        columns=["name", "persona", "protocols", "pools", "volumes", "mappings"],
    )


# =====================================================================
# subsystem
# =====================================================================

@subsystem_app.command("ls")
@_handle
def subsystem_ls():
    """List all subsystems."""
    c = _client()
    subs = c.list_subsystems()
    rows = [
        {
            "name": s["name"],
            "persona": s["persona"],
            "protocols": ", ".join(s.get("protocols_enabled", [])),
        }
        for s in subs
    ]
    render(rows, _state.output, columns=["name", "persona", "protocols"])


@subsystem_app.command("show")
@_handle
def subsystem_show(name: str = typer.Argument(..., help="Subsystem name or ID")):
    """Show full details for a subsystem."""
    c = _client()
    sub = c.get_subsystem(name)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(sub, fmt)


@subsystem_app.command("create")
@_handle
def subsystem_create(
    name: str = typer.Argument(..., help="Subsystem name"),
    persona: str = typer.Option("generic", "--persona", help="Persona type"),
    protocol: Optional[list[str]] = typer.Option(
        None, "--protocol", help="Enabled protocol (repeatable)"
    ),
):
    """Create a new subsystem."""
    c = _client()
    protocols = protocol if protocol else ["iscsi", "nvmeof_tcp"]
    result = c.create_subsystem(name, persona, protocols)
    if not _state.quiet:
        typer.echo(f"Created subsystem '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@subsystem_app.command("rm")
@_handle
def subsystem_rm(
    name: str = typer.Argument(..., help="Subsystem name or ID"),
    force: bool = typer.Option(False, "--force", help="Force deletion"),
):
    """Delete a subsystem."""
    c = _client()
    c.delete_subsystem(name, force=force)
    if not _state.quiet:
        typer.echo(f"Deleted subsystem '{name}'")


@subsystem_app.command("capabilities")
@_handle
def subsystem_capabilities(
    name: str = typer.Argument(..., help="Subsystem name or ID"),
):
    """Show the effective capability profile for a subsystem."""
    c = _client()
    caps = c.get_capabilities(name)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(caps, fmt)


@subsystem_app.command("set-capabilities")
@_handle
def subsystem_set_capabilities(
    name: str = typer.Argument(..., help="Subsystem name or ID"),
    file: str = typer.Option(
        ..., "-f", "--file", help="Capability profile file (YAML or TOML)"
    ),
    merge: bool = typer.Option(
        False, "--merge", help="Deep-merge into existing profile instead of replacing"
    ),
):
    """Update the capability profile for a subsystem."""
    from apollo_gateway.cli.topo.load import load_capability_file

    data = load_capability_file(file)
    c = _client()

    if merge:
        current = c.get_capabilities(name)
        existing = current.get("effective_profile", {})
        data = _deep_merge(existing, data)

    result = c.update_subsystem(name, capability_profile=data)
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
# pool
# =====================================================================

@pool_app.command("ls")
@_handle
def pool_ls(
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """List pools in a subsystem."""
    c = _client()
    pools = c.list_pools(subsystem=subsystem)
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
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """Show pool details."""
    c = _client()
    p = c.resolve_pool(pool, subsystem)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(p, fmt)


@pool_app.command("create")
@_handle
def pool_create(
    pool: str = typer.Argument(..., help="Pool name"),
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
    backend: str = typer.Option(..., "--backend", "-b", help="Backend type: malloc or aio"),
    size_gb: float = typer.Option(..., "--size-gb", help="Pool size in GB"),
    aio_path: Optional[str] = typer.Option(None, "--aio-path", help="File path for AIO backend"),
):
    """Create a new storage pool."""
    c = _client()
    result = c.create_pool(pool, subsystem, backend, size_gb, aio_path)
    if not _state.quiet:
        typer.echo(f"Created pool '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@pool_app.command("rm")
@_handle
def pool_rm(
    pool: str = typer.Argument(..., help="Pool name"),
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """Delete a pool."""
    c = _client()
    p = c.resolve_pool(pool, subsystem)
    c.delete_pool(p["id"])
    if not _state.quiet:
        typer.echo(f"Deleted pool '{pool}'")


# =====================================================================
# volume
# =====================================================================

@volume_app.command("ls")
@_handle
def volume_ls(
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """List volumes in a subsystem."""
    c = _client()
    volumes = c.list_volumes(subsystem=subsystem)
    rows = [
        {
            "name": v["name"],
            "size_gb": round(v["size_mb"] / 1024, 1),
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
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """Show volume details."""
    c = _client()
    v = c.resolve_volume(name, subsystem)
    fmt = _state.output if _state.output != OutputFormat.table else OutputFormat.yaml
    render(v, fmt)


@volume_app.command("create")
@_handle
def volume_create(
    name: str = typer.Argument(..., help="Volume name"),
    pool: str = typer.Option(..., "--pool", "-p", help="Pool name"),
    size_gb: float = typer.Option(..., "--size-gb", help="Volume size in GB"),
    thin: Optional[bool] = typer.Option(None, "--thin", help="Enable thin provisioning"),
    subsystem: Optional[str] = typer.Option(
        None,
        "--subsystem",
        "-s",
        help="Subsystem name (inferred from pool when omitted)",
    ),
):
    """Create a new volume."""
    c = _client()

    if subsystem:
        p = c.resolve_pool(pool, subsystem)
    else:
        # Scan all pools to find the named one
        all_pools = c.list_pools()
        p = next((x for x in all_pools if x["name"] == pool), None)
        if p is None:
            raise ValidationError(f"Pool '{pool}' not found in any subsystem")

    result = c.create_volume(name, p["id"], size_gb)
    if not _state.quiet:
        typer.echo(f"Created volume '{result['name']}'")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@volume_app.command("rm")
@_handle
def volume_rm(
    name: str = typer.Argument(..., help="Volume name"),
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """Delete a volume."""
    c = _client()
    v = c.resolve_volume(name, subsystem)
    c.delete_volume(v["id"])
    if not _state.quiet:
        typer.echo(f"Deleted volume '{name}'")


@volume_app.command("extend")
@_handle
def volume_extend(
    name: str = typer.Argument(..., help="Volume name"),
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
    size_gb: float = typer.Option(..., "--size-gb", help="New total size in GB"),
):
    """Extend (grow) a volume."""
    c = _client()
    v = c.resolve_volume(name, subsystem)
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
            "iqn": h.get("iqn") or "",
            "nqn": h.get("nqn") or "",
        }
        for h in hosts
    ]
    render(rows, _state.output, columns=["name", "iqn", "nqn"])


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
    iscsi_iqn: Optional[str] = typer.Option(None, "--iscsi-iqn", help="iSCSI initiator IQN"),
    nvme_nqn: Optional[str] = typer.Option(None, "--nvme-nqn", help="NVMe-oF host NQN"),
):
    """Add an initiator to a host."""
    if not iscsi_iqn and not nvme_nqn:
        raise ValidationError("Provide at least one of --iscsi-iqn or --nvme-nqn")
    c = _client()
    h = c.resolve_host(name)
    update: dict = {}
    if iscsi_iqn:
        update["iqn"] = iscsi_iqn
    if nvme_nqn:
        update["nqn"] = nvme_nqn
    c.update_host(h["id"], **update)
    if not _state.quiet:
        typer.echo(f"Updated host '{name}'")


@host_app.command("rm-initiator")
@_handle
def host_rm_initiator(
    name: str = typer.Argument(..., help="Host name"),
    iscsi_iqn: Optional[str] = typer.Option(None, "--iscsi-iqn", help="iSCSI IQN to remove"),
    nvme_nqn: Optional[str] = typer.Option(None, "--nvme-nqn", help="NVMe NQN to remove"),
):
    """Remove an initiator from a host."""
    if not iscsi_iqn and not nvme_nqn:
        raise ValidationError("Provide at least one of --iscsi-iqn or --nvme-nqn")
    c = _client()
    h = c.resolve_host(name)
    update: dict = {}
    if iscsi_iqn:
        update["iqn"] = None
    if nvme_nqn:
        update["nqn"] = None
    c.update_host(h["id"], **update)
    if not _state.quiet:
        typer.echo(f"Removed initiator(s) from host '{name}'")


# =====================================================================
# map
# =====================================================================

@map_app.command("ls")
@_handle
def map_ls(
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
):
    """List mappings in a subsystem."""
    c = _client()
    mappings = c.list_mappings(subsystem=subsystem)
    rows = [
        {
            "host": m.get("host_id", ""),
            "volume": m.get("volume_id", ""),
            "protocol": m.get("protocol", ""),
            "lun": m.get("lun_id") or "",
            "nsid": m.get("ns_id") or "",
        }
        for m in mappings
    ]
    render(rows, _state.output, columns=["host", "volume", "protocol", "lun", "nsid"])


@map_app.command("create")
@_handle
def map_create(
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
    host: str = typer.Option(..., "--host", help="Host name"),
    volume: str = typer.Option(..., "--volume", help="Volume name"),
    protocol: str = typer.Option(
        ..., "--protocol", "-p", help="Protocol: iscsi or nvmeof_tcp"
    ),
):
    """Create a volume-to-host mapping."""
    c = _client()
    h = c.resolve_host(host)
    v = c.resolve_volume(volume, subsystem)
    result = c.create_mapping(v["id"], h["id"], protocol)
    if not _state.quiet:
        typer.echo(f"Created mapping {host}\u2192{volume} ({protocol})")
    if _state.output != OutputFormat.table:
        render(result, _state.output)


@map_app.command("rm")
@_handle
def map_rm(
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
    host: str = typer.Option(..., "--host", help="Host name"),
    volume: str = typer.Option(..., "--volume", help="Volume name"),
):
    """Delete a mapping."""
    c = _client()
    m = c.resolve_mapping(host, volume, subsystem)
    c.delete_mapping(m["id"])
    if not _state.quiet:
        typer.echo(f"Deleted mapping {host}\u2192{volume}")


# =====================================================================
# connection-info  (top-level command)
# =====================================================================

@app.command("connection-info")
@_handle
def connection_info(
    subsystem: str = typer.Option(..., "--subsystem", "-s", help="Subsystem name"),
    host: str = typer.Option(..., "--host", help="Host name"),
    volume: str = typer.Option(..., "--volume", help="Volume name"),
):
    """Get Cinder-style connection info for a mapping."""
    c = _client()
    m = c.resolve_mapping(host, volume, subsystem)
    info = c.get_connection_info(m["id"])
    render(info, _state.output)


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
# svc  (IBM SVC façade debug — local in-process execution)
# =====================================================================

@svc_app.command("run")
@_handle
def svc_run(
    subsystem: str = typer.Option(
        ..., "--subsystem", "-s", help="Subsystem name (required)"
    ),
    command: str = typer.Argument(
        ..., help='SVC command string, e.g. "svcinfo lssystem"'
    ),
):
    """Run an IBM SVC façade command via the gateway API."""
    result = _client().svc_run(subsystem, command)
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
    raise typer.Exit(result["exit_code"])


# =====================================================================
# Console-script entrypoint
# =====================================================================

def cli() -> None:
    """``apollo`` console-script entrypoint."""
    app()


if __name__ == "__main__":
    cli()
