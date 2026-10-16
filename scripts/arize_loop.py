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

from grid.config import DATA, has, setup_tracing  # noqa: E402

OUT = DATA / "arize_loop"
JUDGE = ("You grade a night-shift energy dispatcher message for an electric bus depot. It is CLEAR_AND_COMPLETE if it "
         "states (1) when buses charge, (2) tonight's dollar savings, (3) the monthly savings, (4) the safety result, "
         "and (5) asks for a yes/no approval, in plain language a dispatcher can act on. Return JSON "
         "{\"label\": \"pass\"|\"fail\", \"score\": 0..1, \"explanation\": one sentence}.")


def rule_judge(text: str) -> dict:
    parts = {"window": bool(re.search(r"\d{1,2}:\d{2}|\b\d{1,2}\s?(am|pm)\b|midnight|overnight", text, re.I)),
             "tonight $": bool(re.search(r"\$[\d,]+.{0,40}tonight|tonight.{0,40}\$[\d,]+", text, re.I)),
             "monthly $": bool(re.search(r"month", text, re.I)),
             "safety": bool(re.search(r"violation|safety|safe", text, re.I)),
             "approval ask": bool(re.search(r"approve|yes or no|\?", text, re.I))}
    score = sum(parts.values()) / len(parts)
    missing = [k for k, v in parts.items() if not v]
    return {"label": "pass" if score == 1 else "fail", "score": score,
            "explanation": "rule judge: " + ("all five elements present" if not missing else "missing " + ", ".join(missing))}


def llm_judge(text: str) -> dict:
    from grid import agent

    for name, fn, key in (("mistral", agent._mistral, "MISTRAL_API_KEY"), ("anthropic", agent._anthropic, "ANTHROPIC_API_KEY")):
        if not has(key):
            continue
        try:
            raw, _ = fn(JUDGE, text)
            j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            return {"label": str(j["label"]).lower(), "score": float(j["score"]), "explanation": f"{name} judge: {j['explanation']}"}
        except Exception:  # noqa: BLE001
            continue
    return rule_judge(text)


def nights(n: int) -> list[str]:
    from grid.prices import history

    return list(history(n + 2, "rt").keys())[-n:]


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
            evals = {**plan["evals"], "clear_and_complete": judged}
            arize.annotate(exp, evals)
            exp.set_attribute("output.value", plan["message"])
            rows.append({"night": d, "plan_id": plan["plan_id"], "llm": plan["llm"], "tonight_usd": plan["savings"]["tonight_usd"],
                         "message": plan["message"], "explanation": plan["explanation"],
                         **{k: v["label"] for k, v in evals.items()}, "judge_note": judged["explanation"]})
    ok = [r for r in rows if not r.get("error")]

    def rate(k):
        return round(sum(r[k] == "pass" for r in ok) / max(len(ok), 1), 2)

    res = {"version": version, "n": len(ok), "grounded_savings": rate("grounded_savings"),
           "constraint_check": rate("constraint_check"), "clear_and_complete": rate("clear_and_complete"), "rows": rows}
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
        print(f"{'version':<10}{'n':>3}{'grounded_savings':>18}{'constraint_check':>18}{'clear_and_complete':>20}")
        for r in results:
            print(f"{r['version']:<10}{r['n']:>3}{r['grounded_savings']:>18}{r['constraint_check']:>18}{r['clear_and_complete']:>20}")
        b, c = results
        better = (c["grounded_savings"] + c["clear_and_complete"]) >= (b["grounded_savings"] + b["clear_and_complete"])
        print(f"winner: {c['version'] if better else b['version']}  (results in {OUT})")
    finally:
        daytona_sim.shutdown()
        try:
            trace.get_tracer_provider().force_flush(10000)
        except Exception:  # noqa: BLE001
            pass
