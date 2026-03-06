# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for vhost FQDN derivation, DNS name validation, and VhostRegistry."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from apollo_gateway.core.db import Array, init_db, get_session_factory
from apollo_gateway.core.models import ArrayCreate
from apollo_gateway.tls.vhost import (
    VhostRegistry,
    is_dns_safe,
    resolve_hostname,
    resolve_array_fqdn,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# FQDN derivation
# ---------------------------------------------------------------------------


class TestResolveFqdn:
    def test_default_hostname(self):
        with patch("apollo_gateway.tls.vhost.socket.gethostname", return_value="gw01"):
            fqdn = resolve_array_fqdn("pure-a", "lab.example")
        assert fqdn == "pure-a.gw01.lab.example"

    def test_hostname_with_domain_stripped(self):
        with patch(
            "apollo_gateway.tls.vhost.socket.gethostname",
            return_value="gw01.internal.corp",
        ):
            fqdn = resolve_array_fqdn("svc-test", "storage.example.com")
        assert fqdn == "svc-test.gw01.storage.example.com"

    def test_hostname_override(self):
        fqdn = resolve_array_fqdn(
            "pure-a", "lab.example", hostname_override="node99"
        )
        assert fqdn == "pure-a.node99.lab.example"

    def test_resolve_hostname_default(self):
        with patch("apollo_gateway.tls.vhost.socket.gethostname", return_value="myhost"):
            assert resolve_hostname() == "myhost"

    def test_resolve_hostname_override(self):
        assert resolve_hostname("custom") == "custom"


# ---------------------------------------------------------------------------
# DNS name validation
# ---------------------------------------------------------------------------


class TestDnsSafety:
    @pytest.mark.parametrize(
        "name",
        ["default", "pure-a", "svc-test", "a", "abc123", "a-b-c"],
    )
    def test_valid_names(self, name):
        assert is_dns_safe(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Pure-A",       # uppercase
            "1abc",         # starts with digit
            "-abc",         # starts with hyphen
            "abc_def",      # underscore
            "abc.def",      # dot
            "a" * 64,       # too long (64 chars)
            "",             # empty
        ],
    )
    def test_invalid_names(self, name):
        assert not is_dns_safe(name)


class TestArrayNameValidator:
    def test_valid_name_passes(self):
        s = ArrayCreate(name="my-array")
        assert s.name == "my-array"

    def test_invalid_name_rejected(self):
        with pytest.raises(Exception):
            ArrayCreate(name="My_Array!")

    def test_starts_with_digit_rejected(self):
        with pytest.raises(Exception):
            ArrayCreate(name="1bad")


# ---------------------------------------------------------------------------
# VhostRegistry
# ---------------------------------------------------------------------------


class TestVhostRegistry:
    @pytest_asyncio.fixture(autouse=True)
    async def setup_db(self):
        await init_db(TEST_DATABASE_URL)
        factory = get_session_factory()
        async with factory() as session:
            session.add(Array(name="default", vendor="generic"))
            session.add(Array(name="pure-a", vendor="pure"))
            session.add(Array(name="svc-test", vendor="ibm_svc"))
            await session.commit()
        yield

    async def test_rebuild_creates_mappings(self):
        registry = VhostRegistry("lab.example", hostname_override="gw01")
        await registry.rebuild(get_session_factory())
        mappings = registry.all_mappings()
        assert len(mappings) == 3
        assert "default.gw01.lab.example" in mappings
        assert "pure-a.gw01.lab.example" in mappings
        assert "svc-test.gw01.lab.example" in mappings

    async def test_lookup_hit(self):
        registry = VhostRegistry("lab.example", hostname_override="gw01")
        await registry.rebuild(get_session_factory())
        info = registry.lookup("pure-a.gw01.lab.example")
        assert info is not None
        assert info.name == "pure-a"

    async def test_lookup_miss(self):
        registry = VhostRegistry("lab.example", hostname_override="gw01")
        await registry.rebuild(get_session_factory())
        assert registry.lookup("unknown.gw01.lab.example") is None

    async def test_lookup_case_insensitive(self):
        registry = VhostRegistry("lab.example", hostname_override="gw01")
        await registry.rebuild(get_session_factory())
        info = registry.lookup("Pure-A.GW01.Lab.Example")
        assert info is not None
        assert info.name == "pure-a"

    async def test_fqdn_for_name(self):
        registry = VhostRegistry("lab.example", hostname_override="gw01")
        assert registry.fqdn_for_name("new-arr") == "new-arr.gw01.lab.example"
