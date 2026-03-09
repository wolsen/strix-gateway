# Apollo Gateway Compatibility Layer Architecture  
**Project:** Apollo Gateway  
**Organization:** Lunacy Systems  

---

## 1. Purpose

Apollo Gateway provides a canonical storage control-plane backed by SPDK.  
To support OpenStack Cinder driver functional testing across multiple vendors, Apollo implements **vendor compatibility façades** that emulate the management interfaces of real storage arrays.

This document defines:

- The architectural boundaries between the canonical core and vendor façades
- How vendor-specific APIs (REST, SSH CLI) are implemented
- Error and behavior translation rules
- Extension patterns for adding new vendors
- Testing and logging requirements

---

## 2. High-Level Architecture

Apollo Gateway consists of three logical layers:

```
+---------------------------------------------------------+
| Vendor Compatibility Façades                            |
|  - IBM SVC (SSH CLI)                                    |
|  - Pure (REST)                                          |
|  - HPE 3PAR (REST)                                      |
|  - Hitachi (REST + Jobs)                                |
+---------------------------------------------------------+
| Canonical Core (Vendor-Agnostic)                        |
|  - Pools                                                |
|  - Volumes                                              |
|  - Hosts                                                |
|  - Export Containers                                    |
|  - Mappings                                             |
|  - Fault Injection                                      |
+---------------------------------------------------------+
| SPDK Dataplane                                          |
|  - lvol stores (pools)                                  |
|  - lvol bdevs (volumes)                                 |
|  - iSCSI target                                         |
|  - NVMe-oF target                                       |
+---------------------------------------------------------+
```

### Design Principle

Vendor façades:

- MUST NOT directly program SPDK.
- MUST NOT bypass canonical validation logic.
- MUST call canonical core operations.

The canonical core:

- MUST remain vendor-agnostic.
- MUST not contain vendor-specific logic.
- MUST expose stable internal APIs.

---

## 3. Compatibility Layer Goals

The compatibility layer exists to:

1. Emulate vendor APIs sufficiently for Cinder driver functional testing.
2. Provide deterministic, reproducible behavior.
3. Enable fault injection and protocol behavior simulation.
4. Allow incremental vendor support without cross-contamination.

It is NOT intended to:

- Fully implement vendor arrays.
- Achieve performance parity.
- Replace vendor simulators in production.

---

## 4. Vendor Façade Structure

Each vendor has its own isolated module:

```
strix_gateway/compat/<vendor>/
```

Example:

```
strix_gateway/compat/
  ibm_svc/
  pure/
  hpe3par/
  hitachi/
```

Each façade contains:

```
api.py        # Router (FastAPI) or CLI dispatcher
schemas.py    # Vendor request/response models
translate.py  # Mapping vendor semantics -> canonical core calls
errors.py     # Vendor-specific error mapping
format.py     # Output formatting (if applicable)
jobs.py       # (Optional) async job emulation
```

IBM SVC is special:

- Uses SSH + ForceCommand
- CLI parsing instead of REST routing

---

## 5. Canonical Core Contract

The compatibility layer may only interact with the canonical core through defined service functions.

### Required Canonical Operations

- `create_pool(...)`
- `list_pools()`
- `create_volume(...)`
- `delete_volume(...)`
- `extend_volume(...)`
- `create_host(...)`
- `delete_host(...)`
- `add_initiator(...)`
- `create_mapping(...)`
- `delete_mapping(...)`
- `get_connection_info(...)`
- `get_stats()`

Vendor façades must not manipulate database state directly.

---

## 6. API Isolation Rules

Each vendor façade must:

- Own its URL prefix:
  - `/compat/pure/...`
  - `/compat/hpe3par/...`
  - `/compat/hitachi/...`
- Own its authentication model
- Own its HTTP status codes
- Own its error formats
- Own its response schemas

Vendor façades may:

- Transform canonical objects into vendor-specific representations
- Simulate vendor-specific quirks
- Implement async job models

Vendor façades must NOT:

- Share response models with other vendors
- Leak vendor fields into canonical DB schema
- Modify canonical objects in vendor-specific ways

---

## 7. Pass-Through Philosophy

In early implementations, vendor façades may behave as thin translators:

```
Vendor Request
     ↓
Translate
     ↓
Canonical Core Call
     ↓
Translate Response
     ↓
Vendor Response
```

However, each façade must remain independent, even if 80% identical to another vendor at first.

This ensures:

- Future vendor quirks are isolated
- Profiles can diverge safely
- Test surface remains deterministic

---

## 8. Vendor-Specific Behavior Modeling

Vendor façades may implement the following behaviors.

### 8.1 Async Job Emulation

Some vendors (e.g., Hitachi) return a job object:

```
POST /volume → 202 Accepted
{ "jobId": 123 }
```

Apollo should:

- Create a job record
- Execute canonical operation immediately
- Allow job polling endpoint
- Optionally simulate delays

Job behavior must be confined to the vendor façade.

---

### 8.2 Idempotency Rules

Vendors differ in how they respond to duplicate create/delete operations.

Examples:

- Pure may return HTTP 409
- IBM CLI may return exit code 1 with error text
- HPE may return HTTP 400 with message

Façade must map canonical exceptions into vendor-specific responses.

---

### 8.3 Capability Exposure

Each façade defines its capability profile:

- Thin provisioning
- Snapshots
- QoS
- Multi-attach
- NVMe-oF support

Capabilities must:

- Be declared in vendor façade configuration
- Not alter canonical core logic
- Only affect how responses are shaped

---

## 9. Error Translation Model

Canonical core raises typed exceptions:

- `NotFoundError`
- `AlreadyExistsError`
- `ValidationError`
- `BackendError`
- `ConflictError`

Vendor façade must translate these into:

- HTTP codes (REST)
- CLI exit codes (SSH)
- Vendor-specific error body formats

Example mapping (Pure façade):

| Canonical Error     | HTTP Code | Example Pure Response             |
|---------------------|-----------|-----------------------------------|
| NotFoundError       | 404       | `{ "error": "not found" }`        |
| AlreadyExistsError  | 409       | `{ "error": "already exists" }`   |
| ValidationError     | 400       | `{ "error": "invalid input" }`    |
| BackendError        | 500       | `{ "error": "backend error" }`    |

IBM SVC façade:

- Exit code 1
- stderr contains stable error string

---

## 10. Logging Requirements

Each façade must:

- Log every request
- Log canonical operation calls
- Log translated errors
- Include request ID correlation

IBM SVC façade must additionally log:

- SSH original command
- Exit code
- Duration
- Output size

All logs must use logger namespace:

```
strix_gateway.compat.<vendor>
```

---

## 11. Adding a New Vendor

To add a new vendor:

1. Create new directory under `compat/`.
2. Implement:
   - `api.py`
   - `schemas.py`
   - `translate.py`
   - `errors.py`
3. Define capability profile.
4. Add integration tests.
5. Ensure no modifications to canonical core were required.

If core modification is needed:

- Re-evaluate canonical abstraction boundary.

---

## 12. Testing Strategy

Each vendor façade must have:

### Unit tests:

- Translation logic
- Error mapping
- Capability reporting

### Integration tests:

- Volume lifecycle
- Mapping lifecycle
- Connection info correctness

IBM SVC façade must also test:

- Command parsing
- CLI formatting
- Exit codes

---

## 13. Long-Term Evolution

Apollo Gateway compatibility layer is designed to support:

- Multiple vendor personas simultaneously
- Vendor behavior profiles
- Fault injection per vendor
- Chaos testing Cinder driver retry logic
- Capability fuzzing

The canonical core remains stable while façades evolve.

---

## 14. Guiding Principle

Apollo Gateway is not pretending to be a specific array.

It is:

> A deterministic storage personality engine.

Each façade is a personality.  
The core is the physics engine.  
SPDK is the machinery beneath the stage.
