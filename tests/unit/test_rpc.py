# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for SPDKClient and SPDKError."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from strix_gateway.spdk.rpc import SPDKClient, SPDKError


def _sock_mock(response: dict) -> MagicMock:
    """Return a context-manager-compatible socket mock that yields `response`."""
    payload = json.dumps(response).encode()
    sock = MagicMock()
    # Support `with socket.socket(...) as sock:`
    sock.__enter__ = lambda s: s
    sock.__exit__ = MagicMock(return_value=False)
    sock.recv.side_effect = [payload, b""]
    return sock


def _patch_socket(sock_mock):
    return patch("strix_gateway.spdk.rpc.socket.socket", return_value=sock_mock)


class TestSPDKError:
    def test_attributes_stored(self):
        err = SPDKError(code=-32602, message="invalid params")
        assert err.code == -32602
        assert err.message == "invalid params"

    def test_str_contains_code_and_message(self):
        err = SPDKError(code=-1, message="boom")
        assert "-1" in str(err)
        assert "boom" in str(err)


class TestSPDKClientCall:
    def test_successful_call_no_params(self):
        response = {"jsonrpc": "2.0", "id": 1, "result": [{"name": "bdev0"}]}
        sock = _sock_mock(response)
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            result = client.call("bdev_get_bdevs")
        assert result == [{"name": "bdev0"}]
        sock.connect.assert_called_once_with("/tmp/spdk.sock")

    def test_successful_call_with_params(self):
        response = {"jsonrpc": "2.0", "id": 1, "result": True}
        sock = _sock_mock(response)
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            result = client.call("bdev_malloc_create", {"name": "m0", "num_blocks": 1024, "block_size": 512})
        assert result is True
        sent = json.loads(sock.sendall.call_args[0][0])
        assert sent["params"] == {"name": "m0", "num_blocks": 1024, "block_size": 512}
        assert sent["method"] == "bdev_malloc_create"

    def test_params_none_omitted_from_payload(self):
        response = {"jsonrpc": "2.0", "id": 1, "result": None}
        sock = _sock_mock(response)
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            client.call("test_method")
        sent = json.loads(sock.sendall.call_args[0][0])
        assert "params" not in sent

    def test_error_response_raises_spdk_error(self):
        response = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad param"}}
        sock = _sock_mock(response)
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            with pytest.raises(SPDKError) as exc_info:
                client.call("some_method")
        assert exc_info.value.code == -32602
        assert exc_info.value.message == "bad param"

    def test_error_response_missing_fields_uses_defaults(self):
        response = {"jsonrpc": "2.0", "id": 1, "error": {}}
        sock = _sock_mock(response)
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            with pytest.raises(SPDKError) as exc_info:
                client.call("some_method")
        assert exc_info.value.code == -1
        assert exc_info.value.message == "unknown error"

    def test_chunked_response_assembled(self):
        """Response split across multiple recv() calls is reassembled."""
        full = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"}).encode()
        mid = len(full) // 2
        sock = MagicMock()
        sock.__enter__ = lambda s: s
        sock.__exit__ = MagicMock(return_value=False)
        sock.recv.side_effect = [full[:mid], full[mid:], b""]
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            result = client.call("method")
        assert result == "ok"

    def test_id_increments_per_call(self):
        def make_response(call_id):
            return json.dumps({"jsonrpc": "2.0", "id": call_id, "result": None}).encode()

        sock1 = MagicMock()
        sock1.__enter__ = lambda s: s
        sock1.__exit__ = MagicMock(return_value=False)

        sock2 = MagicMock()
        sock2.__enter__ = lambda s: s
        sock2.__exit__ = MagicMock(return_value=False)

        client = SPDKClient("/tmp/spdk.sock")

        with patch("strix_gateway.spdk.rpc.socket.socket", side_effect=[sock1, sock2]):
            sock1.recv.side_effect = [make_response(1), b""]
            client.call("first")
            sock2.recv.side_effect = [make_response(2), b""]
            client.call("second")

        first_payload = json.loads(sock1.sendall.call_args[0][0])
        second_payload = json.loads(sock2.sendall.call_args[0][0])
        assert first_payload["id"] == 1
        assert second_payload["id"] == 2

    def test_socket_closes_before_full_response_raises(self):
        """If recv returns b'' before valid JSON is received, raise SPDKError."""
        sock = MagicMock()
        sock.__enter__ = lambda s: s
        sock.__exit__ = MagicMock(return_value=False)
        sock.recv.return_value = b""  # immediate close
        with _patch_socket(sock):
            client = SPDKClient("/tmp/spdk.sock")
            with pytest.raises(SPDKError, match="socket closed"):
                client.call("method")
