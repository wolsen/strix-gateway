# Snap Configuration Reference

Apollo Gateway configuration is managed via `snap set` / `snap get`. All keys are written to `$SNAP_DATA/config/apollo.env` as `APOLLO_*` environment variables and applied on service restart.

## Configuration Keys

### Storage Protocol Settings

| Key | Environment Variable | Default | Description |
|-----|---------------------|---------|-------------|
| `iscsi-portal-ip` | `APOLLO_ISCSI_PORTAL_IP` | `0.0.0.0` | iSCSI portal listen address |
| `iscsi-portal-port` | `APOLLO_ISCSI_PORTAL_PORT` | `3260` | iSCSI portal listen port |
| `nvmef-portal-ip` | `APOLLO_NVMEF_PORTAL_IP` | `0.0.0.0` | NVMe-oF TCP portal listen address |
| `nvmef-portal-port` | `APOLLO_NVMEF_PORTAL_PORT` | `4420` | NVMe-oF TCP portal listen port |
| `iqn-prefix` | `APOLLO_IQN_PREFIX` | `iqn.2026-02.lunacysystems.apollo` | iSCSI target IQN prefix |
| `nqn-prefix` | `APOLLO_NQN_PREFIX` | `nqn.2026-02.io.lunacysystems:apollo` | NVMe-oF NQN prefix |

### Virtual-Host Multiplexing

| Key | Environment Variable | Default | Description |
|-----|---------------------|---------|-------------|
| `vhost-enabled` | `APOLLO_VHOST_ENABLED` | `false` | Enable TLS vhost multiplexing |
| `vhost-domain` | `APOLLO_VHOST_DOMAIN` | *(empty)* | Domain suffix for subsystem FQDNs (required if vhost enabled) |
| `vhost-hostname-override` | `APOLLO_VHOST_HOSTNAME_OVERRIDE` | *(auto-detect)* | Override the hostname component of FQDNs |
| `vhost-require-match` | `APOLLO_VHOST_REQUIRE_MATCH` | `true` | Return 404 for unrecognised Host headers |

### TLS Settings

| Key | Environment Variable | Default | Description |
|-----|---------------------|---------|-------------|
| `tls-mode` | `APOLLO_TLS_MODE` | `per-subsystem` | Certificate mode: `per-subsystem` or `wildcard` |
| `tls-rotate-before-days` | `APOLLO_TLS_ROTATE_BEFORE_DAYS` | `30` | Reissue certs expiring within this many days |

### Bind Settings

| Key | Environment Variable | Default | Description |
|-----|---------------------|---------|-------------|
| `bind-https-port` | `APOLLO_BIND_HTTPS_PORT` | `443` | HTTPS listen port (when vhost enabled) |
| `bind-http-port` | `APOLLO_BIND_HTTP_PORT` | `0` | HTTP listen port (0 = disabled) |

## Usage Examples

```bash
# Enable vhost mode
sudo snap set apollo-gateway \
    vhost-enabled=true \
    vhost-domain=storage.example.com

# Change HTTPS port
sudo snap set apollo-gateway bind-https-port=8443

# Use wildcard certificate mode
sudo snap set apollo-gateway tls-mode=wildcard

# Override hostname detection
sudo snap set apollo-gateway vhost-hostname-override=gw01

# View current settings
sudo snap get apollo-gateway
```

## Port Binding

The snap service runs as root, so binding to privileged ports (80, 443) works without additional configuration.

When vhost mode is **disabled**, the gateway listens on HTTP port 8080 (default). When **enabled**, it listens on HTTPS port 443 (default). Both ports are configurable.

## Certificate Storage

All TLS state is stored under `$SNAP_COMMON/tls/`:

```
/var/snap/apollo-gateway/common/tls/
  ca.key           # CA private key (0600 permissions)
  ca.crt           # CA certificate (self-signed, 10-year validity)
  leaf/
    <fqdn>.key     # Per-subsystem leaf key
    <fqdn>.crt     # Per-subsystem leaf cert (365-day validity)
```

### Backing Up the CA

For persistent CI environments, back up the CA files after first generation:

```bash
sudo cp /var/snap/apollo-gateway/common/tls/ca.{key,crt} /safe/backup/
```

Restoring these files before starting the gateway ensures previously distributed CA trust remains valid.

### Security Considerations

- **`ca.key` is sensitive** — it can sign certificates for any subsystem hostname. Protect it with filesystem permissions and restrict access to the gateway host.
- The `/v1/tls/sync` and `/v1/tls/ca` endpoints are not authenticated. In production, restrict access by network (firewall rules or bind address).
- Leaf certificate private keys (`leaf/*.key`) are stored with 0600 permissions.

## Runtime Paths

| Path | Description |
|------|-------------|
| `$SNAP_COMMON/run/spdk.sock` | SPDK JSON-RPC Unix socket |
| `$SNAP_DATA/apollo_gateway.db` | SQLite database |
| `$SNAP_DATA/config/apollo.env` | Generated environment file |
| `$SNAP_COMMON/tls/` | TLS certificates and keys |
