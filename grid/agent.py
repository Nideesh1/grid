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
import re
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator

import httpx
from opentelemetry import trace

from grid import optimize, prices
from grid.config import env, has
from grid.sponsors import anchor, arize, daytona_sim, memory, voice

log = logging.getLogger("grid.agent")
_tracer = trace.get_tracer("grid.agent")
PLANS: dict[str, dict] = {}
_llm_state: dict = {"provider": None, "error": None}

PROMPTS = {
    "v1": (
        "You are the AI trader for a 10 MW / 40 MWh grid battery in New York City. It buys power when NYISO N.Y.C. "
        "day-ahead prices are cheap, stores it, and sells when prices are high, inside hard safety limits, and a human "
        "dispatcher approves. Using ONLY the numbers in the tool results, write JSON with two fields: "
        "\"explanation\": 2-3 plain sentences for the dashboard explaining when it buys and sells and why, the expected "
        "profit, how it compares with the naive clock rule, and that the safety simulation passed; "
        "\"message\": one or two short spoken sentences for the dispatcher ending with a yes/no approval question. "
        "Never invent numbers. No em dashes."
    ),
    "v2": (
        "You are the AI trader for a 10 MW / 40 MWh grid battery in New York City. Return JSON "
        "{\"explanation\": str, \"message\": str}. explanation: exactly 3 sentences: (1) the buy and sell windows from "
        "buy_windows and sell_windows and why (cheapest vs priciest hour, quoting prices as $X/MWh), (2) the profit "
        "copied from optimal.profit_usd and the uplift copied from uplift_vs_naive_usd, both with a $ sign, "
        "(3) the safety result: number of violations from the Daytona simulation and the cycles used. message: under "
        "30 words, spoken, in this form: \"Tomorrow I'll buy <buy window> and sell <sell window>, profit $<profit "
        "rounded to dollars>. Approve?\" Use only numbers present in the input. No em dashes."
    ),
}
DEFAULT_PROMPT = "v2"


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
BEDROCK_FALLBACK = ["us.anthropic.claude-sonnet-5-5", "us.anthropic.claude-sonnet-5",
                    "us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
BEDROCK_JUDGE = "us.anthropic.claude-haiku-5-5"
_bedrock: dict = {"model": None, "candidates": None}


def _chain() -> list[str]:
    out = []
    if has("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        out.append("bedrock")
    if has("MISTRAL_API_KEY"):
        out.append("mistral")
    if has("ANTHROPIC_API_KEY"):
        out.append("anthropic")
    return out + ["template"]


def llm_status() -> dict:
    return {"configured": _chain()[0], "chain": _chain(), "last_provider": _llm_state["provider"],
            "bedrock_model": _bedrock["model"], "error": _llm_state["error"]}


def _boto(service: str):
    import boto3

    return boto3.client(service, region_name=env("AWS_REGION", "us-east-1"), aws_access_key_id=env("AWS_ACCESS_KEY_ID"),
                        aws_secret_access_key=env("AWS_SECRET_ACCESS_KEY"), aws_session_token=env("AWS_SESSION_TOKEN"))


def _bedrock_candidates() -> list[str]:
    """Newest Claude Sonnet inference profiles in the region first, then a static fallback list."""
    if _bedrock["candidates"] is None:
        found: list[str] = []
        try:
            if not env("BEDROCK_DISCOVER"):
                raise RuntimeError("discovery off")
            r = _boto("bedrock").list_inference_profiles(maxResults=1000)
            ids = [p["inferenceProfileId"] for p in r.get("inferenceProfileSummaries", [])]
            found = sorted((i for i in ids if "anthropic.claude" in i and "sonnet" in i and i.startswith(("us.", "global."))),
                           key=lambda i: re.sub(r"[^0-9]", "", i.split("sonnet")[-1])[:8] or "0", reverse=True)
        except Exception as e:  # noqa: BLE001
            log.info("bedrock list_inference_profiles unavailable: %s", type(e).__name__)
        _bedrock["candidates"] = list(dict.fromkeys(([env("BEDROCK_MODEL_ID")] if env("BEDROCK_MODEL_ID") else []) + found + BEDROCK_FALLBACK))
    return [m for m in _bedrock["candidates"] if m]


def _bedrock_call(system: str, user: str, model: str | None = None) -> tuple[str, dict]:
    c = _boto("bedrock-runtime")
    cands = [model] if model else [_bedrock["model"]] if _bedrock["model"] else _bedrock_candidates()
    last: Exception | None = None
    for mid in cands:
        try:
            r = c.converse(modelId=mid, system=[{"text": system}],
                           messages=[{"role": "user", "content": [{"text": user + "\nReturn only the JSON object."}]}],
                           inferenceConfig={"maxTokens": 1500})
            if not model:
                _bedrock["model"] = mid
            u = r.get("usage", {})
            return "".join(b.get("text", "") for b in r["output"]["message"]["content"]), {"in": u.get("inputTokens"), "out": u.get("outputTokens")}
        except Exception as e:  # noqa: BLE001
            last = e
            if _bedrock["model"]:
                _bedrock["model"] = None
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
            if code not in ("ValidationException", "AccessDeniedException", "ResourceNotFoundException"):
                raise
    raise last or RuntimeError("no bedrock model")


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
    o = facts["optimal"]
    buy = " and ".join(facts["buy_windows"]) or "no hours"
    sell = " and ".join(facts["sell_windows"]) or "no hours"
    expl = (f"The battery buys {buy}, when NYISO N.Y.C. day-ahead power is cheapest (${facts['cheapest_hour']['usd_mwh']:.2f}/MWh), "
            f"and sells {sell}, near the ${facts['priciest_hour']['usd_mwh']:.2f}/MWh peak. Expected profit is ${o['profit_usd']:,.2f}, "
            f"${facts['uplift_vs_naive_usd']:,.2f} more than the naive clock rule. The Daytona simulation found "
            f"{len(o['violations'])} safety violations using {o['cycles']:.2f} cycles.")
    msg = f"Tomorrow I'll buy {buy} and sell {sell}, profit ${o['profit_usd']:,.0f}. Approve?"
    return {"explanation": expl, "message": msg}


def _clean(t: str) -> str:
    return (t or "").replace("\u2014", ", ").replace("\u2013", "-").strip()


def draft(facts: dict, prompt_version: str = DEFAULT_PROMPT) -> dict:
    """LLM explanation + dispatcher message from tool facts. Provider chain: Mistral -> Anthropic -> template."""
    system = PROMPTS.get(prompt_version, PROMPTS[DEFAULT_PROMPT])
    user = json.dumps(facts, default=str)
    fns = {"bedrock": _bedrock_call, "mistral": _mistral, "anthropic": _anthropic}
    chain = [(n, fns[n]) for n in _chain() if n in fns]
    for name, fn in chain:
        model = {"bedrock": _bedrock["model"] or "claude-sonnet (bedrock)", "mistral": env("MISTRAL_MODEL", "mistral-large-4"),
                 "anthropic": env("ANTHROPIC_MODEL", "claude-sonnet-4-5")}[name]
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
def _trade_date(d) -> date:
    if d:
        return d if isinstance(d, date) else date.fromisoformat(str(d))
    return date.today() + timedelta(days=1)  # trade tomorrow on its day-ahead prices (published ~11am)


def _fmt_h(h: int) -> str:
    h %= 24
    return "midnight" if h == 0 else "noon" if h == 12 else f"{h % 12}{'am' if h < 12 else 'pm'}"


def _windows(hours: list[dict], field: str) -> list[str]:
    """Contiguous hour ranges with activity, e.g. ['2am to 5am']."""
    out, start, prev = [], None, None
    for h in hours:
        on = h[field] > 1e-3
        if on and start is None:
            start = h["hour"]
        if not on and start is not None:
            out.append(f"{_fmt_h(start)} to {_fmt_h(prev + 1)}")
            start = None
        prev = h["hour"]
    if start is not None:
        out.append(f"{_fmt_h(start)} to {_fmt_h(prev + 1)}")
    return out


DEFAULT_CONSTRAINTS = {"reserve_pct": 10.0, "cycles": 1.0, "no_discharge_before": None, "no_charge_after": None}


def active_constraints() -> dict:
    c = dict(DEFAULT_CONSTRAINTS)
    for f in memory.recall("constraint"):
        if f["key"] in c:
            v = f["value"].get("value") if isinstance(f["value"], dict) else f["value"]
            c[f["key"]] = DEFAULT_CONSTRAINTS[f["key"]] if v is None and f["key"] in ("reserve_pct", "cycles") else v
    return c


def parse_constraint(text: str) -> dict | None:
    """Operator constraint text -> {key: value}. Examples: 'keep 30% in reserve for the storm', 'only half a cycle
    today', "don't sell before 6pm", "don't buy after 8pm", 'reset constraints'."""
    t = (text or "").lower()
    if re.search(r"\b(reset|clear|remove|default)\b", t):
        return dict(DEFAULT_CONSTRAINTS)
    out: dict = {}
    m = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", t)
    if m and re.search(r"reserve|keep|hold|floor|minimum", t):
        out["reserve_pct"] = float(m.group(1))
    if "cycle" in t:
        words = {"half": 0.5, "quarter": 0.25, "one": 1.0, "a full": 1.0, "full": 1.0, "zero": 0.0, "no": 0.0}
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:full\s*)?cycles?", t)
        if m:
            out["cycles"] = float(m.group(1))
        else:
            for w, v in words.items():
                if re.search(rf"\b{w}\b[^.]*cycle", t):
                    out["cycles"] = v
                    break

    def hour_after(word_re: str) -> int | None:
        m2 = re.search(word_re + r"\s*(\d{1,2})(?::\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?", t)
        if not m2:
            return None
        h = int(m2.group(1))
        ap = (m2.group(2) or "").replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if not ap and h < 8:  # bare "before 6" in a trading context means the evening
            h += 12
        return h

    if re.search(r"(sell|discharg)", t):
        h = hour_after(r"(?:before|until|till)")
        if h is not None:
            out["no_discharge_before"] = h
    if re.search(r"(buy|charg)", t) and re.search(r"after|from", t):
        h = hour_after(r"(?:after|from)")
        if h is not None:
            out["no_charge_after"] = h
    return out or None


def add_constraint(key: str, value, text: str, source: str = "operator") -> dict:
    return memory.remember("constraint", key, {"value": value, "text": text}, source)


# ------------------------------------------------------------------ the run
def run_plan(d=None, constraint_overrides: dict | None = None, prompt_version: str = DEFAULT_PROMPT,
             speak: bool = True, anchor_get=None, **_ignored) -> Iterator[dict]:
    """Battery arbitrage for one trading day. Yields SSE-shaped events: status..., plan, done (or error)."""
    day = _trade_date(d)
    plan_id = uuid.uuid4().hex[:10]
    t0 = time.time()
    try:
        import agentglow

        agent_cm = agentglow.agent("dispatcher", task=f"battery trading plan for {day}")
    except Exception:  # noqa: BLE001
        agent_cm = _null()
    with agent_cm, _tracer.start_as_current_span("dispatcher", attributes={
            "openinference.span.kind": "AGENT", "agent.name": "dispatcher", "grid.plan_id": plan_id,
            "input.mime_type": "application/json", "session.id": plan_id}) as root:
        try:
            yield {"kind": "status", "text": "fetching NYISO day-ahead prices for N.Y.C."}
            with _tool("get_prices", {"date": day.isoformat(), "kind": "day-ahead"}) as sp:
                da = prices.day_ahead(day)
                source = f"NYISO day-ahead LBMP, zone N.Y.C., {day}"
                if len(da) < 20 and not d:  # tomorrow not published yet: trade today
                    day = date.today()
                    da = prices.day_ahead(day)
                    source = f"NYISO day-ahead LBMP, zone N.Y.C., {day}"
                if len(da) < 20:
                    raise RuntimeError(f"no NYISO day-ahead prices for {day}")
                hp = optimize.hourly_prices(da)
                _set_output(sp, {"hours": len(hp), "min": min(h["usd_mwh"] for h in hp), "max": max(h["usd_mwh"] for h in hp)})
            with _tool("remember", {"kind": "price_forecast", "hours": len(hp)}):
                memory.remember_forecasts(hp, source)

            with _tool("recall", {"q": "active operator constraints + what changed since last night"}) as sp:
                cons = {**active_constraints(), **(constraint_overrides or {})}
                changed = [c for c in memory.changes_since(24) if not c.startswith("price_forecast")][:4]
                _set_output(sp, {"constraints": cons, "changes": changed})
            request = {"trading_day": day.isoformat(), "zone": "N.Y.C.", "battery": "10 MW / 40 MWh", "constraints": cons,
                       "prompt_version": prompt_version}
            root.set_attribute("input.value", json.dumps(request))

            yield {"kind": "status", "text": "building optimal buy/sell schedule"}
            with _tool("build_plan", cons) as sp:
                base = optimize.arb_baseline(da)
                opt = optimize.arb_optimal(da, reserve_pct=float(cons["reserve_pct"] or 10), cycles=float(cons["cycles"] if cons["cycles"] is not None else 1.0),
                                           no_discharge_before=cons.get("no_discharge_before"), no_charge_after=cons.get("no_charge_after"))
                _set_output(sp, {"status": opt["status"], "expected_profit_usd": opt["expected_profit_usd"]})
            if opt["status"] != "ok":
                raise RuntimeError(f"optimizer: {opt['status']}")

            sid = daytona_sim.sandbox_id()
            yield {"kind": "status", "text": f"simulating in Daytona sandbox {sid}" if sid else "simulating locally (Daytona disabled)"}
            with _tool("simulate_in_sandbox", {"plan": "optimal", "sandbox": sid}) as sp:
                ver = daytona_sim.call("arb_simulate", opt, None)
                sim = ver["result"]
                _set_output(sp, {"where": ver["where"], "sandbox_id": ver["sandbox_id"], **{k: sim[k] for k in ("profit_usd", "cycles", "violations")}})
            sim_base = optimize.arb_simulate(base, None)

            yield {"kind": "status", "text": "checking safety limits"}
            with _tool("check_safety") as sp:
                bat = opt["battery"]
                floor = min(max(bat["soc_min_pct"], float(cons["reserve_pct"] or 0)), bat["start_soc_pct"])
                v = sim["violations"]
                checks = [
                    {"name": f"SOC within {floor:.0f}% to {bat['soc_max_pct']:.0f}%", "ok": not any("SOC" in x and "end" not in x for x in v),
                     "detail": f"min {sim['min_soc']:.0f}%, max {sim['max_soc']:.0f}%"},
                    {"name": f"at most {cons['cycles']:g} cycle per day", "ok": not any("cycles" in x for x in v),
                     "detail": f"{sim['cycles']:.2f} cycles"},
                    {"name": "never charge and discharge in the same hour", "ok": not any("simultaneous" in x for x in v), "detail": "0 overlaps" if not any("simultaneous" in x for x in v) else "overlap"},
                    {"name": f"end SOC restored to {bat['end_soc_pct']:.0f}%", "ok": not any("end SOC" in x for x in v), "detail": f"end {sim['end_soc']:.1f}%"},
                    {"name": "simulated in Daytona" if ver["where"] == "daytona" else "simulated locally (Daytona off)",
                     "ok": not v, "detail": ver["sandbox_id"] or "local subprocess", "sandbox_id": ver["sandbox_id"]},
                ]
                if cons.get("no_discharge_before") is not None:
                    checks.append({"name": f"no selling before {_fmt_h(int(cons['no_discharge_before']))}",
                                   "ok": not any("discharge before" in x for x in v), "detail": "operator constraint"})
                row = anchor_get(8.0) if anchor_get else None
                if row:
                    checks.append(row)
                _set_output(sp, checks)

            yield {"kind": "status", "text": "drafting dispatcher message"}
            buy, sell = _windows(opt["hours"], "charge_mw"), _windows(opt["hours"], "discharge_mw")
            facts = {"trading_day": day.isoformat(), "price_source": source, "battery": "10 MW / 40 MWh, 85% round trip",
                     "buy_windows": buy, "sell_windows": sell,
                     "cheapest_hour": min(hp, key=lambda h: h["usd_mwh"]), "priciest_hour": max(hp, key=lambda h: h["usd_mwh"]),
                     "optimal": {k: sim[k] for k in ("profit_usd", "revenue_usd", "cost_usd", "degradation_usd", "cycles", "min_soc", "max_soc", "violations")},
                     "naive_clock_rule_profit_usd": sim_base["profit_usd"],
                     "uplift_vs_naive_usd": round(sim["profit_usd"] - sim_base["profit_usd"], 2),
                     "simulated_in": ver["where"], "constraints": cons, "memory_changes_since_last_night": changed}
            text = draft(facts, prompt_version)

            evals = arize.eval_plan(text["message"] + " " + text["explanation"], sim,
                                    {"tonight_usd": sim["profit_usd"], "monthly_usd": facts["uplift_vs_naive_usd"],
                                     "baseline_cost_usd": sim_base["profit_usd"], "optimal_cost_usd": sim["revenue_usd"],
                                     "extra": [sim["cost_usd"], sim["degradation_usd"]]},
                                    prices_usd_mwh=[h["usd_mwh"] for h in hp])
            arize.annotate(root, evals)

            schedule = [{"hour": h["hour"], "ts": h["ts"], "price": h["price"], "charge_mw": h["charge_mw"],
                         "discharge_mw": h["discharge_mw"], "soc_pct": h["soc_pct"]} for h in opt["hours"]]
            plan = {
                "plan_id": plan_id, "date": day.isoformat(), "price_source": source, "prices": hp,
                "schedule": schedule, "baseline_schedule": [{k: h[k] for k in ("hour", "charge_mw", "discharge_mw", "soc_pct")} for h in base["hours"]],
                "profit_usd": sim["profit_usd"], "baseline_profit_usd": sim_base["profit_usd"],
                "uplift_usd": facts["uplift_vs_naive_usd"], "optimal": sim, "baseline": sim_base,
                "buy_windows": buy, "sell_windows": sell, "constraints": cons, "checks": checks,
                "explanation": text["explanation"], "message": text["message"], "voice_text": text["message"],
                "llm": text.get("provider"), "model": text.get("model"), "prompt_version": text.get("prompt_version"),
                "sandbox_id": ver["sandbox_id"], "simulated_in": ver["where"], "evals": evals,
                "voice_url": None, "audio_url": None, "status": "pending_approval", "elapsed_s": round(time.time() - t0, 2),
                # compatibility with the depot-era UI fields
                "savings": {"tonight_usd": sim["profit_usd"], "monthly_usd": facts["uplift_vs_naive_usd"], "pct": None},
            }
            PLANS[plan_id] = plan
            with _tool("remember", {"kind": "plan"}):
                memory.remember("plan", day.isoformat(), {"plan_id": plan_id, "profit_usd": sim["profit_usd"],
                                                          "status": "pending_approval"}, f"dispatcher:{text.get('provider')}")
            if speak and voice.enabled():
                with _tool("request_approval", {"channel": "voice"}):
                    if voice.synthesize(plan_id, text["message"]):
                        plan["voice_url"] = plan["audio_url"] = f"/api/voice/{plan_id}"
            root.set_attribute("output.value", text["message"])
            root.set_attribute("output.mime_type", "text/plain")
            root.set_attribute("grid.profit_usd", sim["profit_usd"])
            root.set_attribute("grid.violations", len(v))
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
        raise KeyError(plan_id)
    status = "approved" if approved else "rejected"
    plan["status"] = status
    with _tracer.start_as_current_span("approval", attributes={"grid.plan_id": plan_id, "grid.approved": approved}):
        try:
            import agentglow

            agentglow.event("plan_" + status, label=plan_id)
        except Exception:  # noqa: BLE001
            pass
        fact = memory.remember("plan", plan["date"], {"plan_id": plan_id, "status": status,
                                                      "profit_usd": plan["profit_usd"]}, f"human:{who}")
    return {"plan_id": plan_id, "status": status, "fact_id": fact["id"], "date": plan["date"]}


def summary(plan: dict) -> dict:
    """Compact plan for voice tools."""
    return {"plan_id": plan["plan_id"], "trading_day": plan["date"], "profit_usd": round(plan["profit_usd"]),
            "naive_profit_usd": round(plan["baseline_profit_usd"]), "buy": plan["buy_windows"], "sell": plan["sell_windows"],
            "cycles": plan["optimal"]["cycles"], "min_soc_pct": plan["optimal"]["min_soc"],
            "violations": len(plan["optimal"]["violations"]), "simulated_in": plan["simulated_in"],
            "sandbox_id": plan["sandbox_id"], "constraints": plan["constraints"], "message": plan["message"],
            "status": plan["status"]}


def latest_plan() -> dict | None:
    return list(PLANS.values())[-1] if PLANS else None


def replan(constraint: str | None = None, d=None, **_ignored) -> dict:
    stored = []
    if constraint:
        c = parse_constraint(constraint)
        if not c:
            return {"error": f"could not parse {constraint!r}; try 'keep 30% in reserve', 'only half a cycle today', "
                             "\"don't sell before 6pm\" or 'reset constraints'"}
        for k, v in c.items():
            stored.append({"key": k, "value": v, "id": add_constraint(k, v, constraint, "voice")["id"]})
    plan = None
    for ev in run_plan(d):
        if ev["kind"] == "plan":
            plan = ev
        elif ev["kind"] == "error":
            return {"error": ev["text"]}
    out = summary(plan) if plan else {"error": "no plan"}
    out["stored_constraints"] = stored
    return out


def backtest_fanout(days: int = 30, on_progress=None, conc: int = 8) -> dict:
    """30-day backtest, one ephemeral Daytona sandbox per day (max `conc` at once). Each sandbox plans on that day's
    day-ahead prices and settles on real-time hourly averages via grid.optimize (fn arb_day). Local fallback per day."""
    days_list = [date.today() - timedelta(days=days - i) for i in range(days)]
    jobs = []
    for d in days_list:
        da, rt = prices.day_ahead(d), prices.real_time(d)
        if len(da) < 23 or len(rt) < 250:
            continue
        jobs.append((f"day {d.isoformat()}", "arb_day", [d.isoformat(), da, rt], {}))
    cons = active_constraints()
    for j in jobs:
        j[3].update(reserve_pct=float(cons["reserve_pct"] or 10), cycles=float(cons["cycles"] if cons["cycles"] is not None else 1.0))
    with _tracer.start_as_current_span("backtest fan-out", attributes={"grid.days": len(jobs)}):
        res = daytona_sim.fan_out(jobs, conc=conc, on_progress=on_progress)
    out, tb, to = [], 0.0, 0.0
    for r in res:
        x = r["result"]
        tb += x["baseline_profit"]
        to += x["optimal_profit"]
        out.append({**x, "sandbox_id": r["sandbox_id"], "where": r["where"],
                    "baseline_usd": x["baseline_profit"], "optimal_usd": x["optimal_profit"], "saved_usd": x["optimal_profit"]})
    n_sb = sum(1 for r in res if r["where"] == "daytona")
    return {"days": out, "totals": {"days": len(out), "baseline_profit": round(tb, 2), "optimal_profit": round(to, 2),
                                    "uplift": round(to - tb, 2), "saved_usd": round(to, 2), "baseline_usd": round(tb, 2),
                                    "sandboxes": n_sb}}


# ------------------------------------------------------------------ Anchor (non-blocking) + deepagents desk adapter
def start_anchor():
    """Kick off the Anchor live-browser price cross-check in a thread; returns get(timeout) -> check row | None."""
    box: dict = {}
    if not anchor.enabled():
        return lambda timeout=0: None
    import threading

    from opentelemetry import context as otel_ctx

    ctx = otel_ctx.get_current()

    def _run():
        tok = otel_ctx.attach(ctx)
        try:
            with _tool("anchor_cross_check", {"source": "NYISO real-time dashboard in Anchor cloud browser"}):
                box["row"] = anchor.cross_check_price()
        finally:
            otel_ctx.detach(tok)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    def get(timeout: float = 8.0):
        t.join(timeout=timeout)
        return box.get("row")

    return get


def _checks(sim: dict, cons: dict, bat: dict, where: str, sandbox_id: str | None) -> list[dict]:
    v = sim.get("violations") or []
    floor = min(max(bat["soc_min_pct"], float(cons.get("reserve_pct") or 0)), bat["start_soc_pct"])
    out = [
        {"name": f"SOC within {floor:.0f}% to {bat['soc_max_pct']:.0f}%", "ok": not any("SOC" in x and "end" not in x for x in v),
         "detail": f"min {sim['min_soc']:.0f}%, max {sim['max_soc']:.0f}%"},
        {"name": f"at most {float(cons.get('cycles') if cons.get('cycles') is not None else 1):g} cycle per day",
         "ok": not any("cycles" in x for x in v), "detail": f"{sim['cycles']:.2f} cycles"},
        {"name": "never charge and discharge in the same hour", "ok": not any("simultaneous" in x for x in v),
         "detail": "0 overlaps" if not any("simultaneous" in x for x in v) else "overlap"},
        {"name": f"end SOC restored to {bat['end_soc_pct']:.0f}%", "ok": not any("end SOC" in x for x in v), "detail": f"end {sim['end_soc']:.1f}%"},
        {"name": "simulated in Daytona" if where == "daytona" else "simulated locally (Daytona off)",
         "ok": not v, "detail": sandbox_id or "local subprocess", "sandbox_id": sandbox_id},
    ]
    if cons.get("no_discharge_before") is not None:
        out.append({"name": f"no selling before {_fmt_h(int(cons['no_discharge_before']))}",
                    "ok": not any("discharge before" in x for x in v), "detail": "operator constraint"})
    return out


def plan_from_desk(result: dict, anchor_get=None, speak: bool = True) -> dict:
    """Turn a grid.desk result into the same plan event shape as run_plan()."""
    opt, sim = result["plan"], result["simulation"]
    day = opt["date"]
    plan_id = opt.get("plan_id") or uuid.uuid4().hex[:10]
    da = prices.day_ahead(date.fromisoformat(day))
    hp = optimize.hourly_prices(da) if da else [{"ts": h["ts"], "hour": h["hour"], "usd_mwh": h["price"]} for h in opt["hours"]]
    base = optimize.arb_baseline(da) if da else None
    sim_base = optimize.arb_simulate(base, None) if base else {"profit_usd": 0.0}
    cons = {**DEFAULT_CONSTRAINTS, **(opt.get("constraints") or {})}
    checks = _checks(sim, cons, opt["battery"], sim.get("where", "local"), sim.get("sandbox_id"))
    checks.append({"name": f"risk officer verdict: {result['verdict']}", "ok": result["verdict"] == "approve",
                   "detail": ((result.get("verdict_detail") or {}).get("reason") or "")[:160]})
    row = anchor_get(8.0) if anchor_get else None
    if row:
        checks.append(row)
    buy, sell = _windows(opt["hours"], "charge_mw"), _windows(opt["hours"], "discharge_mw")
    spoken = (f"Tomorrow I'll buy {' and '.join(buy) or 'nothing'} and sell {' and '.join(sell) or 'nothing'}, "
              f"profit ${sim['profit_usd']:,.0f}. " + ("Approve?" if result["verdict"] == "approve" else "Risk vetoed it; want to change limits?"))
    explanation = result.get("message") or spoken
    uplift = round(sim["profit_usd"] - sim_base["profit_usd"], 2)
    evals = arize.eval_plan(explanation + " " + spoken, sim,
                            {"tonight_usd": sim["profit_usd"], "monthly_usd": uplift, "baseline_cost_usd": sim_base["profit_usd"],
                             "optimal_cost_usd": sim.get("revenue_usd"), "extra": [sim.get("cost_usd"), sim.get("degradation_usd"), opt.get("expected_profit_usd")]},
                            prices_usd_mwh=[h["usd_mwh"] for h in hp])
    arize.annotate(arize.current_span(), evals)
    plan = {
        "plan_id": plan_id, "date": day, "price_source": f"NYISO day-ahead LBMP, zone N.Y.C., {day}", "prices": hp,
        "schedule": [{k: h[k] for k in ("hour", "ts", "price", "charge_mw", "discharge_mw", "soc_pct")} for h in opt["hours"]],
        "baseline_schedule": [{k: h[k] for k in ("hour", "charge_mw", "discharge_mw", "soc_pct")} for h in base["hours"]] if base else [],
        "profit_usd": sim["profit_usd"], "baseline_profit_usd": sim_base["profit_usd"], "uplift_usd": uplift,
        "optimal": sim, "baseline": sim_base, "buy_windows": buy, "sell_windows": sell, "constraints": cons,
        "checks": checks, "explanation": explanation, "message": spoken, "voice_text": spoken,
        "llm": "bedrock (deepagents desk)", "model": None, "prompt_version": "desk", "engine": "desk",
        "verdict": result["verdict"], "steps": result.get("steps"), "operator_note": result.get("operator_note"),
        "sandbox_id": sim.get("sandbox_id"), "simulated_in": sim.get("where"), "evals": evals,
        "voice_url": None, "audio_url": None, "status": "pending_approval" if result["verdict"] == "approve" else "vetoed",
        "elapsed_s": result.get("elapsed_s"),
        "savings": {"tonight_usd": sim["profit_usd"], "monthly_usd": uplift, "pct": None},
    }
    PLANS[plan_id] = plan
    memory.remember("plan", day, {"plan_id": plan_id, "profit_usd": sim["profit_usd"], "status": plan["status"]}, "desk")
    if speak and voice.enabled() and voice.synthesize(plan_id, spoken):
        plan["voice_url"] = plan["audio_url"] = f"/api/voice/{plan_id}"
    return plan


async def desk_or_fast(d=None, note: str | None = None, fast: bool = False, speak: bool = True, budget_s: float = 45.0):
    """Async generator of SSE events: the deepagents desk (status lines as-is, then plan), falling back to the fast
    direct optimizer path if the desk errors, returns no plan, or exceeds budget_s."""
    import asyncio

    anchor_get = start_anchor()
    if not fast:
        try:
            from grid.desk import stream_desk

            t0 = time.time()
            result = None
            agen = stream_desk(d, note).__aiter__()
            while True:
                left = budget_s - (time.time() - t0)
                if left <= 0:
                    raise TimeoutError(f"desk exceeded {budget_s:.0f}s")
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=left)
                except StopAsyncIteration:
                    break
                if ev["kind"] == "result":
                    result = ev
                elif ev["kind"] == "status":
                    yield ev
                elif ev["kind"] == "error":
                    yield {"kind": "status", "agent": "desk-lead", "text": ev["text"]}
            if result and result.get("plan") and result.get("simulation"):
                plan = await asyncio.to_thread(plan_from_desk, result, anchor_get, speak)
                yield {"kind": "plan", **plan}
                yield {"kind": "done", "plan_id": plan["plan_id"]}
                return
            yield {"kind": "status", "text": "desk returned no plan; switching to the fast optimizer"}
        except Exception as e:  # noqa: BLE001
            log.warning("desk fallback: %s", e)
            yield {"kind": "status", "text": f"desk unavailable ({type(e).__name__}); switching to the fast optimizer"}
            try:
                await agen.aclose()
            except Exception:  # noqa: BLE001
                pass
    if note:
        c = parse_constraint(note)
        for k, v in (c or {}).items():
            add_constraint(k, v, note, "operator")
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def work():
        try:
            for ev in run_plan(d, speak=speak, anchor_get=anchor_get):
                loop.call_soon_threadsafe(q.put_nowait, ev)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    loop.run_in_executor(None, work)
    while True:
        ev = await q.get()
        if ev is None:
            break
        yield ev
