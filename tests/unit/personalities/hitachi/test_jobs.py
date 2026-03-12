# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for Hitachi job tracker."""

from __future__ import annotations

from strix_gateway.personalities.hitachi.jobs import JobState, JobTracker


class TestJobTracker:
    def test_submit_completed(self):
        tracker = JobTracker()
        job = tracker.submit_completed(affected_resources=["/ldevs/0"])
        assert job.job_id == 1
        assert job.state == JobState.completed
        assert job.completed_at is not None
        assert "/ldevs/0" in job.affected_resources

    def test_submit_failed(self):
        tracker = JobTracker()
        job = tracker.submit_failed("something broke")
        assert job.job_id == 1
        assert job.state == JobState.failed
        assert job.error_message == "something broke"

    def test_get_existing(self):
        tracker = JobTracker()
        job = tracker.submit_completed()
        retrieved = tracker.get(job.job_id)
        assert retrieved is not None
        assert retrieved.job_id == job.job_id

    def test_get_nonexistent(self):
        tracker = JobTracker()
        assert tracker.get(999) is None

    def test_sequential_ids(self):
        tracker = JobTracker()
        j1 = tracker.submit_completed()
        j2 = tracker.submit_completed()
        assert j2.job_id == j1.job_id + 1

    def test_bounded_history_eviction(self):
        tracker = JobTracker(max_history=5)
        jobs = [tracker.submit_completed() for _ in range(7)]
        # First two should be evicted
        assert tracker.get(jobs[0].job_id) is None
        assert tracker.get(jobs[1].job_id) is None
        # Last five should still exist
        for j in jobs[2:]:
            assert tracker.get(j.job_id) is not None
