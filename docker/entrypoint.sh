#!/bin/bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Container entrypoint: configure the svc SSH user at runtime, start sshd
# in the background, then exec the FastAPI gateway in the foreground.
#
# Environment variables
# ---------------------
# APOLLO_SVC_SSH_PASSWORD   (optional) plaintext password for user "svc".
#                           Required if Cinder is configured with san_password.
#                           If unset only public-key auth is available.
#
# APOLLO_SVC_AUTHORIZED_KEYS (optional) newline-separated public keys to install
#                             in /home/svc/.ssh/authorized_keys.  Pass the key
#                             via docker-compose environment or a secret.

set -euo pipefail

# ── svc user password ──────────────────────────────────────────────────────
if [ -n "${APOLLO_SVC_SSH_PASSWORD:-}" ]; then
    echo "svc:${APOLLO_SVC_SSH_PASSWORD}" | chpasswd
    echo "[entrypoint] svc user password set from APOLLO_SVC_SSH_PASSWORD"
else
    echo "[entrypoint] APOLLO_SVC_SSH_PASSWORD not set — only public-key auth available for svc"
fi

# ── svc user authorized_keys ───────────────────────────────────────────────
if [ -n "${APOLLO_SVC_AUTHORIZED_KEYS:-}" ]; then
    printf '%s\n' "${APOLLO_SVC_AUTHORIZED_KEYS}" > /home/svc/.ssh/authorized_keys
    chown svc:svc /home/svc/.ssh/authorized_keys
    chmod 600 /home/svc/.ssh/authorized_keys
    echo "[entrypoint] installed authorized_keys for svc"
fi

# ── start sshd ─────────────────────────────────────────────────────────────
echo "[entrypoint] starting sshd"
/usr/sbin/sshd -D &
SSHD_PID=$!

# Propagate SIGTERM to sshd when the container stops
trap "kill ${SSHD_PID} 2>/dev/null; exit 0" TERM INT

# ── start FastAPI gateway ──────────────────────────────────────────────────
echo "[entrypoint] starting apollo-gateway (uvicorn)"
exec uvicorn apollo_gateway.main:app \
    --host 0.0.0.0 \
    --port 8080
