"""Arize loop: trace -> eval -> investigate -> fix -> experiment.

Runs the dispatcher over N backtest nights (a fixed dataset of real NYISO nights) with a baseline prompt (v1) and a
candidate prompt (v2), scores each run with
  (a) code eval  grounded_savings: every $ figure in the message/explanation matches simulate() within $1, and the
      violations claim agrees with the simulation (grid/sponsors/arize.py eval_plan)
  (b) LLM judge  clear_and_complete: Mistral (else Anthropic, else a rule-based judge) labels the dispatcher text
and attaches label/score/explanation as eval.* attributes on each run's top-level "dispatcher" span (Arize project
grid-dispatcher, prompt version in llm.prompt_template.version). Results: data/arize_loop/<version>.json.

uv run python scripts/arize_loop.py --nights 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opentelemetry import trace  # noqa: E402

from grid.config import DATA, setup_tracing  # noqa: E402

OUT = DATA / "arize_loop"
JUDGE = ("You grade an AI battery trader's explanation + spoken message to a grid dispatcher. It is CLEAR_AND_COMPLETE "
         "if it states (1) when it buys, (2) when it sells, (3) the dollar profit, (4) the safety result, and (5) asks "
         "for approval, in plain language a dispatcher can act on. Return JSON "
         "{\"label\": \"pass\"|\"fail\", \"score\": 0..1, \"explanation\": one sentence}.")


def rule_judge(text: str) -> dict:
    parts = {"buy": bool(re.search(r"\b(buy|charg)", text, re.I)),
             "sell": bool(re.search(r"\b(sell|discharg)", text, re.I)),
             "profit $": bool(re.search(r"profit.{0,40}\$[\d,]+|\$[\d,]+.{0,40}profit", text, re.I)),
             "safety": bool(re.search(r"violation|safety|safe", text, re.I)),
             "approval ask": bool(re.search(r"approve|yes or no|\?", text, re.I))}
    score = sum(parts.values()) / len(parts)
    missing = [k for k, v in parts.items() if not v]
    return {"label": "pass" if score == 1 else "fail", "score": score,
            "explanation": "rule judge: " + ("all five elements present" if not missing else "missing " + ", ".join(missing))}


def llm_judge(text: str) -> dict:
    from grid import agent

    def bedrock_judge(sys_, u):
        try:
            return agent._bedrock_call(sys_, u, agent.BEDROCK_JUDGE)
        except Exception:  # noqa: BLE001  (Haiku may not be enabled in the account: use the dispatcher model)
            return agent._bedrock_call(sys_, u)

    fns = {"bedrock": bedrock_judge, "mistral": agent._mistral, "anthropic": agent._anthropic}
    for name in agent._chain():
        fn = fns.get(name)
        if fn is None:
            continue
        try:
            raw, _ = fn(JUDGE, text)
            j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            return {"label": str(j["label"]).lower(), "score": float(j["score"]), "explanation": f"{name} judge: {j['explanation']}"}
        except Exception:  # noqa: BLE001
            continue
    return rule_judge(text)


def nights(n: int) -> list[str]:
    from grid.prices import day_ahead

    out, d = [], date.today() - timedelta(days=1)
    while len(out) < n:
        if len(day_ahead(d)) >= 23:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def run(version: str, dataset: list[str]) -> dict:
    from grid import agent
    from grid.sponsors import arize

    tracer = trace.get_tracer("grid.arize_loop")
    rows = []
    for d in dataset:
        with tracer.start_as_current_span(f"experiment {version} {d}", attributes={
                "openinference.span.kind": "CHAIN", "grid.experiment": "dispatcher-prompt", "grid.prompt_version": version,
                "input.value": json.dumps({"night": d, "prompt_version": version})}) as exp:
            plan = None
            for ev in agent.run_plan(d, prompt_version=version, speak=False):
                if ev["kind"] == "plan":
                    plan = ev
            if not plan:
                rows.append({"night": d, "error": True})
                continue
            text = plan["explanation"] + " " + plan["message"]
            judged = llm_judge(text)
            words = len(plan["message"].split())
            spoken_ok = words <= 30 and "$" in plan["message"] and re.search(r"approve", plan["message"], re.I) is not None
            concise = {"label": "pass" if spoken_ok else "fail", "score": 1.0 if spoken_ok else 0.0,
                       "explanation": f"{words} words; needs <=30 words, a $ profit and an approval ask (it is read aloud)"}
            evals = {**plan["evals"], "clear_and_complete": judged, "voice_ready": concise}
            arize.annotate(exp, evals)
            exp.set_attribute("output.value", plan["message"])
            rows.append({"night": d, "plan_id": plan["plan_id"], "llm": plan["llm"], "profit_usd": plan["profit_usd"],
                         "message": plan["message"], "explanation": plan["explanation"],
                         **{k: v["label"] for k, v in evals.items()}, "judge_note": judged["explanation"]})
    ok = [r for r in rows if not r.get("error")]

    def rate(k):
        return round(sum(r[k] == "pass" for r in ok) / max(len(ok), 1), 2)

    res = {"version": version, "n": len(ok), "grounded_savings": rate("grounded_savings"),
           "constraint_check": rate("constraint_check"), "clear_and_complete": rate("clear_and_complete"),
           "voice_ready": rate("voice_ready"), "rows": rows}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{version}.json").write_text(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--nights", type=int, default=5)
    ap.add_argument("--baseline", default="v1")
    ap.add_argument("--candidate", default="v2")
    a = ap.parse_args()
    tr = setup_tracing()
    print(f"tracing: arize={'on' if tr['arize'] else 'off (no keys)'} agentglow={'on' if tr['agentglow'] else 'off'}")
    from grid import agent
    from grid.sponsors import daytona_sim

    try:
        ds = nights(a.nights)
        print(f"dataset: {len(ds)} real NYISO nights {ds[0]} .. {ds[-1]}")
        results = [run(v, ds) for v in (a.baseline, a.candidate)]
        print(f"llm used: {agent.llm_status()}")
        print(f"{'version':<10}{'n':>3}{'grounded':>10}{'safety':>8}{'clear':>8}{'voice_ready':>13}")
        for r in results:
            print(f"{r['version']:<10}{r['n']:>3}{r['grounded_savings']:>10}{r['constraint_check']:>8}{r['clear_and_complete']:>8}{r['voice_ready']:>13}")
        b, c = results
        better = (c["grounded_savings"] + c["clear_and_complete"] + c["voice_ready"]) >= (b["grounded_savings"] + b["clear_and_complete"] + b["voice_ready"])
        print(f"winner: {c['version'] if better else b['version']}  (results in {OUT})")
    finally:
        daytona_sim.shutdown()
        try:
            trace.get_tracer_provider().force_flush(10000)
        except Exception:  # noqa: BLE001
            pass
