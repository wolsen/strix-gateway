# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for HPE 3PAR error helpers."""

from __future__ import annotations

from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    NotFoundError,
    ValidationError,
)
from strix_gateway.personalities.hpe3par.errors import (
    Hpe3parAlreadyExistsError,
    Hpe3parError,
    Hpe3parInvalidArgError,
    Hpe3parNotFoundError,
    Hpe3parUnknownCommandError,
    core_to_3par,
)


class TestHpe3parErrors:
    def test_not_found_message(self):
        err = Hpe3parNotFoundError("volume 'vol1'")
        assert "does not exist" in str(err)
        assert "vol1" in str(err)

    def test_already_exists_message(self):
        err = Hpe3parAlreadyExistsError("volume 'vol1'")
        assert "already exists" in str(err)

    def test_invalid_arg_message(self):
        err = Hpe3parInvalidArgError("size must be positive")
        assert "size must be positive" in str(err)

    def test_unknown_command_message(self):
        err = Hpe3parUnknownCommandError("badcmd")
        assert "badcmd" in str(err)

    def test_exit_codes(self):
        assert Hpe3parError("x").exit_code == 1
        assert Hpe3parNotFoundError("x").exit_code == 1
        assert Hpe3parUnknownCommandError("x").exit_code == 127


class TestCoreToHpe3par:
    def test_not_found_maps(self):
        exc = NotFoundError("volume", "vol1")
        result = core_to_3par(exc)
        assert isinstance(result, Hpe3parNotFoundError)

    def test_already_exists_maps(self):
        exc = AlreadyExistsError("volume", "vol1")
        result = core_to_3par(exc)
        assert isinstance(result, Hpe3parAlreadyExistsError)

    def test_validation_maps(self):
        exc = ValidationError("bad input")
        result = core_to_3par(exc)
        assert isinstance(result, Hpe3parInvalidArgError)

    def test_unknown_core_error_maps_to_base(self):
        from strix_gateway.core.exceptions import CoreError
        exc = CoreError("oops")
        result = core_to_3par(exc)
        assert isinstance(result, Hpe3parError)
