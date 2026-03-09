# TLS Virtual-Host Multiplexing

## Problem

OpenStack Cinder storage drivers connect to vendor REST APIs on standard ports (443/80) using the hostname configured by the operator. Strix Gateway emulates multiple storage arrays (subsystems), but traditionally exposes a single HTTP endpoint. To avoid modifying Cinder drivers, Strix must route incoming HTTPS requests to the correct subsystem based on the hostname the driver connects to.

## How It Works

When vhost mode is enabled, Strix Gateway:

1. **Generates an internal CA** and per-subsystem leaf certificates with correct SANs
2. **Listens on port 443** (configurable) with TLS enabled
3. **Selects the correct certificate** during the TLS handshake using SNI (Server Name Indication)
4. **Routes requests to the correct subsystem** by matching the HTTP `Host` header to a known subsystem FQDN

No external reverse proxy (nginx, traefik, envoy) is required.

## Default FQDN Pattern

Each subsystem is reachable at:

```
<subsystem-name>.<hostname>.<domain>
```

For example, on a host named `gw01` with domain `lab.example`:

| Subsystem  | FQDN                        |
|------------|-----------------------------|
| `default`  | `default.gw01.lab.example`  |
| `pure-a`   | `pure-a.gw01.lab.example`   |
| `svc-test` | `svc-test.gw01.lab.example` |

- **hostname** is derived from `socket.gethostname()` (short form) unless overridden
- **domain** is set via configuration (required)
- **subsystem name** must be DNS-label-safe: lowercase letters, digits, hyphens; starts with a letter; max 63 chars

## Enabling Vhost Mode

### Snap

```bash
sudo snap set strix-gateway \
    vhost-enabled=true \
    vhost-domain=lab.example

# Optional overrides:
sudo snap set strix-gateway \
    vhost-hostname-override=gw01 \
    bind-https-port=443 \
    tls-mode=per-subsystem
```

The gateway service restarts automatically after `snap set`.

### Development (non-snap)

```bash
export STRIX_VHOST_ENABLED=true
export STRIX_VHOST_DOMAIN=lab.example
export STRIX_TLS_DIR=./tls
export STRIX_BIND_HTTPS_PORT=8443

python -m strix_gateway.server
```

## TLS Modes

### per-subsystem (default)

A separate leaf certificate is issued for each subsystem FQDN. The SNI callback selects the correct certificate during TLS handshake. This is the most explicit and compatible mode.

### wildcard

A single wildcard certificate is issued for `*.<hostname>.<domain>`. This is simpler but requires all clients to accept wildcard certificates.

```bash
sudo snap set strix-gateway tls-mode=wildcard
```

## Certificate Management

### Internal CA

On first startup with vhost enabled, Strix generates:

- `$SNAP_COMMON/tls/ca.key` — ECDSA P-256 private key (600 permissions)
- `$SNAP_COMMON/tls/ca.crt` — Self-signed CA certificate (10-year validity)

Leaf certificates are stored in `$SNAP_COMMON/tls/leaf/`.

### Obtaining the CA Certificate

```bash
# Via API
curl -k https://<any-vhost-fqdn>/v1/tls/ca > strix-ca.crt

# Via filesystem (on the gateway host)
cat /var/snap/strix-gateway/common/tls/ca.crt
```

### Installing the CA in Test Environments

**Ubuntu/Debian:**

```bash
sudo cp strix-ca.crt /usr/local/share/ca-certificates/strix-gateway.crt
sudo update-ca-certificates
```

**LXD container:**

```bash
lxc file push strix-ca.crt mycontainer/usr/local/share/ca-certificates/strix-gateway.crt
lxc exec mycontainer -- update-ca-certificates
```

**Python (requests/urllib3):**

```bash
export REQUESTS_CA_BUNDLE=/path/to/strix-ca.crt
# or
export SSL_CERT_FILE=/path/to/strix-ca.crt
```

### Certificate Rotation

Leaf certificates are issued with 365-day validity. Strix checks for expiration on startup and when `POST /v1/tls/sync` is called. Certificates are reissued when:

- The cert file is missing
- SANs have changed (e.g. subsystem renamed)
- The certificate expires within `tls-rotate-before-days` (default: 30)

To force a re-sync:

```bash
curl -X POST https://<any-vhost-fqdn>/v1/tls/sync
```

### Stable CA for CI

For CI environments that need a persistent CA:

1. Let Strix generate the CA on first run
2. Back up `$SNAP_COMMON/tls/ca.key` and `$SNAP_COMMON/tls/ca.crt`
3. Pre-install the CA cert in CI base images
4. Restoring the backed-up CA files ensures leaf certs remain trusted across gateway rebuilds

## TLS Verification Guidance

### Strict verification (`verify=yes`)

The Cinder driver or client must:

1. Trust the Strix CA certificate
2. Connect using the subsystem's FQDN (not an IP address)
3. DNS must resolve the FQDN to the gateway's IP

### Relaxed verification (`verify=no`)

Works regardless of CA trust or hostname. Strix still serves the correct certificate via SNI, but the client ignores verification errors. Common in development/lab environments.

## IP Address Considerations

If a driver connects by IP address instead of hostname:

- **SNI will be absent** — Strix serves the default subsystem's certificate
- **Hostname verification fails** — the certificate's SANs contain DNS names, not IP addresses
- **Host header routing still works** if the driver sends a `Host` header with the FQDN

**Recommendation:** Always use DNS names. Add DNS entries (or `/etc/hosts`) for each subsystem FQDN pointing to the gateway IP:

```
10.0.0.50  default.gw01.lab.example
10.0.0.50  pure-a.gw01.lab.example
10.0.0.50  svc-test.gw01.lab.example
```

## API Endpoints

### GET /v1/vhosts

List all subsystem-to-FQDN mappings:

```json
{
  "vhost_enabled": true,
  "domain": "lab.example",
  "tls_mode": "per-subsystem",
  "mappings": [
    {"subsystem_name": "default", "subsystem_id": "...", "fqdn": "default.gw01.lab.example"},
    {"subsystem_name": "pure-a", "subsystem_id": "...", "fqdn": "pure-a.gw01.lab.example"}
  ]
}
```

### POST /v1/tls/sync

Trigger certificate re-synchronization. Re-scans all subsystems, issues missing certs, rotates expiring ones.

### GET /v1/tls/ca

Returns the CA certificate in PEM format (`application/x-pem-file`).

## Troubleshooting

### Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `certificate verify failed` | Client does not trust the Strix CA | Install the CA cert (see above) |
| `hostname mismatch` | Client connects by IP or wrong hostname | Use the correct FQDN; check `GET /v1/vhosts` |
| HTTP 404 `Unknown host: ...` | Host header doesn't match any subsystem | Check FQDN spelling; `vhost-require-match=false` to disable |
| `No TLS leaf certificates found` | No subsystems exist or TLS dir is empty | Run `POST /v1/tls/sync`; check `$SNAP_COMMON/tls/leaf/` |

### Logs

```bash
# Snap
sudo journalctl -u snap.strix-gateway.strix-gateway -f

# Development
# Logs go to stdout with the format:
# 2026-03-01 12:00:00 INFO strix_gateway.tls.manager — Issued leaf cert for pure-a.gw01.lab.example
```

### Checking vhost state

```bash
# List all mappings
curl -s https://<fqdn>/v1/vhosts | python3 -m json.tool

# Inspect a certificate's SANs
openssl x509 -in /var/snap/strix-gateway/common/tls/leaf/<fqdn>.crt -text -noout | grep DNS

# Test TLS handshake
openssl s_client -connect <ip>:443 -servername pure-a.gw01.lab.example </dev/null
```
