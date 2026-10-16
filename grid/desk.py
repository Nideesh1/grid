"""Trading desk: a deepagents team on AWS Bedrock that plans tomorrow's battery arbitrage.

desk-lead (orchestrator) delegates to three subagents:
- market-analyst: tomorrow's day-ahead + recent real-time prices (spread, cheapest/priciest hours, volatility).
- trader: calls the optimizer with constraints, writes the 2-3 sentence plan message from tool numbers only.
- risk-officer: simulates the plan (Daytona), checks hard limits and operator rules, approves or vetoes.

Invariant: LLMs never set MW numbers. Only grid.optimize.arb_optimal produces a schedule; the LLMs pick constraint
values (reserve %, cycles, hour windows). The final verdict is forced to veto if the simulation has violations.

Public API:
    await run_desk(date=None, operator_note=None) -> {message, plan, simulation, verdict, steps, ...}
    async for ev in stream_desk(date, operator_note): ...   # {"kind":"status","text":...} ... {"kind":"result",...}
"""
from __future__ import annotations

import grid.config  # noqa: F401  (import first: .env + tracing setup lives here)

import asyncio
import contextvars
import json
import logging
import statistics
import threading
import time
import uuid
from datetime import date as _date, timedelta
from typing import Any, AsyncIterator

from grid.config import env, setup_tracing

log = logging.getLogger("grid.desk")
MODEL_ID = "us.anthropic.claude-sonnet-5-5"
REGION = "us-west-2"

_run: contextvars.ContextVar[dict | None] = contextvars.ContextVar("desk_run", default=None)
_agent_cache: dict = {}
_DEFAULTS = {"reserve_pct": 10.0, "cycles": 1.0, "no_discharge_before": None, "no_charge_after": None}


# ------------------------------------------------------------------ run state + status events
def _state() -> dict:
    st = _run.get()
    if st is None:  # tools called outside a run (tests): give them a scratch state
        st = _new_state(None, None)
        _run.set(st)
    return st


def _new_state(day: str | None, note: str | None) -> dict:
    return {"date": day, "note": note, "plans": {}, "sims": {}, "prices": {}, "messages": {}, "verdicts": [],
            "events": None, "loop": None, "lock": threading.Lock()}


def _emit(agent: str, text: str) -> None:
    st = _run.get()
    if not st or st.get("events") is None:
        return
    ev = {"kind": "status", "agent": agent, "text": f"{agent}: {text}"}
    try:
        st["loop"].call_soon_threadsafe(st["events"].put_nowait, ev)
    except RuntimeError:
        pass


# ------------------------------------------------------------------ data helpers
def _tomorrow() -> str:
    return (_date.today() + timedelta(days=1)).isoformat()


def _da(day: str) -> list[dict]:
    st = _state()
    with st["lock"]:
        if day not in st["prices"]:
            from grid import prices

            st["prices"][day] = prices.day_ahead(day)
        return st["prices"][day]


def _rules() -> dict:
    """Operator rules: persisted constraints (same memory the main agent uses) + desk rules + this run's note."""
    from grid.sponsors import memory

    req = dict(_DEFAULTS)
    texts: list[str] = []
    try:
        for f in memory.recall("constraint"):
            v = f["value"].get("value") if isinstance(f["value"], dict) else f["value"]
            if f["key"] in req and v is not None:
                req[f["key"]] = v
            t = f["value"].get("text") if isinstance(f["value"], dict) else None
            if t:
                texts.append(t)
        for f in memory.recall("desk_rule"):
            val = f["value"] if isinstance(f["value"], dict) else {"text": str(f["value"])}
            texts.append(val.get("text", ""))
            for k, v in (val.get("parsed") or {}).items():
                req[k] = _tighter(k, req.get(k), v)
    except Exception as e:  # noqa: BLE001
        log.warning("memory recall failed: %s", type(e).__name__)
    note = _state().get("note")
    if note:
        texts.append(f"(today's operator note) {note}")
        for k, v in (_parse(note) or {}).items():
            req[k] = _tighter(k, req.get(k), v)
    notes = []
    from grid.optimize import _battery_dict

    bat = _battery_dict()
    cap = min(bat["start_soc_pct"], bat["end_soc_pct"])
    if req["reserve_pct"] > cap:
        notes.append(f"requested {req['reserve_pct']:g}% reserve is above the {cap:g}% start/end level; enforcing {cap:g}%")
        req["reserve_pct"] = cap
    return {"required": req, "texts": [t for t in texts if t], "notes": notes}


def _parse(text: str) -> dict | None:
    try:
        from grid.agent import parse_constraint

        out = parse_constraint(text)
        if out is not None and set(out) == set(_DEFAULTS) and out == _DEFAULTS:
            return None  # "reset" text is not a tightening rule
        return out
    except Exception:  # noqa: BLE001  (grid.agent is edited concurrently; a regex fallback keeps the desk alive)
        import re

        m = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", (text or "").lower())
        return {"reserve_pct": float(m.group(1))} if m and "reserve" in text.lower() else None


def _tighter(k: str, a, b):
    if a is None:
        return b
    if b is None:
        return a
    if k in ("reserve_pct", "no_discharge_before"):
        return max(a, b)
    return min(a, b)  # cycles, no_charge_after


def _rule_violations(plan: dict, sim: dict, req: dict) -> list[str]:
    """Operator rules checked against what the plan actually does (simulated SOC, hours), not its labels."""
    out = []
    floor = req["reserve_pct"]
    if sim.get("min_soc") is not None and sim["min_soc"] + 0.05 < floor:
        out.append(f"rule: charge level dips to {sim['min_soc']:.0f}% below the {floor:g}% reserve")
    if req["cycles"] is not None and (sim.get("cycles") or 0) > req["cycles"] + 1e-3:
        out.append(f"rule: {sim['cycles']:.2f} cycles above the {req['cycles']:g} cycle limit")
    nd = req.get("no_discharge_before")
    if nd is not None and any(h["discharge_mw"] > 1e-6 and h["hour"] < nd for h in plan["hours"]):
        out.append(f"rule: sells before {_clock(nd)}")
    nc = req.get("no_charge_after")
    if nc is not None and any(h["charge_mw"] > 1e-6 and h["hour"] >= nc for h in plan["hours"]):
        out.append(f"rule: buys after {_clock(nc)}")
    return out


def _clock(h: int) -> str:
    h %= 24
    return {0: "midnight", 12: "noon"}.get(h, f"{h % 12}{'am' if h < 12 else 'pm'}")


def _windows(hours: list[int]) -> str:
    """[2,3,4,12,13,14] -> '2 to 5am and noon to 3pm' (hour-beginning hours -> clock ranges)."""
    if not hours:
        return "none"
    runs, start, prev = [], hours[0], hours[0]
    for h in hours[1:] + [None]:
        if h is not None and h == prev + 1:
            prev = h
            continue
        a, b = start, prev + 1
        ca, cb = _clock(a), _clock(b)
        same = (a < 12) == (b % 24 < 12) and a not in (0, 12) and b % 24 not in (0, 12)
        runs.append(f"{a % 12} to {cb}" if same else f"{ca} to {cb}")
        if h is not None:
            start = prev = h
    return runs[0] if len(runs) == 1 else ", ".join(runs[:-1]) + " and " + runs[-1]


# ------------------------------------------------------------------ tools (plain functions; LLMs see the docstrings)
def get_day_ahead(date: str | None = None) -> dict:
    """Tomorrow's (or `date`, YYYY-MM-DD) NYISO N.Y.C. hourly day-ahead prices in $/MWh, with a summary:
    cheapest/priciest hours, spread, mean, stdev."""
    day = date or _state()["date"] or _tomorrow()
    _emit("market-analyst", f"reading day-ahead prices for {day}")
    rows = _da(day)
    if not rows:
        return {"date": day, "error": "no day-ahead prices published"}
    from grid.optimize import hourly_prices

    hp = hourly_prices(rows)
    ps = [h["usd_mwh"] for h in hp]
    order = sorted(hp, key=lambda h: h["usd_mwh"])
    return {"date": day, "hours": [[h["hour"], h["usd_mwh"]] for h in hp],
            "cheapest_4": [[h["hour"], h["usd_mwh"]] for h in order[:4]],
            "priciest_4": [[h["hour"], h["usd_mwh"]] for h in order[-4:][::-1]],
            "min": min(ps), "max": max(ps), "spread": round(max(ps) - min(ps), 2),
            "mean": round(statistics.mean(ps), 2), "stdev": round(statistics.pstdev(ps), 2)}


def get_real_time_summary(days: int = 3) -> dict:
    """Recent NYISO N.Y.C. real-time prices for the last `days` complete days (max 7): per-day hourly
    min/max/mean/stdev and the hours of the daily low and high, to judge volatility vs day-ahead."""
    from grid import prices
    from grid.optimize import hourly_prices

    days = max(1, min(int(days or 3), 7))
    _emit("market-analyst", f"reading {days} days of real-time prices")
    out = []
    for i in range(days, 0, -1):
        d = _date.today() - timedelta(days=i)
        rows = prices.real_time(d)
        if len(rows) < 200:
            continue
        hp = hourly_prices(rows)
        ps = [h["usd_mwh"] for h in hp]
        lo, hi = min(hp, key=lambda h: h["usd_mwh"]), max(hp, key=lambda h: h["usd_mwh"])
        out.append({"date": d.isoformat(), "min": lo["usd_mwh"], "min_hour": lo["hour"], "max": hi["usd_mwh"],
                    "max_hour": hi["hour"], "mean": round(statistics.mean(ps), 2), "stdev": round(statistics.pstdev(ps), 2)})
    return {"days": out}


def optimize(date: str | None = None, reserve_pct: float = 10, cycles: float = 1.0,
             no_discharge_before: int | None = None, no_charge_after: int | None = None) -> dict:
    """Run the battery optimizer (MILP) on the day-ahead prices. You choose ONLY constraints:
    reserve_pct (min state of charge %, 10..50), cycles (0..1 full cycles), no_discharge_before (hour 0-23, no selling
    before it), no_charge_after (hour 0-23, no buying from it on). Returns plan_id and the plan's numbers
    (charge/discharge hours and MW, expected profit). The optimizer alone sets every MW value."""
    day = date or _state()["date"] or _tomorrow()
    reserve_pct = float(min(max(reserve_pct or 10, 10), 50))
    cycles = float(min(max(cycles if cycles is not None else 1.0, 0), 1))
    _emit("trader", f"optimizing {day} (reserve {reserve_pct:g}%, {cycles:g} cycle"
                    + (f", no selling before {no_discharge_before:02d}:00" if no_discharge_before is not None else "")
                    + (f", no buying from {no_charge_after:02d}:00" if no_charge_after is not None else "") + ")")
    rows = _da(day)
    if not rows:
        return {"error": f"no day-ahead prices for {day}"}
    from grid.optimize import arb_optimal

    plan = arb_optimal(rows, reserve_pct=reserve_pct, cycles=cycles,
                       no_discharge_before=no_discharge_before, no_charge_after=no_charge_after)
    pid = "desk-" + uuid.uuid4().hex[:8]
    plan["plan_id"] = pid
    _state()["plans"][pid] = plan
    ch = [[h["hour"], h["charge_mw"], h["price"]] for h in plan["hours"] if h["charge_mw"] > 1e-6]
    dis = [[h["hour"], h["discharge_mw"], h["price"]] for h in plan["hours"] if h["discharge_mw"] > 1e-6]
    return {"plan_id": pid, "date": plan["date"], "status": plan["status"], "constraints": plan["constraints"],
            "buy_window": _windows([c[0] for c in ch]), "sell_window": _windows([d[0] for d in dis]),
            "mwh_bought": round(sum(c[1] for c in ch)), "mwh_sold": round(sum(d[1] for d in dis)),
            "profit": f"${plan['expected_profit_usd']:,.0f}",
            "reserve": f"{plan['constraints']['reserve_pct']:g}%"}


def _simulate(plan_id: str, agent: str) -> dict:
    st = _state()
    plan = st["plans"].get(plan_id)
    if plan is None:
        return {"error": f"unknown plan_id {plan_id}"}
    with st["lock"]:
        if plan_id in st["sims"]:
            return st["sims"][plan_id]
        _emit(agent, f"simulating {plan_id} in Daytona")
        from grid.sponsors import daytona_sim

        try:
            r = daytona_sim.call("arb_simulate", {k: v for k, v in plan.items() if k != "plan_id"}, None)
            sim, where, sbid = r["result"], r["where"], r.get("sandbox_id")
        except Exception as e:  # noqa: BLE001
            log.warning("simulate fallback: %s", type(e).__name__)
            from grid.optimize import arb_simulate

            sim, where, sbid = arb_simulate(plan, None), "local", None
        rules = _rules()
        sim = {**sim, "plan_id": plan_id, "where": where, "sandbox_id": sbid,
               "limit_violations": list(sim.get("violations") or []),
               "rule_violations": _rule_violations(plan, sim, rules["required"]),
               "required_by_rules": rules["required"]}
        sim["violations"] = sim["limit_violations"] + sim["rule_violations"]
        st["sims"][plan_id] = sim
        return sim


def simulate(plan_id: str) -> dict:
    """Replay a plan in the Daytona sandbox: profit, cycles, min/max/end SOC, and `violations` (hard battery limits
    plus operator-rule breaches). An empty violations list means the plan is clean."""
    return _simulate(plan_id, "risk-officer")


def simulate_for_trader(plan_id: str) -> dict:
    """Replay a plan (Daytona) to get its settled numbers: profit_usd, revenue_usd, cost_usd, cycles, SOC."""
    return _simulate(plan_id, "trader")


simulate_for_trader.__name__ = "simulate"


def market_brief(day: str) -> str:
    """market-analyst as a plain tool call: day-ahead shape + recent real-time volatility in 3 lines."""
    da = get_day_ahead(day)
    if da.get("error"):
        return f"No day-ahead prices for {day}."
    rt = get_real_time_summary(3)["days"]
    lo = _windows(sorted(h for h, _ in da["cheapest_4"]))
    hi = _windows(sorted(h for h, _ in da["priciest_4"]))
    rt_sd = round(statistics.mean(d["stdev"] for d in rt), 1) if rt else None
    return (f"Day-ahead {day}: cheapest {lo} (${da['min']:.0f}), priciest {hi} (${da['max']:.0f}), "
            f"spread ${da['spread']:.0f}/MWh, stdev ${da['stdev']:.0f}. "
            + (f"Recent real-time hourly stdev ${rt_sd}/MWh over {len(rt)} days." if rt else ""))


def recall_rules() -> dict:
    """The operator's standing rules from memory plus today's operator note, and the tightest constraints they
    imply (`required`: reserve_pct, cycles, no_discharge_before, no_charge_after)."""
    _emit("risk-officer", "recalling operator rules")
    return _rules()


def remember_rule(text: str) -> dict:
    """Save a standing operator rule (only when the operator asks to remember it, e.g. 'always', 'from now on')."""
    from grid.sponsors import memory

    _emit("risk-officer", f"remembering rule: {text[:80]}")
    f = memory.remember("desk_rule", uuid.uuid4().hex[:8], {"text": text, "parsed": _parse(text) or {}}, "desk")
    return {"saved": True, "id": f["id"]}


def record_verdict(plan_id: str, decision: str, reason: str, cited_violations: list[str] | None = None) -> dict:
    """Record your risk verdict. decision: 'approve' or 'veto'. cited_violations: copy the exact strings from the
    simulation's violations list (empty if none). On veto, reason must say which tighter constraints to replan with."""
    sim = _state()["sims"].get(plan_id)
    if sim is None:
        return {"error": "simulate the plan first"}
    decision = "veto" if decision.strip().lower().startswith("v") else "approve"
    forced = decision == "approve" and bool(sim["violations"])
    if forced:
        decision = "veto"
    v = {"plan_id": plan_id, "decision": decision, "reason": reason, "cited_violations": cited_violations or [],
         "sim_violations": sim["violations"], "forced_veto": forced}
    _state()["verdicts"].append(v)
    if decision == "veto":
        why = (sim["violations"] or [reason])[0].removeprefix("rule: ")
        _emit("risk-officer", f"VETO, {why}, asking trader to replan")
    else:
        _emit("risk-officer", f"APPROVE, {sim['profit_usd']:,.0f} dollars, {len(sim['violations'])} violations")
    return {"recorded": decision, "note": "approve overridden: simulation has violations" if forced else None}


def publish_message(plan_id: str, text: str) -> dict:
    """Publish the 2-3 sentence plan message for the operator. Use only numbers that appear in tool results."""
    _state()["messages"][plan_id] = text.strip()
    _emit("trader", "wrote plan message")
    return {"published": True}


# ------------------------------------------------------------------ the team
LEAD_PROMPT = """You are desk-lead of a trading desk for one grid battery (10 MW / 40 MWh, NYISO N.Y.C.).
Separation of duties: the trader optimizes for profit; the risk-officer alone enforces the operator's rules.
Delegate with the `task` tool; do not use todos or files. Keep task descriptions short.
1. task trader: give it the market brief and the date ONLY (not the operator note). It returns a plan_id.
2. task risk-officer: give it the plan_id. It records approve or veto.
3. If vetoed: task trader again with exactly the constraints the risk-officer asked for, then risk-officer again
   with the new plan_id. At most 2 replans.
Finish with one line: FINAL plan_id=<id> verdict=<approve|veto>. Never invent numbers."""

TRADER_PROMPT = """You are trader. Call optimize with constraints only; the optimizer sets every MW value.
Defaults reserve_pct=10, cycles=1 unless you were given constraints. Then call publish_message(plan_id, text) with
at most 2 plain-English sentences for a stage demo, copied from the optimize result fields, like:
"Tomorrow we buy 43 MWh from 2 to 5am and noon to 3pm and sell 37 MWh from 6 to 8am and 5 to 7pm, for $418 profit.
We keep a 30% reserve and never exceed one cycle." Use buy_window/sell_window/mwh/profit/reserve verbatim.
No hour codes, no decimals, no em dashes. Reply with plan_id and profit in one line."""

RISK_PROMPT = """You are risk-officer. Call simulate(plan_id) and recall_rules() in parallel. If the simulation's
violations list is non-empty you MUST veto: cited_violations = those exact strings, and reason = the tighter
constraints to replan with, taken from recall_rules `required` (e.g. "replan with reserve_pct=30"). Otherwise approve.
Call record_verdict once. Reply in one line: decision, plan_id, constraints to replan with (if veto).
Only call remember_rule if the operator explicitly asks to remember a rule for the future."""


def _model():
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(model=MODEL_ID, region_name=REGION,
                               aws_access_key_id=env("AWS_ACCESS_KEY_ID"),
                               aws_secret_access_key=env("AWS_SECRET_ACCESS_KEY"),
                               aws_session_token=env("AWS_SESSION_TOKEN"), max_tokens=800)


def build_desk():
    """Build (once) the deepagents desk graph."""
    if "agent" in _agent_cache:
        return _agent_cache["agent"]
    setup_tracing()  # AgentGlow watch() instruments LangChain/deepagents; Arize shares the global provider
    from deepagents import create_deep_agent

    model = _model()
    subagents = [
        {"name": "trader", "description": "Runs the optimizer with constraints and publishes the plan message. "
         "Give it the market brief, date, and any constraints from risk-officer.", "system_prompt": TRADER_PROMPT,
         "tools": [optimize, publish_message], "model": model},
        {"name": "risk-officer", "description": "Simulates a plan_id in Daytona, checks hard limits and the "
         "operator's rules, records approve or veto with cited violations.", "system_prompt": RISK_PROMPT,
         "tools": [simulate, recall_rules, remember_rule, record_verdict], "model": model},
    ]
    agent = create_deep_agent(model=model, tools=[], system_prompt=LEAD_PROMPT, subagents=subagents, name="desk-lead")
    _agent_cache["agent"] = agent
    return agent


# ------------------------------------------------------------------ run
def _steps(messages: list) -> list[dict]:
    calls, steps = {}, []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.get("name") == "task":
                calls[tc["id"]] = (tc.get("args") or {}).get("subagent_type", "subagent")
        if getattr(m, "type", "") == "tool" and getattr(m, "tool_call_id", None) in calls:
            txt = m.content if isinstance(m.content, str) else json.dumps(m.content)[:400]
            steps.append({"agent": calls[m.tool_call_id], "summary": " ".join(txt.split())[:400]})
    return steps


def _result(st: dict, messages: list, t0: float) -> dict:
    final = st["verdicts"][-1] if st["verdicts"] else None
    pid = final["plan_id"] if final else (list(st["plans"])[-1] if st["plans"] else None)
    plan = st["plans"].get(pid) if pid else None
    sim = st["sims"].get(pid) if pid else None
    if plan is not None and sim is None:
        sim = _simulate(pid, "risk-officer")
    verdict = final["decision"] if final else "veto"
    if sim and sim["violations"]:
        verdict = "veto"
    msg = st["messages"].get(pid) or (list(st["messages"].values())[-1] if st["messages"] else "")
    steps = [{"agent": "market-analyst", "summary": st.get("brief", "")}] + _steps(messages)
    lead = messages[-1].content if messages else ""
    if isinstance(lead, list):
        lead = " ".join(x.get("text", "") for x in lead if isinstance(x, dict))
    steps.append({"agent": "desk-lead", "summary": " ".join(str(lead).split())[:300]})
    return {"message": msg, "plan": plan, "simulation": sim, "verdict": verdict,
            "verdict_detail": final, "verdicts": st["verdicts"], "steps": steps, "date": st["date"],
            "operator_note": st["note"], "elapsed_s": round(time.time() - t0, 2)}


def _prompt(day: str, note: str | None, brief: str = "") -> str:
    return (f"Trade date: {day}. Market brief (from market-analyst): {brief}\n"
            f"Operator note (risk-officer enforces it via recall_rules): {note or 'none'}.\n"
            "Plan, risk-check and approve tomorrow's trades.")


async def stream_desk(date: str | None = None, operator_note: str | None = None) -> AsyncIterator[dict]:
    """Yields {"kind":"status","text":"<agent>: ..."} events while the desk works, then one {"kind":"result", ...}."""
    t0 = time.time()
    day = date or _tomorrow()
    st = _new_state(day, operator_note)
    st["events"], st["loop"] = asyncio.Queue(), asyncio.get_running_loop()
    token = _run.set(st)
    try:
        agent = build_desk()
        yield {"kind": "status", "agent": "desk-lead", "text": f"desk-lead: planning {day}"
               + (f" with note '{operator_note}'" if operator_note else "")}
        brief = await asyncio.to_thread(contextvars.copy_context().run, market_brief, day)
        st["brief"] = brief
        yield {"kind": "status", "agent": "market-analyst", "text": f"market-analyst: {brief}"}
        prompt = _prompt(day, operator_note, brief)
        try:
            import agentglow

            agcm = agentglow.agent("desk-lead", task=prompt)
        except Exception:  # noqa: BLE001
            import contextlib

            agcm = contextlib.nullcontext()
        out: dict = {}

        async def drive():
            with agcm:
                res = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]},
                                          config={"recursion_limit": 60})
            out["messages"] = res.get("messages", [])

        task = asyncio.create_task(drive())
        while not task.done() or not st["events"].empty():
            try:
                yield await asyncio.wait_for(st["events"].get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
        exc = task.exception()
        if exc:
            log.warning("desk run failed: %s", type(exc).__name__)
            yield {"kind": "error", "text": f"desk-lead: {type(exc).__name__}: {str(exc)[:200]}"}
            r = _result(st, [], t0)
        else:
            r = _result(st, out.get("messages", []), t0)
        yield {"kind": "status", "agent": "desk-lead", "text": f"desk-lead: {r['verdict']} in {r['elapsed_s']}s"}
        yield {"kind": "result", **r}
    finally:
        try:
            _run.reset(token)
        except ValueError:  # generator closed from another context
            pass


async def run_desk(date: str | None = None, operator_note: str | None = None) -> dict:
    """Run the desk once; returns {message, plan, simulation, verdict: approve|veto, steps:[{agent, summary}], ...}."""
    result: dict = {}
    async for ev in stream_desk(date, operator_note):
        if ev["kind"] == "result":
            result = {k: v for k, v in ev.items() if k != "kind"}
    return result
