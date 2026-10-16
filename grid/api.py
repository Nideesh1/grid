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
    threading.Thread(target=voice.ensure_agent, daemon=True).start()
    threading.Thread(target=anchor.cleanup_stale, daemon=True).start()
    yield
    daytona_sim.shutdown()


app = FastAPI(title="grid dispatcher", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5520", "http://127.0.0.1:5520"],
                   allow_methods=["*"], allow_headers=["*"])
setup_tracing(app)


class PlanReq(BaseModel):
    date: str | None = None
    note: str | None = None
    fast: bool = False
    prompt_version: str | None = None
    speak: bool = True


class ApproveReq(BaseModel):
    plan_id: str
    approved: bool
    who: str | None = "dispatcher"


class ReplanReq(BaseModel):
    constraint: str | None = None
    date: str | None = None


class ConstraintReq(BaseModel):
    text: str


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
            "elevenlabs": {**voice.status(), **voice.agent_status()},
            "anchor": anchor.status(),
            "agentglow": {"enabled": tr["agentglow"], "url": AGENTGLOW_URL, "scene": f"{AGENTGLOW_URL}/backend"},
        },
        "keys": {k: has(k) for k in ("DAYTONA_API_KEY", "AWS_ACCESS_KEY_ID", "MISTRAL_API_KEY", "ANTHROPIC_API_KEY", "ARIZE_API_KEY",
                                     "ELEVENLABS_API_KEY", "SURREAL_URL", "ANCHOR_API_KEY", "DISPATCH_PHONE")},
    }


@app.get("/api/prices")
def get_prices(date: str | None = None):
    """Hourly NYISO N.Y.C. day-ahead for the trading day (default tomorrow, else today) + today's real-time so far."""
    import datetime as _dt

    d = _dt.date.fromisoformat(date) if date else _dt.date.today() + timedelta(days=1)
    da = prices.day_ahead(d)
    if len(da) < 20 and not date:
        d = _dt.date.today()
        da = prices.day_ahead(d)
    return {"date": d.isoformat(), "zone": "N.Y.C.", "source": "NYISO public LBMP CSV (mis.nyiso.com)",
            "day_ahead": optimize.hourly_prices(da) if da else [],
            "real_time_today": optimize.hourly_prices(prices.real_time(_dt.date.today()) or [])}


_zones_cache: dict = {"t": 0.0, "v": None}


@app.get("/api/prices/zones")
def get_price_zones():
    """Latest real-time 5-min LBMP for all 11 NYISO zones (today's realtime_zone CSV), cached 60s."""
    import csv
    import io
    import time as _t
    from datetime import date as _d

    if _zones_cache["v"] and _t.time() - _zones_cache["t"] < 60:
        return _zones_cache["v"]
    text = prices._fetch_csv("rt", _d.today()) or ""
    latest: dict = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = row.get("Name")
        if name:
            latest[name] = {"zone": name, "ptid": row.get("PTID"), "ts": row.get("Time Stamp"),
                            "usd_mwh": float(row.get("LBMP ($/MWHr)") or 0)}
    v = {"source": "NYISO real-time zonal LBMP (mis.nyiso.com)", "zones": list(latest.values()),
         "ts": next(iter(latest.values()))["ts"] if latest else None}
    _zones_cache.update(t=_t.time(), v=v)
    return v


@app.post("/api/plan")
async def post_plan(req: PlanReq | None = None, fast: bool = False):
    """SSE. Default: the deepagents trading desk (market-analyst, trader, risk-officer) with live status lines, then a
    plan event. ?fast=1 (or desk error / >45s): the direct optimizer path."""
    req = req or PlanReq()

    async def stream():
        async for ev in agent.desk_or_fast(req.date, req.note, fast=fast or req.fast, speak=req.speak):
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/plan/latest")
def get_latest_plan():
    p = agent.latest_plan()
    if not p:  # voice agent asked before the UI planned: make one
        return agent.replan()
    return agent.summary(p)


@app.post("/api/replan")
async def post_replan(req: ReplanReq, fast: bool = False):
    """Voice tool: re-plan with an operator note (the desk reads it; the fast path parses and stores it as constraints
    with supersession). Returns a compact JSON summary."""
    if req.constraint:
        c = agent.parse_constraint(req.constraint)
        for k, v in (c or {}).items():  # remember it either way so later plans keep it
            agent.add_constraint(k, v, req.constraint, "voice")
    plan, statuses = None, []
    async for ev in agent.desk_or_fast(req.date, req.constraint, fast=fast, speak=False):
        if ev["kind"] == "plan":
            plan = ev
        elif ev["kind"] == "status":
            statuses.append(ev["text"])
        elif ev["kind"] == "error":
            return {"error": ev["text"]}
    if not plan:
        return {"error": "no plan", "status": statuses[-3:]}
    return {**agent.summary(plan), "verdict": plan.get("verdict"), "engine": plan.get("engine", "fast")}


@app.get("/api/voice/agent")
def get_voice_agent(signed: bool = False):
    if not voice.enabled():
        raise HTTPException(503, "ElevenLabs disabled (no ELEVENLABS_API_KEY)")
    aid = voice.ensure_agent()
    if not aid:
        raise HTTPException(502, voice.agent_status()["agent_error"] or "agent setup failed")
    return {"agent_id": aid, "signed_url": voice.signed_url(aid) if signed else None}


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: str):
    p = agent.PLANS.get(plan_id)
    if not p:
        raise HTTPException(404, "unknown plan")
    return p


@app.get("/api/backtest")
def get_backtest(days: int = 30, sandbox: bool = False):
    """Plan on day-ahead, settle on real-time, last `days` days. sandbox=1: fan out one Daytona sandbox per day.
    Cached per (days, mode, today)."""
    key = (days, sandbox, date.today().isoformat())
    if key not in _backtest_cache:
        if sandbox:
            _backtest_cache[key] = agent.backtest_fanout(days)
        else:
            r = optimize.arb_backtest(days=days)
            for x in r["days"]:
                x.update(baseline_usd=x["baseline_profit"], optimal_usd=x["optimal_profit"], saved_usd=x["optimal_profit"])
            t = r["totals"]
            t.update(saved_usd=t["optimal_profit"], baseline_usd=t["baseline_profit"], sandboxes=0)
            _backtest_cache[key] = r
    return _backtest_cache[key]


@app.get("/api/backtest/stream")
async def get_backtest_stream(days: int = 30):
    """SSE: {"kind":"status","text":"night 12/30 simulated in sandbox <id>", i, n, date, sandbox_id} ... then
    {"kind":"backtest", days, totals} and {"kind":"done"}. One ephemeral Daytona sandbox per day, 8 at a time."""
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def progress(i, n, label, sid, where):
        text = f"night {i}/{n} simulated in sandbox {sid}" if sid else f"night {i}/{n} simulated locally"
        loop.call_soon_threadsafe(q.put_nowait, {"kind": "status", "text": text, "i": i, "n": n,
                                                 "date": label.removeprefix("day "), "sandbox_id": sid, "where": where})

    def work():
        try:
            loop.call_soon_threadsafe(q.put_nowait, {"kind": "status", "text": f"fanning out {days} days across Daytona sandboxes (8 at a time)"})
            r = agent.backtest_fanout(days, on_progress=progress)
            _backtest_cache[(days, True, date.today().isoformat())] = r
            loop.call_soon_threadsafe(q.put_nowait, {"kind": "backtest", **r})
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(q.put_nowait, {"kind": "error", "text": f"{type(e).__name__}: {str(e)[:200]}"})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, {"kind": "done"})
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


class BatteryReq(BaseModel):
    name: str | None = None
    power_mw: float | None = None
    energy_mwh: float | None = None
    rte: float | None = None
    start_soc_pct: float | None = None
    end_soc_pct: float | None = None
    min_soc_pct: float | None = None
    max_soc_pct: float | None = None
    cycles_per_day: float | None = None
    degradation_usd_mwh: float | None = None
    zone: str | None = None
    sell_above: float | None = None
    buy_below: float | None = None
    note: str | None = None  # plain words, e.g. "only sell above $60", "we have 22 MWh stored right now"
    replace: bool = False


@app.get("/api/battery")
def get_battery():
    from grid import profile

    return {"profile": profile.get_profile(), "default": profile.DEFAULT, "zones": profile.ZONES,
            "history": profile.history(), "memory": memory.recall("battery", history=True)[:30]}


@app.post("/api/battery")
def post_battery(req: BatteryReq):
    """Save the user's battery profile (merge; replace=true resets unspecified fields to defaults). Fields left null are
    unchanged, except sell_above / buy_below which can be cleared with the note 'remove the price rules'."""
    from grid import profile

    upd = {k: v for k, v in req.model_dump(exclude_unset=True).items() if k not in ("note", "replace")}  # explicit null clears a rule
    if req.note:
        upd.update(profile.parse_note(req.note))
    if not upd and not req.replace:
        raise HTTPException(422, "nothing to update")
    try:
        r = profile.save_profile(upd, "ui", replace=req.replace)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {**r, "history": profile.history(5)}


@app.get("/api/constraints")
def get_constraints():
    return {"active": agent.active_constraints(), "defaults": agent.DEFAULT_CONSTRAINTS}


@app.post("/api/constraints")
def post_constraint(req: ConstraintReq):
    """Store a constraint without re-planning (supersedes the previous value of each key)."""
    c = agent.parse_constraint(req.text)
    if not c:
        raise HTTPException(422, "could not parse constraint")
    return {"stored": [agent.add_constraint(k, v, req.text, "ui") for k, v in c.items()], "active": agent.active_constraints()}


@app.get("/api/memory")
def get_memory(kind: str | None = None, key: str | None = None, history: bool = True, limit: int = 20):
    """Memory panel: status, last facts (newest first, superseded ones carry valid_until + superseded_by),
    and the supersession diff of the last 24h."""
    facts = memory.recall(kind, key, history) if kind else memory.recent(limit)
    return {"status": memory.status(), "facts": facts, "changes_24h": memory.changes_since(24)}


@app.get("/api/memory/ask")
def get_memory_ask(q: str = "what changed since last night?"):
    return memory.ask(q)


# ---- extra routers (other tracks mount here, e.g. grid/ask.py: `router = APIRouter()`)
for _mod in ("grid.ask", "grid.runs"):
    try:
        import importlib

        app.include_router(importlib.import_module(_mod).router)
        log.info("mounted router %s", _mod)
    except ModuleNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("router %s not mounted: %s", _mod, e)
