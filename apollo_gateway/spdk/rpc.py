# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Synchronous JSON-RPC client over a SPDK Unix socket.

All SPDK interaction is funnelled through SPDKClient.call().  Callers in async
contexts should wrap calls with ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Any, Optional

logger = logging.getLogger("apollo_gateway.spdk.rpc")


class SPDKError(Exception):
    """Raised when SPDK returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"SPDK RPC error {code}: {message}")


class SPDKClient:
    """Thread-safe synchronous JSON-RPC client for the SPDK Unix socket."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._id_counter = 0
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id_counter += 1
            return self._id_counter

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Execute a JSON-RPC method and return the ``result`` field.

        Raises:
            SPDKError: if the response contains an ``error`` field.
            ConnectionRefusedError / FileNotFoundError: if the socket is unavailable.
        """
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        raw = json.dumps(payload).encode()
        logger.debug("SPDK -> %s params=%s", method, params)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(self.socket_path)
            sock.sendall(raw)

            buf = b""
            data = None
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                try:
                    data = json.loads(buf)
                    break
                except json.JSONDecodeError:
                    continue

        if data is None:
            raise SPDKError(-1, "socket closed before complete response received")

        logger.debug("SPDK <- %s", data)

        if "error" in data:
            err = data["error"]
            raise SPDKError(
                code=err.get("code", -1),
                message=err.get("message", "unknown error"),
            )

        return data.get("result")
