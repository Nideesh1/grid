"""The dispatcher agent: plans tonight's depot charging and explains it.

Tool steps (each one an AgentGlow tool span under agent "dispatcher", and an OpenInference span for Arize):
get_prices -> get_depot_state (+ recall constraints) -> build_plan -> simulate_in_sandbox (Daytona) -> savings ->
safety checks -> draft message (LLM: Mistral, else Anthropic, else a grounded template) -> remember -> evals.

The numbers are always computed by tools; the LLM only writes 2-3 sentences + a short spoken message from them,
and the grounded_savings eval checks every $ figure it wrote against the simulation.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator

import httpx
from opentelemetry import trace

from grid import optimize, prices
from grid.config import env, has
from grid.depot import apply_constraint, make_depot
from grid.sponsors import arize, daytona_sim, memory, voice

log = logging.getLogger("grid.agent")
_tracer = trace.get_tracer("grid.agent")
PLANS: dict[str, dict] = {}
_llm_state: dict = {"provider": None, "error": None}

PROMPTS = {
    "v1": (
        "You are the overnight energy dispatcher for a 40-bus electric bus depot in New York City. "
        "Using ONLY the numbers in the tool results, write JSON with two fields: "
        "\"explanation\": 2-3 plain sentences for the operations dashboard explaining when the buses charge, why "
        "(NYISO N.Y.C. day-ahead prices), the savings tonight and per month, and that the safety simulation passed; "
        "\"message\": one or two short spoken sentences for the night dispatcher ending with a yes/no approval question. "
        "Copy dollar figures exactly as given (rounded to whole dollars is fine). Never invent numbers. No em dashes."
    ),
    "v2": (
        "You are the overnight energy dispatcher for a 40-bus electric bus depot in New York City. "
        "Return JSON {\"explanation\": str, \"message\": str}. explanation: exactly 3 sentences: (1) the charging "
        "window and peak kW vs the site cap, (2) the dollar savings tonight and per month copied verbatim from "
        "savings.tonight_usd and savings.monthly_usd with a $ sign, (3) the safety result: the number of violations "
        "from the Daytona simulation and the minimum departure state of charge. message: under 30 words, spoken, "
        "states tonight's savings in dollars and asks the dispatcher to approve, yes or no. Use only numbers present "
        "in the input. No em dashes."
    ),
}
DEFAULT_PROMPT = "v1"


# ------------------------------------------------------------------ tracing helpers
@contextmanager
def _tool(name: str, args: dict | None = None):
    """AgentGlow tool span + OpenInference TOOL span (same OTel span when both are on one provider)."""
    try:
        import agentglow

        cm = agentglow.tool(name, args=args or {})
    except Exception:  # noqa: BLE001
        cm = None
    with (cm if cm is not None else _null()):
        with _tracer.start_as_current_span(f"tool {name}", attributes={
                "openinference.span.kind": "TOOL", "tool.name": name,
                "input.value": json.dumps(args or {}, default=str)[:2000]}) as sp:
            yield sp


@contextmanager
def _null():
    yield None


def _set_output(sp, value) -> None:
    if sp is not None:
        try:
            sp.set_attribute("output.value", (value if isinstance(value, str) else json.dumps(value, default=str))[:4000])
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ LLM
def llm_status() -> dict:
    prov = "mistral" if has("MISTRAL_API_KEY") else "anthropic" if has("ANTHROPIC_API_KEY") else "template"
    return {"configured": prov, "last_provider": _llm_state["provider"], "error": _llm_state["error"]}


def _mistral(system: str, user: str) -> tuple[str, dict]:
    from mistralai.client import Mistral

    c = Mistral(api_key=env("MISTRAL_API_KEY"))
    r = c.chat.complete(model=env("MISTRAL_MODEL", "mistral-large-4"), reasoning_effort="none",
                        response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    u = r.usage
    return r.choices[0].message.content, {"in": getattr(u, "prompt_tokens", None), "out": getattr(u, "completion_tokens", None)}


def _anthropic(system: str, user: str) -> tuple[str, dict]:
    r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
        "x-api-key": env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": env("ANTHROPIC_MODEL", "claude-sonnet-4-5"), "max_tokens": 600, "system": system,
              "messages": [{"role": "user", "content": user + "\nReturn only the JSON object."}]})
    r.raise_for_status()
    j = r.json()
    return "".join(b.get("text", "") for b in j["content"]), {"in": j["usage"]["input_tokens"], "out": j["usage"]["output_tokens"]}


def _template(facts: dict) -> dict:
    s, o = facts["savings"], facts["optimal"]
    win = facts.get("charging_window") or "overnight"
    expl = (f"Buses charge mainly {win}, when NYISO N.Y.C. day-ahead prices are lowest, peaking at {o['peak_kw']:,.0f} kW "
            f"against the {facts['cap_kw']:,.0f} kW site cap. That saves ${s['tonight_usd']:,.2f} tonight and "
            f"${s['monthly_usd']:,.2f} per month versus plugging every bus in at 7pm. The Daytona simulation found "
            f"{len(o['violations'])} safety violations and every bus leaves at {o['min_soc_at_departure']:.0%} or more.")
    msg = f"Tonight's charging plan saves ${s['tonight_usd']:,.0f} with all safety checks passing. Approve, yes or no?"
    return {"explanation": expl, "message": msg}


def _clean(t: str) -> str:
    return (t or "").replace("\u2014", ", ").replace("\u2013", "-").strip()


def draft(facts: dict, prompt_version: str = DEFAULT_PROMPT) -> dict:
    """LLM explanation + dispatcher message from tool facts. Provider chain: Mistral -> Anthropic -> template."""
    system = PROMPTS.get(prompt_version, PROMPTS[DEFAULT_PROMPT])
    user = json.dumps(facts, default=str)
    chain = [(n, f) for n, f, k in (("mistral", _mistral, "MISTRAL_API_KEY"), ("anthropic", _anthropic, "ANTHROPIC_API_KEY")) if has(k)]
    for name, fn in chain:
        model = env("MISTRAL_MODEL", "mistral-large-4") if name == "mistral" else env("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        try:
            import agentglow

            inf = agentglow.inference(model, group=name, kind="llm")
        except Exception:  # noqa: BLE001
            inf = _null()
        try:
            with inf, _tracer.start_as_current_span(f"llm {model}", attributes={
                    "openinference.span.kind": "LLM", "llm.model_name": model, "llm.provider": name,
                    "llm.prompt_template.version": prompt_version, "input.value": user[:4000],
                    "llm.input_messages.0.message.role": "system", "llm.input_messages.0.message.content": system,
                    "llm.input_messages.1.message.role": "user", "llm.input_messages.1.message.content": user[:4000]}) as sp:
                text, usage = fn(system, user)
                sp.set_attribute("output.value", text[:4000])
                sp.set_attribute("llm.output_messages.0.message.role", "assistant")
                sp.set_attribute("llm.output_messages.0.message.content", text[:4000])
                if usage.get("in") is not None:
                    sp.set_attribute("llm.token_count.prompt", usage["in"])
                    sp.set_attribute("llm.token_count.completion", usage["out"])
            t = text.strip()
            if t.startswith("```"):
                t = t.strip("`").removeprefix("json").strip()
            out = json.loads(t[t.find("{"): t.rfind("}") + 1])
            _llm_state.update(provider=name, error=None)
            return {"explanation": _clean(out["explanation"]), "message": _clean(out["message"]), "provider": name,
                    "model": model, "prompt_version": prompt_version}
        except Exception as e:  # noqa: BLE001
            _llm_state["error"] = f"{name}: {type(e).__name__}: {str(e)[:120]}"
            log.warning("llm %s failed: %s", name, _llm_state["error"])
    _llm_state["provider"] = "template"
    return {**_template(facts), "provider": "template", "model": None, "prompt_version": "template"}


# ------------------------------------------------------------------ helpers
def _night_date(d) -> date:
    if d:
        return d if isinstance(d, date) else date.fromisoformat(str(d))
    return date.today()


def _hourly(rows: list[dict]) -> list[dict]:
    hours: dict[str, list[float]] = {}
    for p in rows:
        ts = datetime.fromisoformat(p["ts"]).replace(minute=0, second=0)
        hours.setdefault(ts.isoformat(), []).append(float(p["usd_mwh"]))
    return [{"ts": k, "hour": k[11:16], "usd_mwh": round(sum(v) / len(v), 2)} for k, v in hours.items()]


def _window(plan: dict) -> str:
    """Hours carrying >= 50% of the plan's peak, e.g. '01:00 to 05:00'."""
    sch = plan["schedule"]
    peak = max((h["kw"] for h in sch), default=0)
    on = [h["hour"] for h in sch if peak and h["kw"] >= 0.5 * peak]
    if not on:
        return "overnight"
    last = datetime.strptime(on[-1], "%H:00") + timedelta(hours=1)
    return f"{on[0]} to {last:%H:%M}"


def _constraints(dep) -> list[dict]:
    active = memory.recall("constraint")
    for f in active:
        v = f["value"]
        try:
            apply_constraint(dep, int(v["bus_id"]), v.get("departure_hour"), v.get("arrival_hour"))
        except Exception:  # noqa: BLE001
            pass
    return active


def add_constraint(bus_id: int, departure_hour: float | None = None, arrival_hour: float | None = None,
                   source: str = "dispatcher") -> dict:
    """'bus 12 must leave at 04:00' -> departure_hour=28. Supersedes any earlier constraint for that bus."""
    return memory.remember("constraint", f"bus-{bus_id}", {"bus_id": bus_id, "departure_hour": departure_hour,
                                                           "arrival_hour": arrival_hour}, source)


# ------------------------------------------------------------------ the run
def run_plan(d=None, cap_kw: float | None = None, prompt_version: str = DEFAULT_PROMPT,
             speak: bool = True, kind: str = "da") -> Iterator[dict]:
    """Yields SSE-shaped events: status..., plan, done (or error)."""
    night = _night_date(d)
    plan_id = uuid.uuid4().hex[:10]
    t0 = time.time()
    request = {"night": night.isoformat(), "zone": "N.Y.C.", "cap_kw": cap_kw, "prompt_version": prompt_version}
    try:
        import agentglow

        agent_cm = agentglow.agent("dispatcher", task=f"plan charging for night of {night}")
    except Exception:  # noqa: BLE001
        agent_cm = _null()
    with agent_cm, _tracer.start_as_current_span("dispatcher", attributes={
            "openinference.span.kind": "AGENT", "agent.name": "dispatcher", "grid.plan_id": plan_id,
            "input.value": json.dumps(request), "input.mime_type": "application/json",
            "session.id": plan_id}) as root:
        try:
            yield {"kind": "status", "text": "fetching NYISO day-ahead prices for N.Y.C."}
            with _tool("get_prices", {"date": night.isoformat(), "kind": kind}) as sp:
                rows = prices.night(night, kind)
                source = "NYISO day-ahead LBMP, zone N.Y.C." if kind == "da" else "NYISO real-time LBMP, zone N.Y.C."
                if not rows and kind == "da":
                    rows, source = prices.night(night - timedelta(days=1), "rt"), "NYISO real-time LBMP (last night), zone N.Y.C."
                if not rows:
                    raise RuntimeError(f"no NYISO prices for {night}")
                proxy = any(r.get("proxy") for r in rows)
                hourly = _hourly(rows)
                _set_output(sp, {"rows": len(rows), "proxy": proxy, "min": min(h["usd_mwh"] for h in hourly),
                                 "max": max(h["usd_mwh"] for h in hourly)})
            with _tool("remember", {"kind": "price_forecast", "hours": len(hourly)}):
                for h in hourly:  # each new forecast for an hour supersedes the previous one
                    memory.remember("price_forecast", h["ts"], {"usd_mwh": h["usd_mwh"]}, source)

            with _tool("get_depot_state", {"date": night.isoformat()}) as sp:
                dep = make_depot(night)
                constraints = _constraints(dep)
                cap = float(cap_kw or dep.site_cap_kw)
                _set_output(sp, {"buses": len(dep.buses), "energy_needed_kwh": round(dep.energy_needed_kwh()),
                                 "constraints": len(constraints)})

            yield {"kind": "status", "text": "building optimal schedule"}
            with _tool("build_plan", {"cap_kw": cap}) as sp:
                base_plan = optimize.baseline(dep, rows, cap)
                opt_plan = optimize.optimal(dep, rows, cap)
                _set_output(sp, {"status": opt_plan["status"], "solve_s": opt_plan["solve_s"]})
            if opt_plan["status"] != "ok":
                raise RuntimeError(f"optimizer: {opt_plan['status']}")

            sid = daytona_sim.sandbox_id()
            yield {"kind": "status", "text": f"simulating in Daytona sandbox {sid}" if sid else "simulating locally (Daytona disabled)"}
            with _tool("simulate_in_sandbox", {"plan": "optimal", "sandbox": sid}) as sp:
                sim_opt = daytona_sim.simulate_in_sandbox(opt_plan, dep, rows)
                _set_output(sp, {k: sim_opt.get(k) for k in ("where", "sandbox_id", "cost_usd", "peak_kw", "violations")})
            sim_base = optimize.simulate(base_plan, dep, rows)
            with _tool("savings") as sp:
                sav = optimize.savings(sim_base, sim_opt)
                _set_output(sp, sav)

            yield {"kind": "status", "text": "checking safety limits"}
            with _tool("check_safety") as sp:
                checks = [
                    {"name": f"all buses >= {dep.target_soc:.0%} by 05:00", "ok": sim_opt["min_soc_at_departure"] >= dep.target_soc - 1e-4,
                     "detail": f"min departure SOC {sim_opt['min_soc_at_departure']:.1%}"},
                    {"name": f"peak <= site cap {cap:,.0f} kW", "ok": sim_opt["peak_kw"] <= cap + 1e-3,
                     "detail": f"peak {sim_opt['peak_kw']:,.0f} kW"},
                    {"name": "per-charger limit 150 kW", "ok": not any("charger" in v for v in sim_opt["violations"]),
                     "detail": "no charger over limit"},
                    {"name": "simulated in Daytona" if sim_opt["where"] == "daytona" else "simulated locally (Daytona off)",
                     "ok": not sim_opt["violations"], "detail": sim_opt.get("sandbox_id") or "local subprocess",
                     "sandbox_id": sim_opt.get("sandbox_id")},
                ]
                _set_output(sp, checks)

            yield {"kind": "status", "text": "drafting dispatcher message"}
            facts = {"night": night.isoformat(), "price_source": source, "cap_kw": cap,
                     "charging_window": _window(opt_plan),
                     "cheapest_hour": min(hourly, key=lambda h: h["usd_mwh"]),
                     "priciest_hour": max(hourly, key=lambda h: h["usd_mwh"]),
                     "baseline": {k: sim_base[k] for k in ("cost_usd", "peak_kw", "kwh", "avg_usd_mwh")},
                     "optimal": {k: sim_opt[k] for k in ("cost_usd", "peak_kw", "kwh", "avg_usd_mwh", "min_soc_at_departure", "violations")},
                     "savings": sav, "simulated_in": sim_opt["where"], "constraints": [c["value"] for c in constraints]}
            text = draft(facts, prompt_version)

            evals = arize.eval_plan(text["message"] + " " + text["explanation"], sim_opt,
                                    {**sav, "baseline_cost_usd": sim_base["cost_usd"], "optimal_cost_usd": sim_opt["cost_usd"]})
            arize.annotate(root, evals)

            plan = {
                "plan_id": plan_id, "date": night.isoformat(), "price_source": source, "proxy_prices": proxy,
                "prices": hourly, "cap_kw": cap,
                "baseline": {**sim_base, "schedule": base_plan["schedule"]}, "optimal": {**sim_opt, "schedule": opt_plan["schedule"]},
                "schedule": opt_plan["schedule"], "baseline_schedule": base_plan["schedule"],
                "savings": sav, "checks": checks, "charging_window": facts["charging_window"],
                "explanation": text["explanation"], "message": text["message"], "llm": text.get("provider"),
                "model": text.get("model"), "prompt_version": text.get("prompt_version"),
                "sandbox_id": sim_opt.get("sandbox_id"), "simulated_in": sim_opt["where"],
                "constraints": [c["value"] for c in constraints], "evals": evals,
                "voice_url": None, "audio_url": None, "voice_text": text["message"], "status": "pending_approval", "elapsed_s": round(time.time() - t0, 2),
            }
            PLANS[plan_id] = plan
            with _tool("remember", {"kind": "plan"}):
                memory.remember("plan", night.isoformat(), {"plan_id": plan_id, "tonight_usd": sav["tonight_usd"],
                                                            "monthly_usd": sav["monthly_usd"], "status": "pending_approval"},
                                f"dispatcher:{text.get('provider')}")
            if speak and voice.enabled():
                with _tool("request_approval", {"channel": "voice"}):
                    if voice.synthesize(plan_id, text["message"]):
                        plan["voice_url"] = plan["audio_url"] = f"/api/voice/{plan_id}"
            root.set_attribute("output.value", text["message"])
            root.set_attribute("output.mime_type", "text/plain")
            root.set_attribute("grid.savings.tonight_usd", sav["tonight_usd"])
            root.set_attribute("grid.violations", len(sim_opt["violations"]))
            yield {"kind": "plan", **plan}
            yield {"kind": "done", "plan_id": plan_id}
        except Exception as e:  # noqa: BLE001
            log.exception("plan failed")
            root.set_status(trace.Status(trace.StatusCode.ERROR, str(e)[:200]))
            yield {"kind": "error", "text": f"{type(e).__name__}: {str(e)[:200]}"}
            yield {"kind": "done", "plan_id": None}


def approve(plan_id: str, approved: bool, who: str = "dispatcher") -> dict:
    plan = PLANS.get(plan_id)
    if plan is None:
        hist = [f for f in memory.recall("plan", history=True) if f["value"].get("plan_id") == plan_id]
        if not hist:
            raise KeyError(plan_id)
        plan = {"plan_id": plan_id, "date": hist[0]["key"], "savings": {"tonight_usd": hist[0]["value"].get("tonight_usd"),
                                                                         "monthly_usd": hist[0]["value"].get("monthly_usd")}}
    status = "approved" if approved else "rejected"
    plan["status"] = status
    with _tracer.start_as_current_span("approval", attributes={"grid.plan_id": plan_id, "grid.approved": approved}):
        try:
            import agentglow

            agentglow.event("plan_" + status, label=plan_id)
        except Exception:  # noqa: BLE001
            pass
        fact = memory.remember("plan", plan["date"], {"plan_id": plan_id, "status": status,
                                                      "tonight_usd": plan["savings"]["tonight_usd"],
                                                      "monthly_usd": plan["savings"]["monthly_usd"]}, f"human:{who}")
    return {"plan_id": plan_id, "status": status, "fact_id": fact["id"], "date": plan["date"]}
