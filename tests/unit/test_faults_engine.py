# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for the fault/delay injection engine."""

from __future__ import annotations

import pytest

import strix_gateway.core.faults as engine
from strix_gateway.core.faults import FaultInjectionError


def _reset():
    """Clear all registered faults and delays."""
    engine._faults.clear()
    engine._delays.clear()


class TestFaultRegistry:
    def setup_method(self):
        _reset()

    def test_inject_and_check_raises(self):
        engine.inject_fault("create_volume", "simulated failure")
        with pytest.raises(FaultInjectionError, match="simulated failure"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(engine.check_fault("create_volume"))

    def test_clear_fault_stops_raising(self):
        engine.inject_fault("delete_volume", "boom")
        engine.clear_fault("delete_volume")
        # Should not raise
        import asyncio
        asyncio.get_event_loop().run_until_complete(engine.check_fault("delete_volume"))

    def test_clear_fault_nonexistent_is_noop(self):
        engine.clear_fault("does_not_exist")  # should not raise

    def test_list_faults_returns_copy(self):
        engine.inject_fault("op1", "msg1")
        engine.inject_fault("op2", "msg2")
        faults = engine.list_faults()
        assert faults == {"op1": "msg1", "op2": "msg2"}


class TestDelayRegistry:
    def setup_method(self):
        _reset()

    def test_inject_delay_registered(self):
        engine.inject_delay("create_pool", 0.0)
        assert "create_pool" in engine._delays

    def test_clear_delay_removes(self):
        engine.inject_delay("create_pool", 0.0)
        engine.clear_delay("create_pool")
        assert "create_pool" not in engine._delays

    def test_clear_delay_nonexistent_is_noop(self):
        engine.clear_delay("not_there")

    def test_list_delays_returns_copy(self):
        engine.inject_delay("op_a", 0.5)
        result = engine.list_delays()
        assert result == {"op_a": 0.5}


class TestCheckFault:
    def setup_method(self):
        _reset()

    async def test_no_fault_no_delay_passes(self):
        await engine.check_fault("noop")  # should not raise

    async def test_delay_only_does_not_raise(self):
        engine.inject_delay("slow_op", 0.0)  # zero delay for test speed
        await engine.check_fault("slow_op")  # should not raise

    async def test_delay_then_fault_raises(self):
        engine.inject_delay("bad_op", 0.0)
        engine.inject_fault("bad_op", "delayed fault")
        with pytest.raises(FaultInjectionError, match="delayed fault"):
            await engine.check_fault("bad_op")
