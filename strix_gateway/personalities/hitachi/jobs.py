# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""In-memory job tracker for Hitachi Configuration Manager async ops.

The Hitachi Configuration Manager REST API returns ``202 Accepted`` with
a ``Location`` header pointing to ``/ConfigurationManager/v1/objects/jobs/{jobId}``
for mutating operations.  The Cinder driver polls the job until it
completes.

Since Strix core operations are synchronous (they complete before the
handler returns), every submitted job is marked as completed immediately.
The tracker is a shim that satisfies the API contract.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("strix_gateway.personalities.hitachi.jobs")

_MAX_HISTORY = 1000


class JobState(str, Enum):
    completed = "Completed"
    failed = "Failed"
    in_progress = "InProgress"


@dataclass
class JobStatus:
    job_id: int
    state: JobState
    created_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    affected_resources: list[str] = field(default_factory=list)
    error_message: str | None = None


class JobTracker:
    """Bounded in-memory job history."""

    def __init__(self, max_history: int = _MAX_HISTORY) -> None:
        self._jobs: OrderedDict[int, JobStatus] = OrderedDict()
        self._next_id: int = 1
        self._max_history = max_history

    def submit_completed(
        self,
        affected_resources: list[str] | None = None,
    ) -> JobStatus:
        """Create an already-completed job (the common path for Strix)."""
        job_id = self._next_id
        self._next_id += 1
        now = time.monotonic()
        job = JobStatus(
            job_id=job_id,
            state=JobState.completed,
            created_at=now,
            completed_at=now,
            affected_resources=affected_resources or [],
        )
        self._store(job)
        return job

    def submit_failed(
        self,
        error_message: str,
        affected_resources: list[str] | None = None,
    ) -> JobStatus:
        """Create a failed job."""
        job_id = self._next_id
        self._next_id += 1
        now = time.monotonic()
        job = JobStatus(
            job_id=job_id,
            state=JobState.failed,
            created_at=now,
            completed_at=now,
            affected_resources=affected_resources or [],
            error_message=error_message,
        )
        self._store(job)
        return job

    def get(self, job_id: int) -> JobStatus | None:
        """Return job status or ``None`` if not found."""
        return self._jobs.get(job_id)

    def _store(self, job: JobStatus) -> None:
        self._jobs[job.job_id] = job
        while len(self._jobs) > self._max_history:
            self._jobs.popitem(last=False)
