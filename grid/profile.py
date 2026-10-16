"""User battery profile ("bring your own battery"). One active profile, default = 10 MW / 40 MWh in N.Y.C.

Saved to data/battery_profile.json (current + history) and, field by field, to agent memory (SQLite ledger with
supersession + SurrealDB Agent Memory triples on entity battery/<name>, so each change supersedes the old value).

    from grid.profile import get_profile, battery_arg, rules
    plan = arb_optimal(day_ahead(d, p["zone"]), battery=battery_arg(), **rules())
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

from grid.config import DATA

PATH = DATA / "battery_profile.json"
DEFAULT = {"name": "nyc-battery-1", "power_mw": 10.0, "energy_mwh": 40.0, "rte": 0.85, "start_soc_pct": 50.0,
           "end_soc_pct": 50.0, "min_soc_pct": 10.0, "max_soc_pct": 90.0, "cycles_per_day": 1.0,
           "degradation_usd_mwh": 8.0, "zone": "N.Y.C.", "sell_above": None, "buy_below": None}
NUMERIC = {"power_mw", "energy_mwh", "rte", "start_soc_pct", "end_soc_pct", "min_soc_pct", "max_soc_pct",
           "cycles_per_day", "degradation_usd_mwh", "sell_above", "buy_below"}
ZONES = ["WEST", "GENESE", "CENTRL", "NORTH", "MHK VL", "CAPITL", "HUD VL", "MILLWD", "DUNWOD", "N.Y.C.", "LONGIL"]
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {"current": dict(DEFAULT), "history": []}


def get_profile() -> dict:
    return {**DEFAULT, **_load()["current"]}


def history(limit: int = 20) -> list[dict]:
    return _load()["history"][-limit:][::-1]


def battery_arg() -> dict:
    """Battery dict for grid.optimize arb_* (user-facing keys; price rules excluded)."""
    p = get_profile()
    return {k: p[k] for k in ("power_mw", "energy_mwh", "rte", "start_soc_pct", "end_soc_pct", "min_soc_pct",
                              "max_soc_pct", "cycles_per_day", "degradation_usd_mwh", "zone")}


def rules() -> dict:
    p = get_profile()
    return {"sell_above": p.get("sell_above"), "buy_below": p.get("buy_below")}


def _clean(upd: dict) -> dict:
    out = {}
    for k, v in upd.items():
        if k not in DEFAULT or k == "name" and not v or (v is None and k not in NUMERIC):
            continue
        if k in NUMERIC:
            out[k] = None if v in (None, "", "none") else float(v)
        elif k == "zone":
            z = str(v).strip().upper()
            z = {"NYC": "N.Y.C.", "NEW YORK CITY": "N.Y.C.", "LONG ISLAND": "LONGIL", "HUDSON VALLEY": "HUD VL"}.get(z, z)
            if z not in ZONES:
                raise ValueError(f"unknown NYISO zone {v!r}; one of {ZONES}")
            out[k] = z
        else:
            out[k] = str(v)
    if out.get("rte") is not None and out["rte"] > 1:
        out["rte"] = out["rte"] / 100
    return out


def save_profile(upd: dict, source: str = "user", replace: bool = False) -> dict:
    """Merge (or replace) the profile; record history + one memory fact per changed field (supersedes)."""
    from grid.sponsors import memory

    upd = _clean(upd)
    with _lock:
        st = _load()
        old = {**DEFAULT, **st["current"]}
        cur = dict(DEFAULT) if replace else old
        if "start_soc_pct" in upd and "end_soc_pct" not in upd and not replace:
            upd.setdefault("end_soc_pct", cur.get("end_soc_pct", 50.0))
        new = {**cur, **upd}
        changed = {k: new[k] for k in new if old.get(k) != new[k]}
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        st["current"] = new
        st["history"].append({"at": ts, "source": source, "changed": changed})
        PATH.write_text(json.dumps(st, indent=1))
    for k, v in changed.items():
        if k != "name":
            memory.remember("battery", f"{new['name']}:{k}", {"value": v, "text": f"{new['name']} {k} = {v}"}, source)
    return {"profile": new, "changed": changed}


def parse_note(text: str) -> dict:
    """Profile updates from plain words: 'only sell above $60', 'buy below $25', 'we have 22 MWh stored right now',
    'battery is 20 MW / 80 MWh', 'we're in Long Island', 'round trip 88%'."""
    t = (text or "").lower()
    p = get_profile()
    out: dict = {}
    m = re.search(r"(?:only\s+)?sell\w*\s+(?:only\s+)?(?:above|over|at least|>)\s*\$?\s*(\d+(?:\.\d+)?)", t)
    if m:
        out["sell_above"] = float(m.group(1))
    m = re.search(r"(?:only\s+)?buy\w*\s+(?:only\s+)?(?:below|under|at most|<)\s*\$?\s*(\d+(?:\.\d+)?)", t)
    if m:
        out["buy_below"] = float(m.group(1))
    if re.search(r"(no|remove|clear|drop)\s+(the\s+)?(price\s+)?(rule|floor|limit)s?", t):
        out.update(sell_above=None, buy_below=None)
    m = re.search(r"(\d+(?:\.\d+)?)\s*mwh\s*(?:stored|in (?:the )?battery|of charge|left|right now|now)", t) or \
        re.search(r"(?:have|holding|stored)\s*(\d+(?:\.\d+)?)\s*mwh", t)
    if m:
        out["start_soc_pct"] = round(100 * float(m.group(1)) / float(p["energy_mwh"]), 2)
    m = re.search(r"(?:at|currently|now)\s*(\d+(?:\.\d+)?)\s*%\s*(?:charged|soc|state of charge|full)", t)
    if m:
        out["start_soc_pct"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*mw\b(?!h)", t)
    if m and re.search(r"battery|unit|system|is|we have", t):
        out["power_mw"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*mwh\b", t)
    if m and "start_soc_pct" not in out and re.search(r"battery|unit|system|capacity|is", t):
        out["energy_mwh"] = float(m.group(1))
    m = re.search(r"round[- ]?trip\s*(?:efficiency\s*)?(?:of\s*)?(\d+(?:\.\d+)?)\s*%", t)
    if m:
        out["rte"] = float(m.group(1)) / 100
    for z, names in {"LONGIL": ["long island"], "N.Y.C.": ["new york city", "nyc", "n.y.c"], "HUD VL": ["hudson valley"],
                     "WEST": ["western ny", "buffalo"], "CAPITL": ["capital", "albany"], "CENTRL": ["central ny", "syracuse"],
                     "NORTH": ["north country"], "GENESE": ["genesee", "rochester"], "MHK VL": ["mohawk"],
                     "MILLWD": ["millwood"], "DUNWOD": ["dunwoodie", "westchester"]}.items():
        if any(re.search(rf"\b(in|zone|at|to)\s+{re.escape(n)}\b", t) for n in names):
            out["zone"] = z
    return out
