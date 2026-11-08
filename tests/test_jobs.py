import asyncio

import pytest

from google_flow_mcp.jobs import Budget, BudgetExceeded, JobManager


def test_budget(tmp_path):
    b = Budget(50, tmp_path / "l.json")
    b.reserve(30, "a")
    assert b.remaining == 20
    with pytest.raises(BudgetExceeded):
        b.reserve(30, "b")
    b.commit(30, "a", "op")
    assert b.spent == 30 and b.remaining == 20
    again = Budget(50, tmp_path / "l.json")
    assert again.spent == 30


def test_zero_budget_blocks_everything():
    with pytest.raises(BudgetExceeded):
        Budget(0).reserve(1, "video")


async def test_jobs_lifecycle():
    jm = JobManager()

    async def ok(job):
        job.progress = "half"
        await asyncio.sleep(0.05)
        return {"x": 1}

    async def boom(job):
        raise RuntimeError("nope")

    a = jm.start("t", "ok", ok)
    b = jm.start("t", "boom", boom)
    await jm.wait(a.id, 0)  # returns immediately while running
    assert a.status == "running"
    await jm.wait(a.id, 2)
    await jm.wait(b.id, 2)
    assert a.status == "succeeded" and a.result == {"x": 1}
    assert b.status == "failed" and "nope" in b.error
    c = jm.start("t", "slow", lambda job: asyncio.sleep(10))
    jm.cancel(c.id)
    await asyncio.sleep(0.05)
    assert c.status == "cancelled"
    assert len(jm.list()) == 3
