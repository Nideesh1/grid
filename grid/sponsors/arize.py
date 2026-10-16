"""Arize AX: OpenInference tracing of the dispatcher + per-plan evals.

Enabled when ARIZE_SPACE_ID and ARIZE_API_KEY are in .env. register() installs the global OTel provider, so
AgentGlow (agentglow.watch) adds its processor to the same provider: one trace, two viewers.
Evals are attached to the top-level "dispatcher" span as OpenInference-style eval attributes
(eval.<name>.label / score / explanation) so they show up on the trace in Arize.
"""
from __future__ import annotations

import json
import logging
import re

from opentelemetry import trace

from grid.config import env, has

log = logging.getLogger("grid.arize")
PROJECT = "grid-dispatcher"
_state = {"enabled": False, "instrumented": False, "error": None}


def enabled() -> bool:
    return _state["enabled"]


def status() -> dict:
    return {"enabled": _state["enabled"], "project": PROJECT if _state["enabled"] else None,
            "mistral_instrumented": _state["instrumented"], "error": _state["error"],
            "keys_present": has("ARIZE_SPACE_ID", "ARIZE_API_KEY")}


def setup(project: str = PROJECT) -> bool:
    if not has("ARIZE_SPACE_ID", "ARIZE_API_KEY"):
        log.info("arize disabled: ARIZE_SPACE_ID / ARIZE_API_KEY not set")
        return False
    try:
        from arize.otel import register

        tp = register(space_id=env("ARIZE_SPACE_ID"), api_key=env("ARIZE_API_KEY"), project_name=project, verbose=False)
        _state["enabled"] = True
        try:
            from openinference.instrumentation.mistralai import MistralAIInstrumentor

            MistralAIInstrumentor().instrument(tracer_provider=tp)
            _state["instrumented"] = True
        except Exception as e:  # noqa: BLE001  (SDK v3 layout may not be supported; manual LLM spans cover it)
            log.info("mistral auto-instrumentation unavailable (%s); using manual OpenInference spans", type(e).__name__)
        try:
            from openinference.instrumentation.bedrock import BedrockInstrumentor

            BedrockInstrumentor().instrument(tracer_provider=tp)
        except Exception as e:  # noqa: BLE001
            log.info("bedrock auto-instrumentation unavailable (%s)", type(e).__name__)
        log.info("arize tracing enabled, project=%s", project)
        return True
    except Exception as e:  # noqa: BLE001
        _state["error"] = type(e).__name__
        log.warning("arize disabled: %s", type(e).__name__)
        return False


DOLLARS = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|K|thousand)?(\s*(?:/|per)\s*MWh)?", re.I)


def dollar_figures(text: str, per_mwh: bool = False) -> list[float]:
    out = []
    for m in DOLLARS.finditer(text or ""):
        if bool(m.group(3)) != per_mwh:
            continue
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        out.append(v)
    return out


def eval_plan(message: str, sim: dict, savings: dict, tol: float = 1.0, prices_usd_mwh: list[float] | None = None) -> dict:
    """Code evals. constraint_check: simulate violations == 0. grounded_savings: every $ figure in the message
    matches a number the tools produced (within tol, or rounding to the nearest $10/$100 for spoken figures)."""
    viol = sim.get("violations") or []
    allowed = [float(x) for x in (savings.get("tonight_usd"), savings.get("monthly_usd"), sim.get("cost_usd"),
                                  savings.get("baseline_cost_usd"), savings.get("optimal_cost_usd")) if isinstance(x, (int, float))]
    figs = dollar_figures(message)

    def ok(f: float) -> bool:
        return any(abs(f - a) <= tol or (f >= 100 and abs(f - a) <= max(tol, 0.05 * abs(a)) and f % 10 == 0) for a in allowed)

    bad = [f for f in figs if not ok(f)]
    # $/MWh figures must be prices the tools fetched (within $0.01, or rounded to the dollar)
    for f in dollar_figures(message, per_mwh=True):
        if not any(abs(f - p) <= 0.51 for p in (prices_usd_mwh or []) + [sim.get("avg_usd_mwh") or -1e9]):
            bad.append(f)
    claims_safe = bool(re.search(r"\b(no|zero|0)\s+(safety\s+)?violations?\b|all (buses|40 buses)", message or "", re.I))
    return {
        "constraint_check": {"label": "pass" if not viol else "fail", "score": 1.0 if not viol else 0.0,
                             "explanation": f"{len(viol)} violations in simulation"},
        "grounded_savings": {"label": "pass" if figs and not bad else "fail", "score": 1.0 if figs and not bad else 0.0,
                             "explanation": (f"ungrounded $ figures {bad}" if bad else
                                             "no $ figure in message" if not figs else f"{len(figs)} $ figures match tools")
                             + ("" if not viol or not claims_safe else "; claims safe but simulation has violations")},
    }


def annotate(span, evals: dict) -> None:
    """Attach evals to a span (works with or without Arize; AgentGlow sees them too)."""
    if span is None:
        return
    try:
        for name, e in evals.items():
            span.set_attribute(f"eval.{name}.label", str(e.get("label")))
            span.set_attribute(f"eval.{name}.score", float(e.get("score", 0)))
            span.set_attribute(f"eval.{name}.explanation", str(e.get("explanation", ""))[:300])
    except Exception:  # noqa: BLE001
        pass


def current_span():
    s = trace.get_current_span()
    return s if s and s.get_span_context().is_valid else None


def oi_chain_attrs(input_obj, output_obj=None) -> dict:
    a = {"openinference.span.kind": "CHAIN", "input.value": json.dumps(input_obj, default=str)[:4000],
         "input.mime_type": "application/json"}
    if output_obj is not None:
        a["output.value"] = output_obj if isinstance(output_obj, str) else json.dumps(output_obj, default=str)[:4000]
    return a
