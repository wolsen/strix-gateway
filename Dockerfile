# FILE: Dockerfile
FROM python:3.11-slim

# ── System packages ────────────────────────────────────────────────────────
# openssh-server: sshd for the IBM SVC SSH façade (port 22)
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependency installer ────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --system

# ── Application code ───────────────────────────────────────────────────────
COPY apollo_gateway/ ./apollo_gateway/

# ── IBM SVC SSH façade dispatcher ──────────────────────────────────────────
COPY scripts/apollo-svc-shell /usr/local/bin/apollo-svc-shell
RUN chmod +x /usr/local/bin/apollo-svc-shell

# ── sshd setup ─────────────────────────────────────────────────────────────
# 1. Runtime directories
RUN mkdir -p /var/run/sshd \
    && mkdir -p /var/log/apollo \
    && chmod 755 /var/log/apollo

# 2. Deploy minimal sshd_config (ForceCommand for svc user inside)
COPY docker/sshd_config /etc/ssh/sshd_config

# 3. Pre-generate SSH host keys so the image is self-contained.
#    In production, mount /etc/ssh as a volume to keep stable host keys
#    across container restarts (prevents "host key changed" warnings).
RUN ssh-keygen -A

# 4. Create the "svc" system user that Cinder drivers authenticate as.
#    No shell, no home directory contents — ForceCommand handles everything.
RUN useradd \
        --system \
        --shell /usr/sbin/nologin \
        --home-dir /home/svc \
        --create-home \
        svc \
    && mkdir -p /home/svc/.ssh \
    && chown -R svc:svc /home/svc/.ssh \
    && chmod 700 /home/svc/.ssh

# ── Container entrypoint ───────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080 22

ENTRYPOINT ["/entrypoint.sh"]
