"""End-to-end smoke test of the agent track. uv run python scripts/smoke_agent.py [--api http://localhost:8520]

Without --api: runs the agent in-process (Daytona sandbox, memory, LLM/template, evals, voice if keyed).
With --api: hits the running FastAPI (health, prices, plan SSE, approve, voice, backtest).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def in_process(date: str | None) -> int:
    from grid.config import setup_tracing

    print("tracing:", setup_tracing())
    from grid import agent
    from grid.sponsors import daytona_sim, memory

    plan = None
    t0 = time.time()
    try:
        for ev in agent.run_plan(date):
            if ev["kind"] == "status":
                print(f"  [{time.time() - t0:5.1f}s] {ev['text']}")
            elif ev["kind"] == "plan":
                plan = ev
            elif ev["kind"] == "error":
                print("ERROR", ev["text"])
        if not plan:
            return 1
        print(f"plan {plan['plan_id']} day {plan['date']} buy {plan['buy_windows']} sell {plan['sell_windows']} via "
              f"{plan['simulated_in']} {plan['sandbox_id']}")
        print(f"  profit ${plan['profit_usd']:,.2f} vs naive ${plan['baseline_profit_usd']:,.2f}, "
              f"violations={len(plan['optimal']['violations'])}, cycles {plan['optimal']['cycles']}, constraints {plan['constraints']}")
        print(f"  llm={plan['llm']} explanation: {plan['explanation']}")
        print(f"  message: {plan['message']}")
        print(f"  evals: {json.dumps({k: v['label'] for k, v in plan['evals'].items()})}  voice: {plan['voice_url']}")
        print("  approve:", agent.approve(plan["plan_id"], True, "smoke"))
        hist = memory.recall("plan", plan["date"], history=True)
        print(f"  memory[{memory.backend()}] plan facts for night: {len(hist)} "
              f"(active={sum(1 for f in hist if not f['valid_until'])}, superseded={sum(1 for f in hist if f['superseded_by'])})")
        ok = not plan["optimal"]["violations"] and plan["profit_usd"] > 0
        print("SMOKE", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        daytona_sim.shutdown()
        try:
            from opentelemetry import trace

            trace.get_tracer_provider().force_flush(5000)
        except Exception:  # noqa: BLE001
            pass


def via_api(api: str, date: str | None) -> int:
    import httpx

    c = httpx.Client(base_url=api, timeout=180)
    h = c.get("/api/health").json()
    print("health:", json.dumps({k: v.get("enabled") for k, v in h["sponsors"].items()}), "llm:", h["llm"]["configured"])
    p = c.get("/api/prices").json()
    print(f"prices: {len(p['day_ahead'])} day-ahead rows for {p['date']}")
    plan = None
    with c.stream("POST", "/api/plan", json={"date": date} if date else {}) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev["kind"] == "status":
                    print("  status:", ev["text"])
                elif ev["kind"] == "plan":
                    plan = ev
                elif ev["kind"] == "error":
                    print("  ERROR", ev["text"])
    if not plan:
        return 1
    print(f"plan {plan['plan_id']}: profit ${plan['profit_usd']:,.2f} (naive ${plan['baseline_profit_usd']:,.2f}), sandbox {plan['sandbox_id']}, llm {plan['llm']}")
    print("  message:", plan["message"])
    print("approve:", c.post("/api/approve", json={"plan_id": plan["plan_id"], "approved": True}).json())
    v = c.get(f"/api/voice/{plan['plan_id']}")
    print("voice:", v.status_code, v.headers.get("content-type"), len(v.content), "bytes")
    t = time.time()
    b = c.get("/api/backtest", params={"days": 30, "sandbox": 1}).json()
    print(f"backtest: {b['totals']} ({time.time() - t:.1f}s)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=None)
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    sys.exit(via_api(a.api, a.date) if a.api else in_process(a.date))
