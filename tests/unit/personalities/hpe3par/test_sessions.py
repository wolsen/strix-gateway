# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for HPE 3PAR WSAPI session store."""

from __future__ import annotations

import time

from strix_gateway.personalities.hpe3par.sessions import WsapiSessionStore


class TestWsapiSessionStore:
    def test_create_returns_session_info(self):
        store = WsapiSessionStore()
        info = store.create()
        assert len(info.key) == 24  # hex(12) = 24 chars

    def test_create_unique_keys(self):
        store = WsapiSessionStore()
        s1 = store.create()
        s2 = store.create()
        assert s1.key != s2.key

    def test_validate_valid_key(self):
        store = WsapiSessionStore()
        info = store.create()
        result = store.validate(info.key)
        assert result is not None
        assert result.key == info.key

    def test_validate_invalid_key_returns_none(self):
        store = WsapiSessionStore()
        store.create()
        assert store.validate("bad-key") is None

    def test_validate_expired_key(self):
        store = WsapiSessionStore(ttl=0.01)
        info = store.create()
        time.sleep(0.02)
        assert store.validate(info.key) is None

    def test_delete_existing_key(self):
        store = WsapiSessionStore()
        info = store.create()
        assert store.delete(info.key) is True
        assert store.validate(info.key) is None

    def test_delete_nonexistent_key(self):
        store = WsapiSessionStore()
        assert store.delete("nonexistent") is False

    def test_evict_on_create(self):
        store = WsapiSessionStore(ttl=0.01)
        s1 = store.create()
        time.sleep(0.02)
        _s2 = store.create()
        assert store.validate(s1.key) is None
