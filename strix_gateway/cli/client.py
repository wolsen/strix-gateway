# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HTTP client wrapper for the Strix Gateway REST API.

All public methods return parsed JSON (dicts/lists).  Non-2xx responses
are translated into :class:`~strix_gateway.cli.errors.APIError`.
Name resolution helpers cache results per client instance (i.e. per CLI
invocation).
"""

from __future__ import annotations

from typing import Any

import httpx

from strix_gateway.cli.errors import APIError, ValidationError


class StrixClient:
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
    # Arrays
    # ------------------------------------------------------------------

    def list_arrays(self) -> list[dict]:
        return self.get("/v1/arrays")

    def get_array(self, name_or_id: str) -> dict:
        return self.get(f"/v1/arrays/{name_or_id}")

    def create_array(
        self,
        name: str,
        vendor: str = "generic",
        profile: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "vendor": vendor}
        if profile is not None:
            body["profile"] = profile
        return self.post("/v1/arrays", json=body)

    def delete_array(self, name_or_id: str, force: bool = False) -> None:
        params = {"force": "true"} if force else {}
        self.delete(f"/v1/arrays/{name_or_id}", params=params)

    def get_capabilities(self, name_or_id: str) -> dict:
        return self.get(f"/v1/arrays/{name_or_id}/capabilities")

    def update_array(self, name_or_id: str, **fields: Any) -> dict:
        """Try PATCH; fall back to PUT when the server returns 405."""
        try:
            return self.patch(f"/v1/arrays/{name_or_id}", json=fields)
        except APIError as exc:
            if exc.status_code in (404, 405):
                return self.put(f"/v1/arrays/{name_or_id}", json=fields)
            raise

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def list_endpoints(self, array: str) -> list[dict]:
        return self.get(f"/v1/arrays/{array}/endpoints")

    def create_endpoint(
        self,
        array: str,
        protocol: str,
        targets: dict | None = None,
        addresses: dict | None = None,
        auth: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"protocol": protocol}
        body["targets"] = targets if targets is not None else {}
        if addresses is not None:
            body["addresses"] = addresses
        if auth is not None:
            body["auth"] = auth
        return self.post(f"/v1/arrays/{array}/endpoints", json=body)

    def delete_endpoint(self, array: str, endpoint_id: str) -> None:
        self.delete(f"/v1/arrays/{array}/endpoints/{endpoint_id}")

    def update_endpoint(
        self,
        array: str,
        endpoint_id: str,
        targets: dict | None = None,
        addresses: dict | None = None,
        auth: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if targets is not None:
            body["targets"] = targets
        if addresses is not None:
            body["addresses"] = addresses
        if auth is not None:
            body["auth"] = auth
        return self.patch(f"/v1/arrays/{array}/endpoints/{endpoint_id}", json=body)

    # ------------------------------------------------------------------
    # Pools
    # ------------------------------------------------------------------

    def list_pools(self, array: str | None = None) -> list[dict]:
        params = {"array": array} if array else {}
        return self.get("/v1/pools", params=params)

    def create_pool(
        self,
        name: str,
        array: str,
        backend: str,
        size_gb: float,
        aio_path: str | None = None,
    ) -> dict:
        backend_type = "aio_file" if backend == "aio" else backend
        body: dict[str, Any] = {
            "name": name,
            "backend_type": backend_type,
            "size_mb": int(size_gb * 1024),
        }
        if aio_path:
            body["aio_path"] = aio_path
        if array == "default":
            return self.post("/v1/pools", json=body)

        # The v1 pool-create API always creates on the default array.
        # For named arrays, first repair any misplaced default-bound pool
        # left by an older apply run, otherwise create then attach.
        try:
            default_pool = self.resolve_pool(name, "default")
        except ValidationError:
            default_pool = self.post("/v1/pools", json=body)

        return self.post(
            f"/v1/arrays/{array}/pools/{default_pool['id']}",
        )

    def delete_pool(self, pool_id: str) -> None:
        self.delete(f"/v1/pools/{pool_id}")

    def resolve_pool(self, name: str, array: str) -> dict:
        """Resolve a pool by name within *array* (list + filter)."""
        pools = self.list_pools(array=array)
        for p in pools:
            if p["name"] == name:
                return p
        raise ValidationError(f"Pool '{name}' not found in array '{array}'")

    # ------------------------------------------------------------------
    # Volumes
    # ------------------------------------------------------------------

    def list_volumes(self, array: str | None = None) -> list[dict]:
        params = {"array": array} if array else {}
        return self.get("/v1/volumes", params=params)

    def get_volume(self, volume_id: str) -> dict:
        return self.get(f"/v1/volumes/{volume_id}")

    def create_volume(self, name: str, pool_id: str, size_gb: float) -> dict:
        return self.post(
            "/v1/volumes",
            json={
                "name": name,
                "pool_id": pool_id,
                "size_gb": size_gb,
            },
        )

    def delete_volume(self, volume_id: str) -> None:
        self.delete(f"/v1/volumes/{volume_id}")

    def extend_volume(self, volume_id: str, size_gb: float) -> dict:
        return self.post(
            f"/v1/volumes/{volume_id}/extend",
            json={"new_size_gb": size_gb},
        )

    def resolve_volume(self, name: str, array: str) -> dict:
        """Resolve a volume by name within *array* (list + filter)."""
        volumes = self.list_volumes(array=array)
        for v in volumes:
            if v["name"] == name:
                return v
        raise ValidationError(
            f"Volume '{name}' not found in array '{array}'"
        )

    # ------------------------------------------------------------------
    # Hosts
    # ------------------------------------------------------------------

    def list_hosts(self) -> list[dict]:
        return self.get("/v1/hosts")

    def create_host(
        self,
        name: str,
        iqns: list[str] | None = None,
        nqns: list[str] | None = None,
        wwpns: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name}
        if iqns:
            body["iqns"] = iqns
        if nqns:
            body["nqns"] = nqns
        if wwpns:
            body["wwpns"] = wwpns
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

    def get_host_attachments(self, host_id: str) -> dict:
        """GET /v1/hosts/{host_id}/attachments."""
        return self.get(f"/v1/hosts/{host_id}/attachments")

    # ------------------------------------------------------------------
    # Mappings
    # ------------------------------------------------------------------

    def list_mappings(self, array: str | None = None) -> list[dict]:
        params = {"array": array} if array else {}
        return self.get("/v1/mappings", params=params)

    def create_mapping(
        self,
        volume_id: str,
        host_id: str,
        *,
        persona_endpoint_id: str | None = None,
        underlay_endpoint_id: str | None = None,
        protocol: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "volume_id": volume_id,
            "host_id": host_id,
        }
        if persona_endpoint_id:
            body["persona_endpoint_id"] = persona_endpoint_id
        if underlay_endpoint_id:
            body["underlay_endpoint_id"] = underlay_endpoint_id
        if protocol:
            # Use as persona_protocol selector if no explicit endpoint IDs
            if not persona_endpoint_id:
                body["persona_protocol"] = protocol
            if not underlay_endpoint_id:
                body["underlay_protocol"] = protocol
        return self.post("/v1/mappings", json=body)

    def delete_mapping(self, mapping_id: str) -> None:
        self.delete(f"/v1/mappings/{mapping_id}")

    def resolve_mapping(
        self,
        host_name: str,
        volume_name: str,
        array: str,
    ) -> dict:
        """Find a mapping by host + volume names within *array*."""
        host = self.resolve_host(host_name)
        volume = self.resolve_volume(volume_name, array)
        mappings = self.list_mappings(array=array)
        for m in mappings:
            if m["host_id"] == host["id"] and m["volume_id"] == volume["id"]:
                return m
        raise ValidationError(
            f"No mapping found for host '{host_name}' → volume "
            f"'{volume_name}' in array '{array}'"
        )
