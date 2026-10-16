"""Hatchet workflows for the grid battery trader (namespace "grid").

  nightly_plan     cron, daily 11:30 America/New_York (after NYISO publishes tomorrow's day-ahead prices): runs the
                   dispatcher for tomorrow and stores the plan as pending approval (memory + data/plans/<id>.json).
  backtest         fan-out parent: one `backtest_night` child per night, each simulating in its own ephemeral Daytona
                   sandbox (grid.sponsors.daytona_sim). Children are capped at 8 concurrent (10 vCPU org quota).
                   The parent streams {"kind":"backtest", n, of, sandbox_id, day} per finished night and aggregates.
  backtest_night   child: plan on day-ahead, settle on real-time, in one sandbox (local fallback flagged "local").

Every run is wrapped in agentglow.job so it shows in the AgentGlow scene.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- Hatchet client env (must be set before Hatchet() is constructed). Never logs the token.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
for _f in (os.environ.get("HATCHET_CLIENT_TOKEN_FILE"), str(ROOT / "data" / "hatchet" / "token")):
    if not os.environ.get("HATCHET_CLIENT_TOKEN") and _f and Path(_f).is_file() and Path(_f).read_text().strip():
        os.environ["HATCHET_CLIENT_TOKEN"] = Path(_f).read_text().strip()
os.environ.setdefault("HATCHET_CLIENT_HOST_PORT", "localhost:7378")
os.environ.setdefault("HATCHET_CLIENT_SERVER_URL", "http://localhost:8388")
os.environ.setdefault("HATCHET_CLIENT_TLS_STRATEGY", "none")
os.environ.setdefault("HATCHET_CLIENT_NAMESPACE", "grid")

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy, Context, Hatchet  # noqa: E402
from pydantic import BaseModel  # noqa: E402

log = logging.getLogger("grid.workflows")
hatchet = Hatchet()
NY = ZoneInfo("America/New_York")
PLANS_DIR = ROOT / "data" / "plans"
SANDBOX_CONC = int(os.environ.get("BACKTEST_CONCURRENCY", "8"))
STREAM_GRACE_S = 0.7  # let the API subscribe before the first event (Hatchet streams are not buffered)


class _nullcm:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set(self, *a, **k):
        pass


def _job(name: str, kind: str):
    try:
        import agentglow

        return agentglow.job(name, kind=kind)
    except Exception:  # noqa: BLE001
        return _nullcm()


async def _put(ctx: Context, ev: dict) -> None:
    try:
        await ctx.aio_put_stream(json.dumps(ev, default=str))
    except Exception:  # noqa: BLE001  stream is best effort; the output carries everything
        pass


# ------------------------------------------------------------------ backtest (fan-out)
class NightInput(BaseModel):
    date: str
    reserve_pct: float = 10
    cycles: float = 1.0


class BacktestInput(BaseModel):
    days: int = 30
    end: str | None = None  # last night (inclusive); default yesterday
    reserve_pct: float | None = None  # None: use the operator's active constraints
    cycles: float | None = None


def _night_sync(inp: NightInput) -> dict:
    from grid import prices
    from grid.sponsors import daytona_sim

    d = date.fromisoformat(inp.date)
    da, rt = prices.day_ahead(d), prices.real_time(d)
    if len(da) < 23 or len(rt) < 250:
        return {"date": inp.date, "skipped": "incomplete NYISO data"}
    job = (f"night {inp.date}", "arb_day", [inp.date, da, rt], {"reserve_pct": inp.reserve_pct, "cycles": inp.cycles})
    r = daytona_sim.fan_out([job], conc=1)[0]
    x = r["result"]
    return {**x, "sandbox_id": r["sandbox_id"], "where": r["where"]}


@hatchet.task(name="backtest_night", input_validator=NightInput, execution_timeout=timedelta(minutes=5), retries=1,
              concurrency=ConcurrencyExpression(expression="'daytona'", max_runs=SANDBOX_CONC,
                                                limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN))
async def backtest_night(input: NightInput, ctx: Context) -> dict:
    return await asyncio.to_thread(_night_sync, input)


def _constraints(inp: BacktestInput) -> tuple[float, float]:
    reserve, cycles = inp.reserve_pct, inp.cycles
    if reserve is None or cycles is None:
        try:
            from grid.agent import active_constraints

            c = active_constraints()
            reserve = reserve if reserve is not None else float(c.get("reserve_pct") or 10)
            cycles = cycles if cycles is not None else float(c["cycles"] if c.get("cycles") is not None else 1.0)
        except Exception:  # noqa: BLE001
            reserve, cycles = reserve or 10.0, cycles if cycles is not None else 1.0
    return float(reserve), float(cycles)


@hatchet.task(name="backtest", input_validator=BacktestInput, execution_timeout=timedelta(minutes=20), retries=0)
async def backtest(input: BacktestInput, ctx: Context) -> dict:
    await asyncio.sleep(STREAM_GRACE_S)
    t0 = time.monotonic()
    end = date.fromisoformat(input.end) if input.end else date.today() - timedelta(days=1)
    nights = [(end - timedelta(days=input.days - 1 - i)).isoformat() for i in range(input.days)]
    reserve, cycles = _constraints(input)
    events: list[dict] = []

    async def emit(ev: dict) -> None:
        ev = {**ev, "seq": len(events), "run_id": ctx.workflow_run_id}
        events.append(ev)
        await _put(ctx, ev)

    with _job(f"backtest-{input.days}n-{ctx.workflow_run_id[:8]}", "hatchet-backtest") as job:
        await emit({"kind": "status", "text": f"fan-out: {len(nights)} nights, {SANDBOX_CONC} Daytona sandboxes at a time"})

        async def one(d: str) -> dict:
            try:
                return await backtest_night.aio_run(NightInput(date=d, reserve_pct=reserve, cycles=cycles))
            except Exception as e:  # noqa: BLE001
                return {"date": d, "error": f"{type(e).__name__}: {str(e)[:160]}"}

        results: list[dict] = []
        for n, fut in enumerate(asyncio.as_completed([one(d) for d in nights]), 1):
            r = await fut
            results.append(r)
            await emit({"kind": "backtest", "n": n, "of": len(nights), "sandbox_id": r.get("sandbox_id"),
                        "where": r.get("where"), "date": r.get("date"), "day": r})
            try:
                import agentglow

                agentglow.progress(n / len(nights), label=f"{n}/{len(nights)} nights")
            except Exception:  # noqa: BLE001
                pass

        days = sorted((r for r in results if "baseline_profit" in r), key=lambda r: r["date"])
        tb = sum(r["baseline_profit"] for r in days)
        to = sum(r["optimal_profit"] for r in days)
        totals = {"days": len(days), "nights_requested": len(nights), "baseline_profit": round(tb, 2),
                  "optimal_profit": round(to, 2), "uplift": round(to - tb, 2),
                  "sandboxes": sum(1 for r in days if r.get("where") == "daytona"),
                  "distinct_sandboxes": len({r["sandbox_id"] for r in days if r.get("sandbox_id")}),
                  "skipped": [r["date"] for r in results if "baseline_profit" not in r],
                  "reserve_pct": reserve, "cycles": cycles, "elapsed_s": round(time.monotonic() - t0, 1)}
        job.set("agentglow.final", f"{totals['days']} nights, {totals['sandboxes']} in Daytona, uplift ${totals['uplift']:,.0f}")
        await emit({"kind": "backtest_done", "days": days, "totals": totals})
        await emit({"kind": "done"})
    return {"days": days, "totals": totals, "events": events}


# ------------------------------------------------------------------ nightly plan
class NightlyInput(BaseModel):
    date: str | None = None  # trading day; default tomorrow (America/New_York)
    force: bool = False  # cron fires at 15:30 and 16:30 UTC; only the one at 11:xx New York time runs


def _nightly_sync(inp: NightlyInput, emit_sync) -> dict:
    from grid import agent

    day = inp.date or (datetime.now(NY).date() + timedelta(days=1)).isoformat()
    plan = None
    for ev in agent.run_plan(d=day, speak=True):
        emit_sync(ev)
        if ev.get("kind") == "plan":
            plan = {k: v for k, v in ev.items() if k != "kind"}
        if ev.get("kind") == "error":
            raise RuntimeError(ev.get("text"))
    if plan is None:
        raise RuntimeError("dispatcher produced no plan")
    plan["status"] = "pending_approval"
    plan["source"] = "hatchet:nightly_plan"
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    (PLANS_DIR / f"{plan['plan_id']}.json").write_text(json.dumps(plan, default=str))
    return plan


@hatchet.task(name="nightly_plan", input_validator=NightlyInput, on_crons=["30 15 * * *", "30 16 * * *"],
              execution_timeout=timedelta(minutes=10), retries=1)
async def nightly_plan(input: NightlyInput, ctx: Context) -> dict:
    now_ny = datetime.now(NY)
    if not input.force and input.date is None and now_ny.hour != 11 and ctx.additional_metadata.get("manual") != "1":
        # Hatchet crons are UTC: 15:30 UTC is 11:30 EDT, 16:30 UTC is 11:30 EST. Skip the off-DST one.
        return {"skipped": f"not 11:30 in New York (now {now_ny:%H:%M %Z})"}
    await asyncio.sleep(STREAM_GRACE_S)
    loop = asyncio.get_running_loop()
    events: list[dict] = []

    def emit_sync(ev: dict) -> None:
        ev = {**ev, "seq": len(events), "run_id": ctx.workflow_run_id}
        events.append(ev)
        asyncio.run_coroutine_threadsafe(_put(ctx, ev), loop)

    with _job(f"nightly-{ctx.workflow_run_id[:8]}", "hatchet-nightly") as job:
        plan = await asyncio.to_thread(_nightly_sync, input, emit_sync)
        job.set("agentglow.final", f"plan {plan['plan_id']} for {plan['date']}: ${plan.get('profit_usd', 0):,.0f} pending approval")
    return {"plan": plan, "events": events}


WORKFLOWS = [nightly_plan, backtest, backtest_night]
