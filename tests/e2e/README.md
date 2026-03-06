# E2E Tests — Apollo Gateway

End-to-end tests that spin up LXD VMs, deploy a minimal Keystone + Cinder
stack, and exercise Cinder volume lifecycle through the Apollo Gateway using
real storage driver paths (SVC iSCSI, SVC FC, etc.) in both **vhost** and
**non-vhost** modes.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Host (test runner)                                  │
│  ./run_all.sh                                        │
│    ├─ creates 2 LXD VMs                              │
│    ├─ pushes repos + lib scripts                     │
│    ├─ runs one-time setup on each VM                 │
│    └─ iterates scenario matrix                       │
│         └─ scenarios/run_scenario.sh                 │
│              ├─ resets + reconfigures gateway         │
│              ├─ resets + reconfigures Cinder          │
│              └─ runs vm/test_flow.sh on consumer     │
│                   └─ Cinder-driven volume lifecycle   │
├──────────────────────────────────────────────────────┤
│  Gateway VM (e2e-gw-*)                               │
│    ├─ Apollo Gateway (FastAPI)                       │
│    ├─ Fake SPDK JSON-RPC socket                      │
│    ├─ targetcli-fb iSCSI target                      │
│    ├─ SVC SSH facade (ForceCommand)                  │
│    ├─ Keystone (uwsgi, SQLite)                       │
│    ├─ RabbitMQ                                       │
│    └─ Cinder (cinder-all, SQLite)                    │
├──────────────────────────────────────────────────────┤
│  Consumer VM (e2e-con-*)                             │
│    ├─ open-iscsi / iscsid                            │
│    ├─ os-brick (iSCSI + FC connectors)               │
│    ├─ apollo_fc.ko + dm_apollo_fc.ko (FC scenarios)  │
│    ├─ apollo-fcctl agent (FC scenarios)               │
│    └─ openstackclient + cinderclient                 │
└──────────────────────────────────────────────────────┘
```

## Prerequisites

- Linux host with **LXD** installed (`snap install lxd && lxd init --auto`)
- Both repos checked out side by side:
  ```
  lunacy-systems/
  ├── apollo-gateway/    # this repo
  └── apollo-fc/         # optional — needed only for FC scenarios
  ```
- Host connectivity to LXD VMs (default LXD bridge)

## Quick Start

```bash
cd apollo-gateway/tests/e2e

# Run all scenarios
./run_all.sh

# Run only iSCSI scenarios
./run_all.sh --filter 'svc_iscsi'

# Run only vhost scenarios
./run_all.sh --filter 'vhost'

# Run a single scenario
./run_all.sh --filter 'svc_iscsi/non-vhost'

# Keep VMs around for debugging on failure
./run_all.sh --keep-vms

# Reuse existing VMs (skip VM creation)
./run_all.sh --reuse-vms

# Destroy VMs on success (default: preserve)
./run_all.sh --destroy-on-success
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KEEP_VMS` | `0` | Keep VMs on failure |
| `REUSE_VMS` | `0` | Skip VM creation if VMs exist |
| `DESTROY_ON_SUCCESS` | `0` | Destroy VMs after all pass |
| `SCENARIOS_FILTER` | `""` | Regex to select scenarios |
| `LXD_IMAGE` | `ubuntu:24.04` | LXD image for VMs |
| `GATEWAY_ROOT` | auto-detected | Path to apollo-gateway repo |
| `FC_ROOT` | auto-detected | Path to apollo-fc repo |
| `SVC_PASSWORD` | `apollo_svc_pass` | SSH password for SVC facade |
| `GATEWAY_PORT` | `8080` | Gateway HTTP listen port |

## Scenario Matrix

Defined in [`scenarios/matrix.env`](scenarios/matrix.env):

```
# driver       mode        enable_fc
svc_iscsi      non-vhost   false
svc_iscsi      vhost       false
svc_fc         non-vhost   true
svc_fc         vhost       true
```

Each row is a separate scenario run one at a time. Between scenarios the
gateway and Cinder databases are wiped and services restarted.

## Test Flow (per scenario)

1. **Gateway reset** — kill gateway, wipe DB
2. **Gateway start** — start fake SPDK + gateway (vhost or non-vhost)
3. **Topology apply** — load driver-specific topology YAML
4. **Cinder reset** — wipe Cinder DB, configure backend from driver conf
5. **Cinder start** — `cinder-manage db sync && cinder-all`
6. **Volume create** — `openstack volume create` from consumer VM
7. **Attachment create** — `openstack volume attachment create`
8. **os-brick connect** — iSCSI login or FC HBA attach
9. **Driver verify** — `verify.sh` checks transport-specific paths
10. **Data verify** — write + read + SHA-256 on block device
11. **Cleanup** — os-brick disconnect, attachment delete, volume delete

## Directory Layout

```
tests/e2e/
├── run_all.sh                    # Top-level orchestrator
├── README.md                     # This file
├── lib/
│   ├── common.sh                 # Logging, assertions, waiters
│   ├── lxd.sh                    # LXD VM lifecycle
│   ├── openstack.sh              # Keystone + Cinder install
│   ├── gateway.sh                # Gateway + fake SPDK + SSH facade
│   └── consumer.sh               # Consumer deps, os-brick, FC modules
├── drivers/
│   └── <driver_name>/            # One dir per driver
│       ├── cinder-backend.conf   # Cinder backend config template
│       ├── topo.yaml             # Non-vhost topology
│       ├── topo-vhost.yaml       # Vhost topology
│       └── verify.sh             # Transport-specific verification
├── scenarios/
│   ├── matrix.env                # Scenario definitions
│   └── run_scenario.sh           # Per-scenario lifecycle
└── vm/
    ├── gateway_setup.sh          # One-time gateway VM setup
    ├── consumer_setup.sh         # One-time consumer VM setup
    └── test_flow.sh              # Cinder-driven test (runs on consumer)
```

## Adding a New Driver

1. Create `drivers/<name>/`:
   - `cinder-backend.conf` — Cinder backend config with `${GATEWAY_IP}`,
     `${GATEWAY_SSH_PORT}`, `${SVC_PASSWORD}` placeholders
   - `topo.yaml` — topology for non-vhost mode
   - `topo-vhost.yaml` — topology for vhost mode (array name becomes FQDN)
   - `verify.sh` — transport-specific assertions (block device, sessions, etc.)

2. Add a row to `scenarios/matrix.env`:
   ```
   <name>    non-vhost   <true|false>
   <name>    vhost       <true|false>
   ```

3. If the driver requires a new personality in Apollo Gateway, implement it in
   `apollo_gateway/compat/` and ensure the topology YAML references it.

## Adding a New Personality

1. Implement the personality in `apollo_gateway/compat/<vendor>/`
2. Register it in `apollo_gateway/core/personas.py`
3. Create driver configs under `drivers/<vendor>_<transport>/`
4. The existing test flow (`test_flow.sh`) is driver-agnostic — it uses
   `openstack` CLI for volume lifecycle and `os-brick` for data-path verification
