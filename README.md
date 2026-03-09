# Strix Gateway

**Virtual Storage Device control-plane**

Strix Gateway provisions volumes backed by [SPDK](https://spdk.io/) and exports
them over iSCSI and NVMe-oF TCP. It is intended for OpenStack Cinder driver
functional testing, not full vendor feature parity.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  FastAPI REST API (port 8080)                   │
│   /v1/pools, /v1/volumes, /v1/hosts,            │
│   /v1/mappings, /admin/faults, /admin/delays    │
├─────────────────────────────────────────────────┤
│  Core Logic                                     │
│   models.py · db.py · reconcile.py · faults.py  │
├─────────────────────────────────────────────────┤
│  SPDK Abstraction Layer                         │
│   rpc.py · ensure.py · iscsi.py · nvmf.py      │
├────────────────────┬────────────────────────────┤
│  SPDK JSON-RPC     │  Unix socket               │
│  (spdk.sock)       │  /var/tmp/spdk.sock         │
└────────────────────┴────────────────────────────┘
         │                      │
    ┌────┴────┐           ┌────┴────┐
    │  iSCSI  │           │ NVMe-oF │
    │  :3260  │           │  :4420  │
    └─────────┘           └─────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Install dependencies

```bash
# With uv
uv sync
```

### Run tests (no SPDK required)

```bash
# Run all 133 tests
uv run pytest

# Run with coverage report
uv run pytest --cov=strix_gateway --cov-report=term-missing --cov-branch

# Run only unit tests
uv run pytest tests/unit/

# Run only integration tests
uv run pytest tests/integration/
```

### Snap install and run

```bash
# Build the snap
snapcraft

# Install locally for testing
sudo snap install --dangerous ./strix-gateway_*.snap

# Run the daemon command
strix-gateway

# Check health
curl http://localhost:8080/healthz
```

The snap bundles runtime dependencies and is the preferred deployment artifact.

### Start standalone (development)

```bash
# Set environment variables
export STRIX_SPDK_SOCKET_PATH=/var/tmp/spdk.sock
export STRIX_DATABASE_URL=sqlite+aiosqlite:///./strix_gateway.db

uvicorn strix_gateway.main:app --host 0.0.0.0 --port 8080 --reload
```

## Configuration

All settings are controlled via environment variables with the `STRIX_` prefix:

| Variable | Default | Description |
|---|---|---|
| `STRIX_DATABASE_URL` | `sqlite+aiosqlite:///./strix_gateway.db` | SQLAlchemy async DB URL |
| `STRIX_SPDK_SOCKET_PATH` | `/var/tmp/spdk.sock` | SPDK JSON-RPC Unix socket |
| `STRIX_ISCSI_PORTAL_IP` | `0.0.0.0` | iSCSI portal listen address |
| `STRIX_ISCSI_PORTAL_PORT` | `3260` | iSCSI portal port |
| `STRIX_NVMEF_PORTAL_IP` | `0.0.0.0` | NVMe-oF TCP listen address |
| `STRIX_NVMEF_PORTAL_PORT` | `4420` | NVMe-oF TCP port |
| `STRIX_IQN_PREFIX` | `iqn.2026-02.strix.strix` | iSCSI IQN prefix |
| `STRIX_NQN_PREFIX` | `nqn.2026-02.io.strix:strix` | NVMe NQN prefix |

## REST API Reference

### Pools

```bash
# Create a malloc-backed pool (in-memory)
curl -X POST http://localhost:8080/v1/pools \
  -H "Content-Type: application/json" \
  -d '{"name": "fast-pool", "backend_type": "malloc", "size_mb": 4096}'

# Create an AIO-file-backed pool (persistent)
curl -X POST http://localhost:8080/v1/pools \
  -H "Content-Type: application/json" \
  -d '{"name": "disk-pool", "backend_type": "aio_file", "aio_path": "/data/pool0.img"}'

# List pools
curl http://localhost:8080/v1/pools
```

### Volumes

```bash
# Create a volume
curl -X POST http://localhost:8080/v1/volumes \
  -H "Content-Type: application/json" \
  -d '{"name": "vol-1", "pool_id": "<POOL_ID>", "size_mb": 1024}'

# Get volume details
curl http://localhost:8080/v1/volumes/<VOLUME_ID>

# Extend a volume
curl -X POST http://localhost:8080/v1/volumes/<VOLUME_ID>/extend \
  -H "Content-Type: application/json" \
  -d '{"new_size_mb": 2048}'

# Delete a volume (must unmap first)
curl -X DELETE http://localhost:8080/v1/volumes/<VOLUME_ID>
```

### Hosts

```bash
# Register a host
curl -X POST http://localhost:8080/v1/hosts \
  -H "Content-Type: application/json" \
  -d '{
    "name": "compute-01",
    "iqn": "iqn.1993-08.org.debian:compute-01",
    "nqn": "nqn.2014-08.org.nvmexpress:uuid:compute-01"
  }'

# List hosts
curl http://localhost:8080/v1/hosts
```

### Mappings (Export)

```bash
# Create an iSCSI mapping
curl -X POST http://localhost:8080/v1/mappings \
  -H "Content-Type: application/json" \
  -d '{
    "volume_id": "<VOLUME_ID>",
    "host_id": "<HOST_ID>",
    "protocol": "iscsi"
  }'

# Create an NVMe-oF TCP mapping
curl -X POST http://localhost:8080/v1/mappings \
  -H "Content-Type: application/json" \
  -d '{
    "volume_id": "<VOLUME_ID>",
    "host_id": "<HOST_ID>",
    "protocol": "nvmeof_tcp"
  }'

# Get connection info (OpenStack Cinder format)
curl http://localhost:8080/v1/mappings/<MAPPING_ID>/connection-info

# Delete a mapping
curl -X DELETE http://localhost:8080/v1/mappings/<MAPPING_ID>
```

### Connection Info Response Shapes

**iSCSI:**
```json
{
  "driver_volume_type": "iscsi",
  "data": {
    "target_iqn": "iqn.2026-02.strix.strix:<export_id>",
    "target_portal": "0.0.0.0:3260",
    "target_lun": 0,
    "access_mode": "rw",
    "discard": true
  }
}
```

**NVMe-oF TCP:**
```json
{
  "driver_volume_type": "nvmeof",
  "data": {
    "target_nqn": "nqn.2026-02.io.strix:strix:<export_id>",
    "transport_type": "tcp",
    "target_portal": "0.0.0.0:4420",
    "ns_id": 1,
    "access_mode": "rw"
  }
}
```

### Admin (Fault/Delay Injection)

```bash
# Inject a fault (causes the next operation to fail with 500)
curl -X POST http://localhost:8080/admin/faults \
  -H "Content-Type: application/json" \
  -d '{"operation": "create_volume", "error_message": "simulated failure"}'

# List active faults
curl http://localhost:8080/admin/faults

# Clear a fault
curl -X DELETE http://localhost:8080/admin/faults/create_volume

# Inject a delay
curl -X POST http://localhost:8080/admin/delays \
  -H "Content-Type: application/json" \
  -d '{"operation": "create_volume", "delay_seconds": 5.0}'

# List active delays
curl http://localhost:8080/admin/delays

# Clear a delay
curl -X DELETE http://localhost:8080/admin/delays/create_volume
```

Injectable operations: `create_pool`, `create_volume`, `delete_volume`,
`extend_volume`, `create_host`, `create_mapping`, `delete_mapping`

### Health Check

```bash
curl http://localhost:8080/healthz
# {"status": "ok"}
```

## Manual Testing with Actual SPDK bdevs

### Setup SPDK on the Host

```bash
# 1. Configure hugepages (2048 x 2MB = 4GB)
echo 2048 | sudo tee /proc/sys/vm/nr_hugepages
sudo mkdir -p /dev/hugepages
sudo mount -t hugetlbfs nodev /dev/hugepages

# 2. Start SPDK target (from SPDK build directory)
sudo ./build/bin/spdk_tgt -S /var/tmp &

# Wait for socket to appear
sleep 2
ls -la /var/tmp/spdk.sock
```

### Test with malloc (in-memory) Pool

```bash
# 1. Start Strix Gateway
export STRIX_SPDK_SOCKET_PATH=/var/tmp/spdk.sock
uvicorn strix_gateway.main:app --host 0.0.0.0 --port 8080

# 2. Create a malloc pool (4GB)
POOL=$(curl -s -X POST http://localhost:8080/v1/pools \
  -H "Content-Type: application/json" \
  -d '{"name": "test-pool", "backend_type": "malloc", "size_mb": 4096}')
POOL_ID=$(echo $POOL | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Pool ID: $POOL_ID"

# 3. Create a 1GB volume
VOL=$(curl -s -X POST http://localhost:8080/v1/volumes \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"test-vol\", \"pool_id\": \"$POOL_ID\", \"size_mb\": 1024}")
VOL_ID=$(echo $VOL | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Volume ID: $VOL_ID"

# 4. Register a host
HOST=$(curl -s -X POST http://localhost:8080/v1/hosts \
  -H "Content-Type: application/json" \
  -d '{"name": "test-host", "iqn": "iqn.1993-08.org.debian:test", "nqn": "nqn.2014-08.org.nvmexpress:uuid:test"}')
HOST_ID=$(echo $HOST | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Host ID: $HOST_ID"

# 5a. Map via iSCSI
ISCSI_MAP=$(curl -s -X POST http://localhost:8080/v1/mappings \
  -H "Content-Type: application/json" \
  -d "{\"volume_id\": \"$VOL_ID\", \"host_id\": \"$HOST_ID\", \"protocol\": \"iscsi\"}")
ISCSI_MAP_ID=$(echo $ISCSI_MAP | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "iSCSI mapping: $ISCSI_MAP_ID"

# 5b. Get iSCSI connection info
curl -s http://localhost:8080/v1/mappings/$ISCSI_MAP_ID/connection-info | python3 -m json.tool

# 6. Connect with iscsiadm (from another host or locally)
# sudo iscsiadm -m discovery -t sendtargets -p <IP>:3260
# sudo iscsiadm -m node --login

# 7. Cleanup
curl -X DELETE http://localhost:8080/v1/mappings/$ISCSI_MAP_ID
curl -X DELETE http://localhost:8080/v1/volumes/$VOL_ID
```

### Test with AIO File Pool (Persistent)

```bash
# 1. Create a backing file (4GB)
dd if=/dev/zero of=/tmp/strix-pool.img bs=1M count=4096

# 2. Create an aio_file pool
POOL=$(curl -s -X POST http://localhost:8080/v1/pools \
  -H "Content-Type: application/json" \
  -d '{"name": "file-pool", "backend_type": "aio_file", "aio_path": "/tmp/strix-pool.img"}')
POOL_ID=$(echo $POOL | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 3. Create volume and map via NVMe-oF TCP
VOL=$(curl -s -X POST http://localhost:8080/v1/volumes \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"nvme-vol\", \"pool_id\": \"$POOL_ID\", \"size_mb\": 1024}")
VOL_ID=$(echo $VOL | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

HOST=$(curl -s -X POST http://localhost:8080/v1/hosts \
  -H "Content-Type: application/json" \
  -d '{"name": "nvme-host", "nqn": "nqn.2014-08.org.nvmexpress:uuid:test"}')
HOST_ID=$(echo $HOST | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

NVME_MAP=$(curl -s -X POST http://localhost:8080/v1/mappings \
  -H "Content-Type: application/json" \
  -d "{\"volume_id\": \"$VOL_ID\", \"host_id\": \"$HOST_ID\", \"protocol\": \"nvmeof_tcp\"}")
NVME_MAP_ID=$(echo $NVME_MAP | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 4. Get NVMe-oF connection info
curl -s http://localhost:8080/v1/mappings/$NVME_MAP_ID/connection-info | python3 -m json.tool

# 5. Connect with nvme-cli (from another host)
# sudo nvme discover -t tcp -a <IP> -s 4420
# sudo nvme connect -t tcp -n <NQN> -a <IP> -s 4420
```

## SPDK Naming Conventions

| Resource | Naming Format |
|---|---|
| lvol store | `{pool.name}` |
| Backing bdev | `strix-pool-{pool_uuid}` |
| lvol bdev | `{pool.name}/strix-vol-{volume_uuid}` |
| iSCSI target IQN | `iqn.2026-02.strix.strix:{export_container_id}` |
| NVMe subsystem NQN | `nqn.2026-02.io.strix:strix:{export_container_id}` |

## Volume Status Transitions

```
creating → available → in_use → available (after unmap)
    ↓          ↓          ↓
   error     error      error
    
available → extending → available
                ↓
              error

available → deleting → (deleted)
               ↓
             error
```

## Project Structure

```
strix_gateway/
├── __init__.py          # Package version
├── main.py              # FastAPI app entrypoint + lifespan
├── config.py            # Pydantic settings (env-based)
├── api/
│   ├── v1.py            # /v1/* routes (pools, volumes, hosts, mappings)
│   ├── subsystems.py    # /v1/subsystems/* routes
│   └── admin.py         # /admin/* routes (fault/delay injection)
├── cli/                 # ← NEW: human-facing CLI (thin REST client)
│   ├── main.py          # Typer app + all command groups
│   ├── client.py        # httpx client wrapper + name resolution
│   ├── output.py        # table / json / yaml formatting
│   ├── errors.py        # typed errors → exit codes
│   ├── svc.py           # IBM SVC façade debug wrapper
│   └── topo/
│       ├── models.py    # Pydantic topology models
│       ├── load.py      # YAML / TOML loader
│       ├── validate.py  # Cross-reference validation
│       └── apply.py     # Idempotent apply + smoke test
├── personalities/
│   ├── generic/         # Generic storage personality
│   └── svc/             # IBM SVC personality + SSH façade runtime
├── core/
│   ├── models.py        # Pydantic request/response schemas
│   ├── db.py            # SQLAlchemy 2.x async ORM models
│   ├── capabilities.py  # Capability-check helpers
│   ├── personas.py      # Persona defaults + CapabilityProfile
│   ├── reconcile.py     # Startup state reconciliation
│   └── faults.py        # In-memory fault/delay injection engine
├── spdk/
│   ├── rpc.py           # Synchronous JSON-RPC client (Unix socket)
│   ├── ensure.py        # Idempotent ensure_* functions
│   ├── iscsi.py         # iSCSI-specific RPC helpers
│   └── nvmf.py          # NVMe-oF-specific RPC helpers
└── topology/
    ├── schema.py        # Topology Pydantic models (server-side)
    ├── load.py          # Topology loader
    └── validate.py      # Topology validator
```

---

## Strix CLI

The `strix` command-line tool is a human-facing thin client for the Strix
Gateway REST API. It does **not** call SPDK directly.

### Installation

```bash
# Install the full package (includes CLI)
uv sync

# Verify
strix --help
```

### Configuration

```bash
# Set the API URL (default: http://localhost:8080)
export STRIX_URL=http://localhost:8080
```

Global flags available on every command:

| Flag | Default | Description |
|---|---|---|
| `--url URL` | `$STRIX_URL` / `http://localhost:8080` | API base URL |
| `--output table\|json\|yaml` | `table` | Output format |
| `--quiet` / `--verbose` | off | Control verbosity |
| `--timeout SECONDS` | `30` | HTTP timeout |

Exit codes: `0` success, `1` validation error, `2` API error, `3` unexpected.

### Usage Examples

#### Status

```bash
# Show all subsystems with pool/volume/mapping counts
strix status

# Filter to one subsystem
strix status --subsystem svc-a
```

#### Subsystems

```bash
strix subsystem ls
strix subsystem show svc-a
strix subsystem create svc-a --persona ibm_svc --protocol iscsi
strix subsystem rm svc-a --force
strix subsystem capabilities svc-a
strix subsystem set-capabilities svc-a -f examples/capabilities/ibm_svc_basic.yaml --merge
```

#### Pools

```bash
strix pool ls --subsystem svc-a
strix pool create gold --subsystem svc-a --backend malloc --size-gb 500
strix pool show gold --subsystem svc-a
strix pool rm gold --subsystem svc-a
```

#### Volumes

```bash
strix volume ls --subsystem svc-a
strix volume create vol-001 --pool gold --size-gb 20 --subsystem svc-a
strix volume show vol-001 --subsystem svc-a
strix volume extend vol-001 --subsystem svc-a --size-gb 40
strix volume rm vol-001 --subsystem svc-a
```

#### Hosts

```bash
strix host ls
strix host create compute-01
strix host add-initiator compute-01 --iscsi-iqn "iqn.1993-08.org.debian:01:abc123"
strix host show compute-01
strix host rm-initiator compute-01 --iscsi-iqn "iqn.1993-08.org.debian:01:abc123"
strix host rm compute-01
```

#### Mappings + Connection Info

```bash
strix map ls --subsystem svc-a
strix map create --subsystem svc-a --host compute-01 --volume vol-001 --protocol iscsi
strix connection-info --subsystem svc-a --host compute-01 --volume vol-001
strix map rm --subsystem svc-a --host compute-01 --volume vol-001
```

#### Topology / CI Workflows

```bash
# Validate a topology file
strix validate -f examples/ci/topo-min.yaml

# Apply idempotently (create missing resources)
strix apply -f examples/ci/topo-min.yaml

# Apply with strict mode (report live resources not in file)
strix apply -f examples/ci/topo-multi-subsystem.yaml --strict

# Smoke test (validate + check all resources exist + connection-info)
strix smoke -f examples/ci/topo-min.yaml
```

#### IBM SVC Façade Debug

```bash
# Run SVC commands through SSH ForceCommand (which forwards to /v1/svc/run)
ssh svc@<gateway-host> "svcinfo lssystem"
ssh svc@<gateway-host> "svcinfo lsmdiskgrp -delim :"
ssh svc@<gateway-host> "svctask mkvdisk -name testvol -mdiskgrp gold -size 10 -unit gb"
```

## License

GPL-3.0-only (GNU General Public License v3.0 only)
