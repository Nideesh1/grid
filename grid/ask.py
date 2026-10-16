"""Ask the trader: question -> Bedrock Claude planner (tools over grid.optimize/grid.prices) -> the same model renders
an OpenUI Lang program streamed as SSE {"kind": "status"|"ui"|"error"|"done"}. Numbers only come from tool results.
Big arrays (hourly schedule, backtest days) are substituted server-side for the bare placeholders HOURS and DAYS."""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel

from grid import optimize, prices
from grid.config import env

log = logging.getLogger("grid.ask")
_tracer = trace.get_tracer("grid.ask")
router = APIRouter()

MODEL_ID = "us.anthropic.claude-sonnet-5-5"
REGION = "us-west-2"
TIMEOUT_S = 150
MAX_TURNS = 6

# ------------------------------------------------------------------ tracing


@contextmanager
def _null():
    yield None


def _ag(_fn: str, *a, **kw):
    try:
        import agentglow

        return getattr(agentglow, _fn)(*a, **kw)
    except Exception:  # noqa: BLE001
        return _null()


@contextmanager
def _tool_span(name: str, args: dict):
    with _ag("tool", name, args=args or {}):
        with _tracer.start_as_current_span(f"tool {name}", attributes={
                "openinference.span.kind": "TOOL", "tool.name": name,
                "input.value": json.dumps(args or {}, default=str)[:2000]}) as sp:
            yield sp


# ------------------------------------------------------------------ tools


def _default_date() -> str:
    tom = date.today() + timedelta(days=1)
    return tom.isoformat() if len(prices.day_ahead(tom)) >= 23 else date.today().isoformat()


def _da(d: str | None) -> tuple[str, list[dict]]:
    d = d or _default_date()
    rows = prices.day_ahead(d)
    if len(rows) < 23:
        raise ValueError(f"no NYISO day-ahead prices for {d}")
    return d, rows


def _hours(plan: dict) -> list[dict]:
    return [{k: h[k] for k in ("hour", "price", "charge_mw", "discharge_mw", "soc_pct")} for h in plan["hours"]]


def _checks(plan: dict, sim: dict, where: str, sandbox: str | None) -> list[dict]:
    bat, cons, v = plan["battery"], plan.get("constraints") or {}, sim.get("violations") or []
    floor = min(max(bat["soc_min_pct"], cons.get("reserve_pct") or 0), bat["start_soc_pct"], bat["end_soc_pct"])
    lim = cons.get("cycles", bat["max_cycles"])
    has = lambda s: [x for x in v if s in x]  # noqa: E731
    return [
        {"name": "SOC within limits", "ok": not has("SOC "),
         "detail": f"{sim.get('min_soc')}% to {sim.get('max_soc')}% (allowed {floor:.0f}% to {bat['soc_max_pct']:.0f}%)"},
        {"name": f"At most {lim} cycle", "ok": not has("cycles >"), "detail": f"{sim.get('cycles')} cycles"},
        {"name": "Never charge and discharge in the same hour", "ok": not has("simultaneous"), "detail": "checked every hour"},
        {"name": "End SOC restored", "ok": not has("end SOC"), "detail": f"ends at {sim.get('end_soc')}%"},
        {"name": "Independent simulation", "ok": not v,
         "detail": (f"Daytona sandbox {sandbox}" if where == "daytona" else "local replay") + f", {len(v)} violations"},
    ]


def _simulate(plan: dict) -> tuple[dict, str, str | None]:
    try:
        from grid.sponsors import daytona_sim

        r = daytona_sim.call("arb_simulate", plan)
        if isinstance(r.get("result"), dict) and "profit_usd" in r["result"]:
            return r["result"], r.get("where", "local"), r.get("sandbox_id")
    except Exception as e:  # noqa: BLE001
        log.warning("daytona arb_simulate failed: %s", type(e).__name__)
    return optimize.arb_simulate(plan), "local", None


def _summary(plan: dict) -> dict:
    hs = plan["hours"]
    return {"buy_hours": [h["hour"] for h in hs if h["charge_mw"] > 1e-3],
            "sell_hours": [h["hour"] for h in hs if h["discharge_mw"] > 1e-3],
            "price_by_hour_usd_mwh": {h["hour"]: h["price"] for h in hs}}


def t_get_prices(state: dict, date: str | None = None) -> dict:
    d, rows = _da(date)
    hp = optimize.hourly_prices(rows)
    lo, hi = min(hp, key=lambda h: h["usd_mwh"]), max(hp, key=lambda h: h["usd_mwh"])
    return {"date": d, "zone": "N.Y.C.", "market": "NYISO day-ahead", "unit": "usd_mwh",
            "hours": [{"hour": h["hour"], "usd_mwh": h["usd_mwh"]} for h in hp],
            "cheapest": {"hour": lo["hour"], "usd_mwh": lo["usd_mwh"]}, "priciest": {"hour": hi["hour"], "usd_mwh": hi["usd_mwh"]},
            "average_usd_mwh": round(sum(h["usd_mwh"] for h in hp) / len(hp), 2)}


def t_plan(state: dict, date: str | None = None, reserve_pct: float = 10, cycles: float = 1.0,
           no_discharge_before: int | None = None, no_charge_after: int | None = None) -> dict:
    d, rows = _da(date)
    plan = optimize.arb_optimal(rows, reserve_pct=reserve_pct, cycles=cycles,
                                no_discharge_before=no_discharge_before, no_charge_after=no_charge_after)
    sim, where, sb = _simulate(plan)
    state["HOURS"] = _hours(plan)
    checks = _checks(plan, sim, where, sb)
    state["CHECKS"] = checks
    return {"date": d, "status": plan["status"], "constraints": plan["constraints"],
            "expected_profit_usd": plan["expected_profit_usd"],
            "simulation": {k: sim.get(k) for k in ("revenue_usd", "cost_usd", "degradation_usd", "profit_usd",
                                                   "mwh_charged", "mwh_discharged", "cycles", "min_soc", "max_soc",
                                                   "end_soc", "violations")},
            "simulated_in": f"daytona {sb}" if where == "daytona" else "local", "checks": checks, **_summary(plan),
            "hours": "HOURS (placeholder: the server inserts this plan's 24 hourly rows)"}


def t_compare(state: dict, date: str | None = None, reserve_pct: float = 10, cycles: float = 1.0) -> dict:
    d, rows = _da(date)
    b, o = optimize.arb_baseline(rows), optimize.arb_optimal(rows, reserve_pct=reserve_pct, cycles=cycles)
    sb, so = optimize.arb_simulate(b), optimize.arb_simulate(o)
    state["HOURS"] = _hours(o)
    return {"date": d, "baseline_rule": "charge 01:00-05:00, discharge 17:00-21:00 at full power",
            "baseline": {k: sb[k] for k in ("profit_usd", "revenue_usd", "cost_usd", "cycles")} | _summary(b),
            "optimal": {k: so[k] for k in ("profit_usd", "revenue_usd", "cost_usd", "cycles")}
            | {kk: vv for kk, vv in _summary(o).items() if kk != "price_by_hour_usd_mwh"},
            "uplift_usd": round(so["profit_usd"] - sb["profit_usd"], 2)}


def t_backtest(state: dict, days: int = 7) -> dict:
    days = max(1, min(int(days), 30))
    r = optimize.arb_backtest(days=days)
    state["DAYS"] = [{"date": x["date"], "profit": x["optimal_profit"], "baseline": x["baseline_profit"]} for x in r["days"]]
    return {"settled_on": "NYISO real-time prices (planned on day-ahead)", "totals": r["totals"],
            "days": [{"date": x["date"], "optimal_profit": x["optimal_profit"], "baseline_profit": x["baseline_profit"]}
                     for x in r["days"]],
            "note": "pass DAYS (bare placeholder) to BacktestBars"}


_D = {"type": "string", "description": "YYYY-MM-DD; omit for tomorrow's day-ahead market"}
TOOLS: dict[str, tuple[Any, str, dict]] = {
    "get_prices": (t_get_prices, "Hourly NYISO N.Y.C. day-ahead prices ($/MWh) for a date.", {"date": _D}),
    "plan": (t_plan, "Optimal battery trading plan (MILP) for a date under constraints, replayed in an independent "
                     "simulation (Daytona) with safety checks. Use for any what-if (reserve, half cycle, no selling before X).",
             {"date": _D, "reserve_pct": {"type": "number", "description": "minimum state of charge kept in reserve, % (default 10)"},
              "cycles": {"type": "number", "description": "max full cycles today, e.g. 0.5 for half a cycle (default 1)"},
              "no_discharge_before": {"type": "integer", "description": "hour 0-23; no selling before it"},
              "no_charge_after": {"type": "integer", "description": "hour 0-23; no buying from it on"}}),
    "compare": (t_compare, "Simple clock rule (buy 1-5am, sell 5-9pm) vs the optimal plan for one date, on day-ahead prices.",
                {"date": _D, "reserve_pct": {"type": "number"}, "cycles": {"type": "number"}}),
    "backtest": (t_backtest, "Last N days (ending yesterday): plan on day-ahead, settle on real-time; per-day profit of the "
                             "optimal plan vs the simple rule, plus totals.", {"days": {"type": "integer", "description": "1-30, default 7"}}),
}
STATUS = {"get_prices": "reading NYISO day-ahead prices...", "plan": "optimizing the trading plan and simulating it...",
          "compare": "comparing against the simple clock rule...", "backtest": "backtesting on real-time prices..."}


def _tool_config() -> dict:
    return {"tools": [{"toolSpec": {"name": n, "description": d,
                                    "inputSchema": {"json": {"type": "object", "properties": p}}}}
                      for n, (_, d, p) in TOOLS.items()]}


# ------------------------------------------------------------------ prompts

PLAN_PROMPT = """You are the trader for a grid battery in New York City: 10 MW / 40 MWh, 85% round trip efficiency,
SOC 10% to 90%, starts and ends the day at 50%, at most 1 full cycle per day, $8/MWh degradation, never charges and
discharges in the same hour. It buys power when NYISO N.Y.C. prices are low and sells when high.
Answer the operator's question by calling tools. For a what-if, call plan with the default constraints AND plan with
the changed constraints so the difference can be shown. For "why not sell at X", call plan and get_prices.
For "last week vs the simple rule", call backtest(7). Call 1 to 4 tools, then reply "done" (the UI is drawn next).
"""

RENDER_PROMPT = """You render the trader's answer as a generative UI. The tool results below are your ONLY source of
numbers: never invent, extrapolate or round wildly. Dollar amounts are USD, prices are $/MWh, hours are 0-23.

Streaming layout (REQUIRED): line 1 is `root = Stack([headline, ...])` using ONLY reference names; then define each
referenced component on its OWN line in the same order. Never nest component calls inline inside root.
Signatures (positional, in this order): ProfitHeadline(value, label, summary); BuySellChart(title, hours);
SafetyChecks(items); Compare(title, rows); BacktestBars(title, days); Narrative(text); Evidence(text).
Placeholders (bare words, never quoted, never typed out yourself): HOURS = the last plan's 24 hourly rows, use as
BuySellChart("title", HOURS). DAYS = the backtest days, use as BacktestBars("title", DAYS). CHECKS = the last plan's
safety checks, use as SafetyChecks(CHECKS).
Typical answer: ProfitHeadline first; then BuySellChart (when a plan was made); Compare for what-if or baseline
deltas (rows with label, value, unit "usd" | "pct" | "mwh" | "usd_mwh" | "cycles" | "hour"); BacktestBars for history;
SafetyChecks when a plan was made; a one or two sentence Narrative; Evidence last (data source and where it was
simulated, e.g. "NYISO N.Y.C. day-ahead, Oct 17; simulated in Daytona sandbox abc").
Style: plain punctuation, never em dashes or en dashes. Hours as "3pm". Keep it compact: 4 to 7 blocks.
Output ONLY the OpenUI Lang program: no prose, no markdown, no code fences.

"""


# ------------------------------------------------------------------ bedrock


def _client():
    import boto3

    return boto3.client("bedrock-runtime", region_name=REGION, aws_access_key_id=env("AWS_ACCESS_KEY_ID"),
                        aws_secret_access_key=env("AWS_SECRET_ACCESS_KEY"), aws_session_token=env("AWS_SESSION_TOKEN"))


def _converse(c, **kw) -> dict:
    with _ag("inference", MODEL_ID, group="bedrock", kind="llm"), \
            _tracer.start_as_current_span(f"llm {MODEL_ID}", attributes={
                "openinference.span.kind": "LLM", "llm.model_name": MODEL_ID, "llm.provider": "bedrock"}) as sp:
        r = c.converse(modelId=MODEL_ID, **kw)
        u = r.get("usage", {})
        sp.set_attribute("llm.token_count.prompt", u.get("inputTokens") or 0)
        sp.set_attribute("llm.token_count.completion", u.get("outputTokens") or 0)
        return r


# ------------------------------------------------------------------ OpenUI statement streaming

_STMT = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(\S.*)$", re.S)


class StatementParser:
    """Incremental: feed text deltas, get complete `id = Comp(...)` statements (balanced brackets, outside strings)."""

    def __init__(self):
        self.buf, self.depth, self.quote, self.esc = "", 0, None, False

    def feed(self, text: str) -> list[tuple[str, str]]:
        out = []
        for ch in text:
            if ch == "\n" and self.depth <= 0 and self.quote is None:
                out += self._flush()
                continue
            self.buf += ch
            if self.quote:
                if self.esc:
                    self.esc = False
                elif ch == "\\":
                    self.esc = True
                elif ch == self.quote:
                    self.quote = None
            elif ch in "\"'":
                self.quote = ch
            elif ch in "([{":
                self.depth += 1
            elif ch in ")]}":
                self.depth -= 1
        return out

    def _flush(self) -> list[tuple[str, str]]:
        s, self.buf, self.depth, self.quote, self.esc = self.buf.strip(), "", 0, None, False
        if not s or s.startswith("```"):
            return []
        m = _STMT.match(s)
        return [(m.group(1), s)] if m else []

    def close(self) -> list[tuple[str, str]]:
        return self._flush()


def _subst(line: str, state: dict) -> str:
    """Replace bare placeholders (HOURS, DAYS, CHECKS) outside strings with the server-held JSON arrays."""
    keys = [k for k in ("HOURS", "DAYS", "CHECKS") if k in line]
    if not keys:
        return line
    out, i, quote = [], 0, None
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 1
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        hit = next((k for k in keys if line.startswith(k, i) and not (i and (line[i - 1].isalnum() or line[i - 1] == "_"))
                    and not line[i + len(k):i + len(k) + 1].isalnum()), None)
        if hit:
            out.append(json.dumps(state.get(hit, []), separators=(", ", ": ")))
            i += len(hit)
            continue
        out.append(ch)
        i += 1
    return "".join(out).replace("—", ", ").replace("–", " to ")


# ------------------------------------------------------------------ pipeline


def _run(q: str, system_prompt: str, emit) -> list[str]:
    c = _client()
    state: dict = {}
    used: list[str] = []
    results: list[dict] = []
    today = date.today()
    ctx = f"\nToday is {today.isoformat()} ({today.strftime('%A')}); tomorrow is {(today + timedelta(days=1)).isoformat()}."
    with _ag("agent", "trader-ask", task=q[:120]), _tracer.start_as_current_span("trader-ask", attributes={
            "openinference.span.kind": "AGENT", "agent.name": "trader-ask", "input.value": q[:2000]}) as root_sp:
        emit({"kind": "status", "text": "reading the question..."})
        msgs: list[dict] = [{"role": "user", "content": [{"text": q}]}]
        for _ in range(MAX_TURNS):
            r = _converse(c, system=[{"text": PLAN_PROMPT + ctx}], messages=msgs, toolConfig=_tool_config(),
                          inferenceConfig={"maxTokens": 800})
            m = r["output"]["message"]
            msgs.append(m)
            calls = [b["toolUse"] for b in m["content"] if "toolUse" in b]
            if not calls:
                break
            reply = []
            for tu in calls:
                name, args = tu["name"], tu.get("input") or {}
                emit({"kind": "status", "text": STATUS.get(name, name + "...")})
                used.append(name)
                try:
                    fn = TOOLS[name][0]
                    with _tool_span(name, args) as sp:
                        res = fn(state, **args)
                        if sp is not None:
                            sp.set_attribute("output.value", json.dumps(res, default=str)[:4000])
                    results.append({"tool": name, "args": args, "result": res})
                    reply.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": json.loads(json.dumps(res, default=float))}]}})
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {str(e)[:200]}"
                    emit({"kind": "status", "text": f"{name} failed: {err}"})
                    reply.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"text": err}], "status": "error"}})
            msgs.append({"role": "user", "content": reply})
        emit({"kind": "status", "text": "drawing your answer..."})
        user = (f"Question: {q}\n\nTool results (JSON):\n{json.dumps(results, default=str)}\n\n"
                "Write the OpenUI Lang program now.")
        parser, n = StatementParser(), 0
        with _ag("inference", MODEL_ID, group="bedrock", kind="llm"), _tracer.start_as_current_span(
                f"llm {MODEL_ID} render", attributes={"openinference.span.kind": "LLM", "llm.model_name": MODEL_ID}):
            r = c.converse_stream(modelId=MODEL_ID, system=[{"text": RENDER_PROMPT + (system_prompt or "") + ctx}],
                                  messages=[{"role": "user", "content": [{"text": user}]}],
                                  inferenceConfig={"maxTokens": 2500})
            for ev in r["stream"]:
                delta = ev.get("contentBlockDelta", {}).get("delta", {}).get("text")
                if delta:
                    for sid, line in parser.feed(delta):
                        emit({"kind": "ui", "id": sid, "line": _subst(line, state)})
                        n += 1
            for sid, line in parser.close():
                emit({"kind": "ui", "id": sid, "line": _subst(line, state)})
                n += 1
        if not n:
            emit({"kind": "error", "text": "the trader returned no UI"})
        root_sp.set_attribute("output.value", f"{len(used)} tool calls, {n} UI statements")
    return used


async def run_ask(q: str, system_prompt: str = "") -> AsyncIterator[dict]:
    t0 = time.monotonic()
    qu: queue.Queue = queue.Queue()
    used: list[str] = []

    def work():
        try:
            used.extend(_run(q, system_prompt, qu.put))
        except Exception as e:  # noqa: BLE001
            log.exception("ask failed")
            qu.put({"kind": "error", "text": f"{type(e).__name__}: {str(e)[:300]}"})
        finally:
            qu.put(None)

    threading.Thread(target=work, daemon=True).start()
    deadline = t0 + TIMEOUT_S
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            yield {"kind": "error", "text": f"timed out after {TIMEOUT_S}s"}
            break
        try:
            item = await asyncio.to_thread(qu.get, True, min(left, 1.0))
        except queue.Empty:
            continue
        if item is None:
            break
        yield item
    yield {"kind": "done", "meta": {"ms": int((time.monotonic() - t0) * 1000), "tools": used}}


class AskReq(BaseModel):
    q: str
    system_prompt: str | None = ""


@router.post("/api/ask")
async def ask(req: AskReq):
    async def gen():
        async for ev in run_ask(req.q, req.system_prompt or ""):
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
