"""FastAPI :8520. Run: uv run uvicorn grid.api:app --port 8520"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from grid import agent, optimize, prices
from grid.config import AGENTGLOW_URL, has, setup_tracing, tracing_status
from grid.sponsors import anchor, arize, daytona_sim, memory, voice

log = logging.getLogger("grid.api")
_backtest_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # warm the Daytona sandbox in the background so the first plan does not pay for creation
    threading.Thread(target=daytona_sim.sandbox_id, daemon=True).start()
    yield
    daytona_sim.shutdown()


app = FastAPI(title="grid dispatcher", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5520", "http://127.0.0.1:5520"],
                   allow_methods=["*"], allow_headers=["*"])
setup_tracing(app)


class PlanReq(BaseModel):
    date: str | None = None
    cap_kw: float | None = None
    prompt_version: str | None = None
    speak: bool = True


class ApproveReq(BaseModel):
    plan_id: str
    approved: bool
    who: str | None = "dispatcher"


class ConstraintReq(BaseModel):
    bus_id: int
    departure_hour: float | None = None
    arrival_hour: float | None = None


@app.get("/api/health")
def health():
    tr = tracing_status()
    return {
        "ok": True,
        "llm": agent.llm_status(),
        "sponsors": {
            "daytona": daytona_sim.status(),
            "memory": memory.status(),
            "arize": arize.status(),
            "elevenlabs": voice.status(),
            "anchor": anchor.status(),
            "agentglow": {"enabled": tr["agentglow"], "url": AGENTGLOW_URL, "scene": f"{AGENTGLOW_URL}/backend"},
        },
        "keys": {k: has(k) for k in ("DAYTONA_API_KEY", "MISTRAL_API_KEY", "ANTHROPIC_API_KEY", "ARIZE_API_KEY",
                                     "ELEVENLABS_API_KEY", "SURREAL_URL", "ANCHOR_API_KEY", "DISPATCH_PHONE")},
    }


@app.get("/api/prices")
def get_prices(date: str | None = None):
    d = date or __import__("datetime").date.today().isoformat()
    da = prices.night(d, "da")
    rt = prices.night(d, "rt")
    if not rt:  # tonight has not happened yet: show last night's real-time for context
        prev = (__import__("datetime").date.fromisoformat(d) - timedelta(days=1)).isoformat()
        rt_prev = prices.night(prev, "rt")
    else:
        rt_prev = []
    return {"date": d, "zone": "N.Y.C.", "source": "NYISO public LBMP CSV (mis.nyiso.com)",
            "day_ahead": da, "real_time": rt, "real_time_last_night": rt_prev,
            "proxy": any(r.get("proxy") for r in da)}


@app.post("/api/plan")
async def post_plan(req: PlanReq | None = None):
    req = req or PlanReq()
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def work():
        try:
            for ev in agent.run_plan(req.date, req.cap_kw, req.prompt_version or agent.DEFAULT_PROMPT, req.speak):
                loop.call_soon_threadsafe(q.put_nowait, ev)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    loop.run_in_executor(None, work)

    async def stream():
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: str):
    p = agent.PLANS.get(plan_id)
    if not p:
        raise HTTPException(404, "unknown plan")
    return p


@app.get("/api/backtest")
def get_backtest(days: int = 30, mode: str = "rt"):
    key = (days, mode, date.today().isoformat())
    if key not in _backtest_cache:
        _backtest_cache[key] = optimize.backtest(days=days, mode=mode)
    return _backtest_cache[key]


@app.post("/api/approve")
def post_approve(req: ApproveReq):
    try:
        return agent.approve(req.plan_id, req.approved, req.who or "dispatcher")
    except KeyError:
        raise HTTPException(404, "unknown plan")


@app.get("/api/voice/{plan_id}")
def get_voice(plan_id: str):
    p = voice.path_for(plan_id)
    if not p.exists():
        plan = agent.PLANS.get(plan_id)
        if not voice.enabled():
            raise HTTPException(503, "ElevenLabs disabled (no ELEVENLABS_API_KEY)")
        if not plan or not voice.synthesize(plan_id, plan["message"]):
            raise HTTPException(404, "no audio for plan")
    return FileResponse(p, media_type="audio/mpeg")


@app.post("/api/constraints")
def post_constraint(req: ConstraintReq):
    return agent.add_constraint(req.bus_id, req.departure_hour, req.arrival_hour)


@app.get("/api/memory")
def get_memory(kind: str = "plan", key: str | None = None, history: bool = True):
    return {"backend": memory.backend(), "facts": memory.recall(kind, key, history)}
