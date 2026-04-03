# Vendor Conformance System

Strix Gateway enforces that every storage personality has E2E coverage before it can be merged.
This document describes the conformance system, the coverage model, and the process for adding
a new vendor.

## Coverage levels

| Level | Definition |
|---|---|
| **implemented** | Personality exists under `strix_gateway/personalities/` with `app.py` and passes unit tests. |
| **e2e-covered** | At least one scenario runs through the full Cinder volume lifecycle (create, attach, detach, delete) against the gateway. |
| **fully-covered** | Both iSCSI and FC transports have E2E scenarios, and vhost mode is validated. |

A new vendor personality must reach **e2e-covered** before merging. iSCSI is the required baseline;
FC and vhost can follow in subsequent PRs.

## How the system works

Three components enforce conformance:

### 1. `tests/vendors.yaml` — the manifest

The single source of truth. Every personality is listed here with:
- `personality_dir` — subdirectory under `strix_gateway/personalities/`
- `registry_id` — key passed to `personality_registry.register()`
- `e2e_required` — whether E2E coverage is mandatory
- `scenarios` — list of scenario names that must exist on disk

### 2. `scripts/validate_vendors.py` — completeness check

Discovers personality directories by checking for `app.py` presence (the authoritative signal
that a directory is a real personality, not infrastructure). Validates:
- Every personality with `app.py` appears in `vendors.yaml`
- Every `e2e_required` vendor has at least one scenario
- All required fixture files exist for every declared scenario

Run manually:
```bash
python scripts/validate_vendors.py
```

### 3. `scripts/generate_e2e_matrix.py` — matrix generation

Reads `vendors.yaml` and each `scenario.yaml`, emits `tests/e2e/scenarios/matrix.env`.
Also renders `backend.conf.j2` templates to `cinder-backend.conf` files.

Run manually:
```bash
python scripts/generate_e2e_matrix.py          # regenerate
python scripts/generate_e2e_matrix.py --check  # verify no drift (CI mode)
```

### CI enforcement

`.github/workflows/vendor-conformance.yml` runs both scripts on every PR that touches
`strix_gateway/personalities/**`, `tests/vendors.yaml`, `tests/e2e/vendors/**`, or
`tests/e2e/drivers/**`. The workflow fails if:
- A personality exists without a `vendors.yaml` entry
- A declared scenario is missing required files
- `matrix.env` is out of sync with the declared scenarios

## Directory layout

```
tests/
├── vendors.yaml                            # Vendor manifest
└── e2e/
    ├── vendors/
    │   └── <personality_dir>/
    │       └── <scenario_name>/
    │           ├── scenario.yaml           # Scenario metadata
    │           ├── backend.conf.j2         # Jinja2 config template (optional)
    │           └── seed.sh                 # Pre-test hook
    └── drivers/
        └── <driver_dir>/
            ├── topo.yaml                   # Array topology (non-vhost)
            ├── topo-vhost.yaml             # Array topology (vhost, if applicable)
            ├── cinder-backend.conf         # Rendered Cinder backend config
            └── verify.sh                   # Transport verification script
```

## Adding a new vendor

Follow these steps. CI will fail if any step is missing.

### Step 1: Implement the personality

Create `strix_gateway/personalities/<vendor>/app.py` (and the rest of the personality package).
The `app.py` must register with `personality_registry`:

```python
personality_registry.register("<registry_id>", MyVendorAppFactory())
```

### Step 2: Add to vendors.yaml

```yaml
- personality_dir: <vendor>
  registry_id: <registry_id>
  e2e_required: true
  scenarios:
    - name: <vendor>_iscsi
      description: "<Vendor> iSCSI transport"
```

### Step 3: Create scenario metadata

Create `tests/e2e/vendors/<vendor>/<scenario_name>/`:

**`scenario.yaml`** (required):
```yaml
driver_dir: <vendor>_iscsi       # maps to tests/e2e/drivers/<driver_dir>/
modes:
  - non-vhost                     # or [non-vhost, vhost] for both modes
needs_fc: false
transport: iscsi
cinder_driver: cinder.volume.drivers.<vendor>.<module>.<ClassName>
```

**`backend.conf.j2`** (optional — if the driver conf needs rendering):
```ini
[<vendor>-iscsi]
volume_driver = cinder.volume.drivers.<vendor>.<module>.<ClassName>
volume_backend_name = <vendor>-iscsi
san_ip = {{ GATEWAY_IP }}
san_login = <user>
san_password = <password>
# REST-based drivers also need:
# api_url = http://{{ GATEWAY_IP }}:{{ GATEWAY_PORT }}/api/v1
```

**`seed.sh`** (required, may be a no-op):
```bash
#!/usr/bin/env bash
: # no-op
```

### Step 4: Create driver execution files

Create `tests/e2e/drivers/<driver_dir>/`:

**`topo.yaml`** (required):
```yaml
arrays:
  - name: default
    vendor: <registry_id>
    profile:
      model: "..."
      version: "..."
      features:
        thin_provisioning: true
        snapshots: true
    endpoints:
      - protocol: iscsi
        targets:
          target_iqn: "iqn.2026-03.com.lunacy:strix.e2e.target"
pools:
  - name: gold
    array: default
    backend: malloc
    size_gb: 100
```

**`cinder-backend.conf`** (required — can be rendered from j2 or written directly):
Uses `${GATEWAY_IP}`, `${GATEWAY_PORT}`, `${GATEWAY_SSH_PORT}`, `${SVC_PASSWORD}` bash variables.
These are substituted at test runtime by `configure_cinder_backend()`.

**`verify.sh`** (required):
Sources a `verify_iscsi()` or `verify_fc()` function called by `test_flow.sh`.
For iSCSI, copy the pattern from `drivers/svc_iscsi/verify.sh`.

### Step 5: Extend test_flow.sh if needed

If the new driver uses an existing transport (iSCSI or FC), add it to the existing
`case` branches in `tests/e2e/vm/test_flow.sh`:

```bash
# 4 locations — search for svc_iscsi and add the new driver name with |
svc_iscsi | hitachi_iscsi | hpe3par_iscsi | <vendor>_iscsi)
```

If the driver uses a new transport protocol, implement the full connector/connect/disconnect
logic following the `svc_iscsi` pattern.

### Step 6: Regenerate and validate

```bash
python scripts/generate_e2e_matrix.py   # renders j2, writes matrix.env
python scripts/validate_vendors.py      # must exit 0
python scripts/generate_e2e_matrix.py --check  # must exit 0
```

### Step 7: Run locally (optional but recommended)

```bash
cd tests/e2e
./run_all.sh --filter '<vendor>_iscsi'
```

## Template variable reference

These variables are available in `backend.conf.j2` templates and in `cinder-backend.conf` files:

| Variable | Source | Default | Description |
|---|---|---|---|
| `GATEWAY_IP` | LXD VM IP | — | Routable IP of the gateway VM |
| `GATEWAY_PORT` | env | `8080` | Gateway HTTP port |
| `GATEWAY_SSH_PORT` | env | `22` | SSH port for SVC-style CLI facades |
| `SVC_PASSWORD` | env | `strix_svc_pass` | Password for SVC SSH facade |

### Two-stage rendering

Templates use `{{ VAR }}` (Jinja2 style). The generator translates these to `${VAR}` (bash style)
in the rendered `cinder-backend.conf`. The actual IP/port values are substituted at test runtime
inside the gateway VM by `configure_cinder_backend()` in `tests/e2e/lib/openstack.sh`.

This means:
- `backend.conf.j2` is the human-editable source (readable, IDE-friendly)
- `cinder-backend.conf` is a committed artifact in bash-variable form (no Python needed at runtime)
- Runtime values stay in the shell environment where they belong

## Minimum E2E contract

Every vendor must pass the following volume lifecycle test (implemented in `tests/e2e/vm/test_flow.sh`):

1. Create a 1 GiB volume via Cinder API
2. Build connector properties (iSCSI initiator IQN, host IP, hostname)
3. Create a volume attachment with the connector
4. Connect via `os-brick` (discovers and returns block device path)
5. Run transport-specific verification (`verify.sh`)
6. Write and read back data via the block device (SHA-256 integrity check)
7. Disconnect via `os-brick`
8. Delete the attachment
9. Delete the volume

FC is not required for initial coverage. iSCSI is the required baseline.
