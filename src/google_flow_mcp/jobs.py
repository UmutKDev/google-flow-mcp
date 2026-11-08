"""Background jobs and the credit budget.

Most MCP clients give up on a tool call after about a minute, and a video takes
one to five. So long work runs as a job: the tool waits up to ``sync_wait_s``
and, if the job is still going, answers with its id; ``flow_wait`` or
``flow_job_status`` collects the result later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("google_flow_mcp.jobs")


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """A hard ceiling on credits this server may spend.

    The ledger is persisted next to the outputs so a restart of a long-lived
    container does not reset it. On Obot (ephemeral pods) the ceiling applies
    per pod lifetime, which is still the backstop against a runaway loop.
    """

    def __init__(self, ceiling: int, path: Optional[Path] = None):
        self.ceiling = max(0, int(ceiling))
        self.path = path
        self.spent = 0
        self.reserved = 0
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.spent = int(data.get("spent", 0))
            self.entries = list(data.get("entries", []))[-200:]
        except Exception:  # noqa: BLE001
            log.warning("could not read budget ledger %s", self.path)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"spent": self.spent, "entries": self.entries[-200:]}, indent=1))
        except Exception:  # noqa: BLE001
            log.warning("could not write budget ledger %s", self.path)

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self.spent - self.reserved)

    def reserve(self, cost: int, what: str) -> None:
        if cost > self.remaining:
            raise BudgetExceeded(
                f"{what} is estimated at {cost} credits but only {self.remaining} of the "
                f"{self.ceiling}-credit budget remain (FLOW_BUDGET_CREDITS).")
        self.reserved += cost

    def commit(self, cost: int, what: str, ref: Optional[str] = None) -> None:
        self.reserved = max(0, self.reserved - cost)
        self.spent += cost
        self.entries.append({"t": int(time.time()), "credits": cost, "what": what, "ref": ref})
        self._save()

    def release(self, cost: int) -> None:
        self.reserved = max(0, self.reserved - cost)

    def snapshot(self) -> dict:
        return {"ceiling": self.ceiling, "spent_estimate": self.spent, "reserved": self.reserved,
                "remaining": self.remaining, "recent": self.entries[-10:]}


@dataclass
class Job:
    id: str
    kind: str
    summary: str
    status: str = "running"          # running | succeeded | failed | cancelled
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    progress: str = "submitted"
    result: Any = None
    error: Optional[str] = None

    def public(self) -> dict:
        d = asdict(self)
        d["elapsed_s"] = round((self.finished or time.time()) - self.created, 1)
        return d


class JobManager:
    def __init__(self, max_jobs: int = 200):
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.max_jobs = max_jobs

    def start(self, kind: str, summary: str,
              work: Callable[[Job], Awaitable[Any]]) -> Job:
        job = Job(id=f"{kind}-{uuid.uuid4().hex[:10]}", kind=kind, summary=summary[:200])
        self._jobs[job.id] = job
        self._trim()

        async def runner() -> None:
            try:
                job.result = await work(job)
                job.status = "succeeded"
                job.progress = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.error = "cancelled by the user (Flow may still finish the clip on its side)"
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s failed", job.id)
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.finished = time.time()

        task = asyncio.create_task(runner())

        def settle(t: asyncio.Task) -> None:
            # A task cancelled before it first ran never enters runner().
            if job.status == "running" and t.cancelled():
                job.status = "cancelled"
                job.error = "cancelled before it started"
                job.finished = time.time()

        task.add_done_callback(settle)
        self._tasks[job.id] = task
        return job

    def _trim(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        done = sorted((j for j in self._jobs.values() if j.status != "running"), key=lambda j: j.created)
        for j in done[: len(self._jobs) - self.max_jobs]:
            self._jobs.pop(j.id, None)
            self._tasks.pop(j.id, None)

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise KeyError(f"unknown job id {job_id!r} (jobs live in memory and vanish on restart)") from None

    async def wait(self, job_id: str, timeout_s: float) -> Job:
        job = self.get(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout_s))
            except asyncio.TimeoutError:
                pass
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        return job

    def list(self, limit: int = 20) -> list[dict]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
        return [{k: v for k, v in j.public().items() if k != "result"} for j in jobs]

    @property
    def running(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status == "running")
