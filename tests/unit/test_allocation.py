# FILE: tests/unit/test_allocation.py
"""Unit tests for LUN and NSID allocation logic."""

import pytest

from apollo_gateway.spdk.ensure import allocate_lun, allocate_nsid


class TestAllocateLun:
    def test_empty_returns_zero(self):
        assert allocate_lun([]) == 0

    def test_sequential_returns_next(self):
        assert allocate_lun([0]) == 1
        assert allocate_lun([0, 1]) == 2
        assert allocate_lun([0, 1, 2]) == 3

    def test_fills_gap(self):
        assert allocate_lun([0, 2]) == 1
        assert allocate_lun([1, 2]) == 0
        assert allocate_lun([0, 1, 3]) == 2

    def test_single_element_not_zero(self):
        assert allocate_lun([1]) == 0

    def test_large_list_with_gap(self):
        used = list(range(10))
        used.remove(5)
        assert allocate_lun(used) == 5

    def test_unordered_input(self):
        assert allocate_lun([3, 1, 0]) == 2


class TestAllocateNsid:
    def test_empty_returns_one(self):
        assert allocate_nsid([]) == 1

    def test_sequential_returns_next(self):
        assert allocate_nsid([1]) == 2
        assert allocate_nsid([1, 2]) == 3
        assert allocate_nsid([1, 2, 3]) == 4

    def test_fills_gap(self):
        assert allocate_nsid([1, 3]) == 2
        assert allocate_nsid([2, 3]) == 1
        assert allocate_nsid([1, 2, 4]) == 3

    def test_never_returns_zero(self):
        # Even with 0 in the used list, min result is still 1
        assert allocate_nsid([0]) == 1

    def test_large_list_with_gap(self):
        used = list(range(1, 11))
        used.remove(6)
        assert allocate_nsid(used) == 6

    def test_unordered_input(self):
        assert allocate_nsid([3, 1, 2]) == 4
