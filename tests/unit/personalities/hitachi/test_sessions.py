# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for Hitachi session store."""

from __future__ import annotations

import time

from strix_gateway.personalities.hitachi.sessions import SessionStore


class TestSessionStore:
    def test_create_returns_session_info(self):
        store = SessionStore()
        info = store.create()
        assert info.session_id == 1
        assert len(info.token) == 32  # hex(16) = 32 chars

    def test_create_increments_session_id(self):
        store = SessionStore()
        s1 = store.create()
        s2 = store.create()
        assert s2.session_id == s1.session_id + 1

    def test_validate_with_valid_token(self):
        store = SessionStore()
        info = store.create()
        result = store.validate(info.token)
        assert result is not None
        assert result.session_id == info.session_id

    def test_validate_with_invalid_token(self):
        store = SessionStore()
        store.create()
        assert store.validate("bad-token") is None

    def test_validate_expired_token(self):
        store = SessionStore(ttl=0.01)
        info = store.create()
        time.sleep(0.02)
        assert store.validate(info.token) is None

    def test_delete_removes_session(self):
        store = SessionStore()
        info = store.create()
        assert store.delete(info.session_id) is True
        assert store.validate(info.token) is None

    def test_delete_nonexistent_returns_false(self):
        store = SessionStore()
        assert store.delete(999) is False

    def test_evict_on_create(self):
        store = SessionStore(ttl=0.01)
        s1 = store.create()
        time.sleep(0.02)
        _s2 = store.create()
        assert store.validate(s1.token) is None
