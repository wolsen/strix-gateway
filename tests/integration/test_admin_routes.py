# FILE: tests/integration/test_admin_routes.py
"""Integration tests for /admin/* routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import apollo_gateway.core.faults as fault_engine

pytestmark = pytest.mark.asyncio


def _reset_faults():
    fault_engine._faults.clear()
    fault_engine._delays.clear()


class TestFaultRoutes:
    def setup_method(self):
        _reset_faults()

    async def test_inject_fault(self, client: AsyncClient):
        r = await client.post("/admin/faults", json={"operation": "op1", "error_message": "boom"})
        assert r.status_code == 201
        assert r.json()["operation"] == "op1"

    async def test_list_faults(self, client: AsyncClient):
        fault_engine.inject_fault("op2", "msg2")
        r = await client.get("/admin/faults")
        assert r.status_code == 200
        assert r.json() == {"op2": "msg2"}

    async def test_clear_fault(self, client: AsyncClient):
        fault_engine.inject_fault("op3", "msg3")
        r = await client.delete("/admin/faults/op3")
        assert r.status_code == 204
        assert "op3" not in fault_engine._faults

    async def test_list_faults_empty(self, client: AsyncClient):
        r = await client.get("/admin/faults")
        assert r.status_code == 200
        assert r.json() == {}


class TestDelayRoutes:
    def setup_method(self):
        _reset_faults()

    async def test_inject_delay(self, client: AsyncClient):
        r = await client.post("/admin/delays", json={"operation": "slow_op", "delay_seconds": 0.5})
        assert r.status_code == 201
        assert r.json()["delay_seconds"] == 0.5

    async def test_list_delays(self, client: AsyncClient):
        fault_engine.inject_delay("op_x", 1.0)
        r = await client.get("/admin/delays")
        assert r.status_code == 200
        assert r.json() == {"op_x": 1.0}

    async def test_clear_delay(self, client: AsyncClient):
        fault_engine.inject_delay("op_y", 2.0)
        r = await client.delete("/admin/delays/op_y")
        assert r.status_code == 204
        assert "op_y" not in fault_engine._delays
