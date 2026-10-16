"""Hatchet-backed runs (mounted by grid/api.py as a router).

  POST /api/runs/backtest {days?, end?, reserve_pct?, cycles?} -> text/event-stream, each `data:` line one JSON event:
       {"kind":"run", run_id} | {"kind":"status", text} | {"kind":"backtest", n, of, sandbox_id, where, date, day}
       | {"kind":"backtest_done", days, totals} | {"kind":"done"} | {"kind":"error", text}
  POST /api/runs/nightly {date?, wait?} -> triggers nightly_plan now. wait=true (default) returns {run_id, plan} and
       registers the plan with the API so /api/approve works on it; wait=false returns {run_id} immediately.
  GET  /api/runs/plans -> plans stored by the worker (data/plans/*.json), newest first.

Hatchet client modules are imported lazily so the API still starts when Hatchet is down (clear error event instead).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from grid.config import DATA

log = logging.getLogger("grid.runs")
router = APIRouter()
PLANS_DIR = DATA / "plans"
REPLAY_GRACE_S = 1.5


class BacktestReq(BaseModel):
    days: int = 30
    end: str | None = None
    reserve_pct: float | None = None
    cycles: float | None = None


class NightlyReq(BaseModel):
    date: str | None = None
    wait: bool = True


def _meta() -> dict[str, str]:
    meta: dict[str, str] = {}
    try:
        from opentelemetry.propagate import inject

        inject(meta)  # traceparent: the worker's run is a child of this request (one tree in AgentGlow)
    except Exception:  # noqa: BLE001
        pass
    return meta


def _register(plan: dict) -> None:
    """Make a worker-made plan approvable through the existing /api/approve (agent.PLANS is per process)."""
    try:
        from grid import agent

        agent.PLANS.setdefault(plan["plan_id"], plan)
    except Exception as e:  # noqa: BLE001
        log.warning("plan not registered: %s", type(e).__name__)


def load_plans() -> list[dict]:
    out = []
    for p in sorted(PLANS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if PLANS_DIR.exists() else []:
        try:
            plan = json.loads(p.read_text())
            _register(plan)
            out.append(plan)
        except Exception:  # noqa: BLE001
            continue
    return out


async def _backtest_events(body: BacktestReq) -> AsyncIterator[dict]:
    try:
        from grid.workflows import BacktestInput, backtest, hatchet
    except Exception as e:  # noqa: BLE001
        yield {"kind": "error", "text": f"hatchet unavailable: {type(e).__name__}: {str(e)[:200]}"}
        yield {"kind": "done"}
        return
    seen: set[int] = set()
    q: asyncio.Queue = asyncio.Queue()
    finished = asyncio.Event()

    async def emit(ev: dict) -> None:
        seq = ev.get("seq")
        if finished.is_set() or (seq is not None and seq in seen):
            return
        if seq is not None:
            seen.add(seq)
        await q.put(ev)
        if ev.get("kind") == "done":
            finished.set()

    try:
        ref = await asyncio.wait_for(backtest.aio_run(BacktestInput(**body.model_dump()), wait_for_result=False,
                                                      additional_metadata=_meta()), 15)
    except Exception as e:  # noqa: BLE001
        yield {"kind": "error", "text": f"hatchet trigger failed: {type(e).__name__}: {str(e)[:200]}"}
        yield {"kind": "done"}
        return
    run_id = ref.workflow_run_id
    yield {"kind": "run", "run_id": run_id, "workflow": "backtest"}

    async def live():
        async for chunk in hatchet.runs.subscribe_to_stream(run_id):
            try:
                ev = json.loads(chunk.decode() if isinstance(chunk, bytes) else chunk)
            except (ValueError, AttributeError):
                continue
            if isinstance(ev, dict):
                await emit(ev)
            if finished.is_set():
                return

    async def replay():
        try:
            out = await ref.aio_result()
        except Exception as e:  # noqa: BLE001
            await emit({"kind": "error", "text": f"backtest run failed: {type(e).__name__}: {str(e)[:200]}"})
            await emit({"kind": "done"})
            return
        try:
            await asyncio.wait_for(finished.wait(), REPLAY_GRACE_S)
            return
        except TimeoutError:
            pass
        res = out.get("backtest", out) if isinstance(out, dict) else {}
        for ev in sorted(res.get("events") or [], key=lambda e: e.get("seq", 0)):
            await emit(dict(ev))
        await emit({"kind": "done"})

    tasks = [asyncio.create_task(live()), asyncio.create_task(replay())]
    try:
        while True:
            ev = await q.get()
            ev.pop("seq", None)
            yield ev
            if ev.get("kind") == "done":
                break
    finally:
        for t in tasks:
            t.cancel()


@router.post("/api/runs/backtest")
async def post_backtest(body: BacktestReq | None = None):
    body = body or BacktestReq()

    async def gen():
        async for ev in _backtest_events(body):
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/runs/nightly")
async def post_nightly(body: NightlyReq | None = None):
    body = body or NightlyReq()
    try:
        from grid.workflows import NightlyInput, nightly_plan
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"hatchet unavailable: {type(e).__name__}") from e
    ref = await nightly_plan.aio_run(NightlyInput(date=body.date, force=True), wait_for_result=False,
                                     additional_metadata={**_meta(), "manual": "1"})
    if not body.wait:
        return {"run_id": ref.workflow_run_id, "status": "queued"}
    out = await ref.aio_result()
    res = out.get("nightly_plan", out) if isinstance(out, dict) else {}
    plan = res.get("plan")
    if plan:
        _register(plan)
    return {"run_id": ref.workflow_run_id, "plan": plan, "skipped": res.get("skipped")}


@router.get("/api/runs/plans")
def get_plans(limit: int = 10):
    return {"plans": load_plans()[:limit]}
