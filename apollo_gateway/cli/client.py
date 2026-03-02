# FILE: apollo_gateway/cli/client.py
"""HTTP client wrapper for the Apollo Gateway REST API.

All public methods return parsed JSON (dicts/lists).  Non-2xx responses
are translated into :class:`~apollo_gateway.cli.errors.APIError`.
Name resolution helpers cache results per client instance (i.e. per CLI
invocation).
"""

from __future__ import annotations

from typing import Any

import httpx

from apollo_gateway.cli.errors import APIError, ValidationError


class ApolloClient:
    """Thin synchronous httpx wrapper with name-resolution helpers."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        # Per-invocation caches ------------------------------------------
        self._host_cache: dict[str, dict] = {}

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise APIError(resp.status_code, str(detail))
        if resp.status_code == 204:
            return None
        return resp.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self._request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self._request("POST", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self._request("DELETE", path, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self._request("PATCH", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self._request("PUT", path, **kw)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> dict:
        return self.get("/healthz")

    # ------------------------------------------------------------------
    # Subsystems
    # ------------------------------------------------------------------

    def list_subsystems(self) -> list[dict]:
        return self.get("/v1/subsystems")

    def get_subsystem(self, name_or_id: str) -> dict:
        return self.get(f"/v1/subsystems/{name_or_id}")

    def create_subsystem(
        self,
        name: str,
        persona: str = "generic",
        protocols: list[str] | None = None,
        capability_profile: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "persona": persona}
        if protocols is not None:
            body["protocols_enabled"] = protocols
        if capability_profile is not None:
            body["capability_profile"] = capability_profile
        return self.post("/v1/subsystems", json=body)

    def delete_subsystem(self, name_or_id: str, force: bool = False) -> None:
        params = {"force": "true"} if force else {}
        self.delete(f"/v1/subsystems/{name_or_id}", params=params)

    def get_capabilities(self, name_or_id: str) -> dict:
        return self.get(f"/v1/subsystems/{name_or_id}/capabilities")

    def update_subsystem(self, name_or_id: str, **fields: Any) -> dict:
        """Try PATCH; fall back to PUT when the server returns 405."""
        try:
            return self.patch(f"/v1/subsystems/{name_or_id}", json=fields)
        except APIError as exc:
            if exc.status_code in (404, 405):
                return self.put(f"/v1/subsystems/{name_or_id}", json=fields)
            raise

    # ------------------------------------------------------------------
    # Pools
    # ------------------------------------------------------------------

    def list_pools(self, subsystem: str | None = None) -> list[dict]:
        params = {"subsystem": subsystem} if subsystem else {}
        return self.get("/v1/pools", params=params)

    def create_pool(
        self,
        name: str,
        subsystem: str,
        backend: str,
        size_gb: float,
        aio_path: str | None = None,
    ) -> dict:
        backend_type = "aio_file" if backend == "aio" else backend
        body: dict[str, Any] = {
            "name": name,
            "backend_type": backend_type,
            "subsystem": subsystem,
            "size_mb": int(size_gb * 1024),
        }
        if aio_path:
            body["aio_path"] = aio_path
        return self.post("/v1/pools", json=body)

    def delete_pool(self, pool_id: str) -> None:
        self.delete(f"/v1/pools/{pool_id}")

    def resolve_pool(self, name: str, subsystem: str) -> dict:
        """Resolve a pool by name within *subsystem* (list + filter)."""
        pools = self.list_pools(subsystem=subsystem)
        for p in pools:
            if p["name"] == name:
                return p
        raise ValidationError(f"Pool '{name}' not found in subsystem '{subsystem}'")

    # ------------------------------------------------------------------
    # Volumes
    # ------------------------------------------------------------------

    def list_volumes(self, subsystem: str | None = None) -> list[dict]:
        params = {"subsystem": subsystem} if subsystem else {}
        return self.get("/v1/volumes", params=params)

    def get_volume(self, volume_id: str) -> dict:
        return self.get(f"/v1/volumes/{volume_id}")

    def create_volume(self, name: str, pool_id: str, size_gb: float) -> dict:
        return self.post(
            "/v1/volumes",
            json={
                "name": name,
                "pool_id": pool_id,
                "size_mb": int(size_gb * 1024),
            },
        )

    def delete_volume(self, volume_id: str) -> None:
        self.delete(f"/v1/volumes/{volume_id}")

    def extend_volume(self, volume_id: str, size_gb: float) -> dict:
        return self.post(
            f"/v1/volumes/{volume_id}/extend",
            json={"new_size_mb": int(size_gb * 1024)},
        )

    def resolve_volume(self, name: str, subsystem: str) -> dict:
        """Resolve a volume by name within *subsystem* (list + filter)."""
        volumes = self.list_volumes(subsystem=subsystem)
        for v in volumes:
            if v["name"] == name:
                return v
        raise ValidationError(
            f"Volume '{name}' not found in subsystem '{subsystem}'"
        )

    # ------------------------------------------------------------------
    # Hosts
    # ------------------------------------------------------------------

    def list_hosts(self) -> list[dict]:
        return self.get("/v1/hosts")

    def create_host(
        self,
        name: str,
        iqn: str | None = None,
        nqn: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name}
        if iqn:
            body["iqn"] = iqn
        if nqn:
            body["nqn"] = nqn
        return self.post("/v1/hosts", json=body)

    def get_host(self, host_id: str) -> dict:
        return self.get(f"/v1/hosts/{host_id}")

    def delete_host(self, host_id: str) -> None:
        self.delete(f"/v1/hosts/{host_id}")

    def update_host(self, host_id: str, **fields: Any) -> dict:
        return self.patch(f"/v1/hosts/{host_id}", json=fields)

    def resolve_host(self, name: str) -> dict:
        """Resolve a host by name (list + filter, with caching)."""
        if name in self._host_cache:
            return self._host_cache[name]
        hosts = self.list_hosts()
        for h in hosts:
            self._host_cache[h["name"]] = h
            if h["name"] == name:
                return h
        raise ValidationError(f"Host '{name}' not found")

    # ------------------------------------------------------------------
    # Mappings
    # ------------------------------------------------------------------

    def list_mappings(self, subsystem: str | None = None) -> list[dict]:
        params = {"subsystem": subsystem} if subsystem else {}
        return self.get("/v1/mappings", params=params)

    def create_mapping(
        self,
        volume_id: str,
        host_id: str,
        protocol: str,
    ) -> dict:
        return self.post(
            "/v1/mappings",
            json={
                "volume_id": volume_id,
                "host_id": host_id,
                "protocol": protocol,
            },
        )

    def delete_mapping(self, mapping_id: str) -> None:
        self.delete(f"/v1/mappings/{mapping_id}")

    def get_connection_info(self, mapping_id: str) -> dict:
        return self.get(f"/v1/mappings/{mapping_id}/connection-info")

    # ------------------------------------------------------------------
    # SVC façade
    # ------------------------------------------------------------------

    def svc_run(self, subsystem: str, command: str) -> dict:
        """POST /v1/svc/run and return the raw response dict."""
        return self.post("/v1/svc/run", json={"subsystem": subsystem, "command": command})

    def resolve_mapping(
        self,
        host_name: str,
        volume_name: str,
        subsystem: str,
    ) -> dict:
        """Find a mapping by host + volume names within *subsystem*."""
        host = self.resolve_host(host_name)
        volume = self.resolve_volume(volume_name, subsystem)
        mappings = self.list_mappings(subsystem=subsystem)
        for m in mappings:
            if m["host_id"] == host["id"] and m["volume_id"] == volume["id"]:
                return m
        raise ValidationError(
            f"No mapping found for host '{host_name}' → volume "
            f"'{volume_name}' in subsystem '{subsystem}'"
        )
