"""NYISO grid alerts read from the human-facing System Conditions page in an Anchor cloud browser.

The page https://www.nyiso.com/system-conditions is JS-rendered: the operating state (Normal / Alert / Major
Emergency), the Thunderstorm Alert and Reserve Pick-up notices are filled in by page script. Anchor renders it the way
an operator sees it and we parse the rendered text. One short-lived session at a time, always deleted, cached 5 min.
If Anchor fails we return via="unavailable" and no alerts (never fake ones).

Demo override (stage use only, always labeled): env GRID_ALERT_DEMO=thunderstorm|emergency, or set_demo()/clear_demo()
(POST/DELETE /api/alerts/demo). Simulated alerts carry simulated=True and the label "simulated for demo".
"""
from __future__ import annotations

import html as _html
import logging
import re
import threading
import time
from datetime import datetime, timezone

from grid.config import env
from grid.sponsors import anchor

log = logging.getLogger("grid.alerts")
SOURCE_URL = "https://www.nyiso.com/system-conditions"
CACHE_S = 300
ALERT_RESERVE_PCT = 30.0
DEMO_LABEL = "simulated for demo"
_lock = threading.Lock()
_cache: dict = {"t": 0.0, "res": None}
_demo: dict = {"type": (env("GRID_ALERT_DEMO", "") or "").strip().lower() or None}

DEMO_TYPES = {
    "thunderstorm": {"type": "Thunderstorm Alert", "zone": "N.Y.C.", "status": "alert",
                     "text": "Thunderstorm Alert in effect for N.Y.C. (Zone J): NYISO secures the city against loss of "
                             "lines from storms. SIMULATED FOR DEMO, not a real NYISO posting."},
    "emergency": {"type": "Major Emergency", "zone": "NYCA", "status": "emergency",
                  "text": "Major Emergency declared for the New York Control Area. SIMULATED FOR DEMO, not a real NYISO posting."},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(fragment: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _notice(page: str, key: str) -> tuple[bool | None, str | None]:
    """('ta' etc.) -> (in_effect, since). None if the notice was not rendered."""
    m = re.search(rf'ajax-value-{key}\b[^>]*>(.*?)</div>\s*(?:<div class="ts-{key}">([^<]*)</div>)?', page, re.S)
    if not m:
        return None, None
    t = _text(m.group(1)).lower()
    if "not in effect" in t:
        return False, None
    if "in effect" in t:
        ts = m.group(2)
        if not ts:
            ts_m = re.search(rf'class="ts-{key}">([^<]*)<', page)
            ts = ts_m.group(1) if ts_m else None
        return True, (ts or "").strip() or None
    return None, None


def parse(page: str) -> dict:
    """Rendered System Conditions HTML -> {status, state_text, alerts, raw_excerpt}. Raises ValueError if not rendered."""
    m = re.search(r'ajax-system-conditions-conditions-text[^>]*>\s*([^<]+?)\s*<', page)
    state = (m.group(1).strip() if m else "")
    if not state:
        raise ValueError("system condition not rendered")
    alerts: list[dict] = []
    status = {"normal": "normal", "alert": "alert", "major emergency": "emergency"}.get(state.lower(), "alert")
    if status != "normal":
        alerts.append({"type": f"NYISO system state: {state}", "zone": "NYCA", "text": f"NYISO reports the system is in {state} state.",
                       "since": None, "simulated": False})
    notices = {}
    for key, label, zone in (("ta", "Thunderstorm Alert", "N.Y.C."), ("rpu", "Reserve Pick-up", "NYCA")):
        on, since = _notice(page, key)
        notices[label] = "in effect" if on else ("not in effect" if on is False else "not shown")
        if on:
            alerts.append({"type": label, "zone": zone, "text": f"NYISO {label} in effect" + (f" since {since}" if since else ""),
                           "since": since, "simulated": False})
            if status == "normal":
                status = "alert"
    banner = re.search(r"(Statewide energy supply conditions[^<]{0,160}?\.)", page)
    excerpt = (f"System condition: {state}. " + " ".join(f"{k}: {v}." for k, v in notices.items())
               + (f" Banner: {_text(banner.group(1))}" if banner else ""))
    return {"status": status, "state_text": state, "notices": notices, "alerts": alerts, "raw_excerpt": excerpt[:600]}


def _fetch() -> dict:
    page, err = anchor.render_page(SOURCE_URL, "html", timeout=25)
    if page is None:
        return {"status": "normal", "alerts": [], "raw_excerpt": None, "via": "unavailable", "error": err}
    try:
        return {**parse(page), "via": "anchor"}
    except ValueError as e:
        return {"status": "normal", "alerts": [], "raw_excerpt": None, "via": "unavailable", "error": str(e)}


def _real(wait_s: float = 30.0) -> dict:
    if _cache["res"] and time.time() - _cache["t"] < CACHE_S:
        return {**_cache["res"], "via": "cache"}
    if not _lock.acquire(timeout=wait_s):  # another fetch is running: never open a second browser
        return ({**_cache["res"], "via": "cache"} if _cache["res"] else
                {"status": "normal", "alerts": [], "raw_excerpt": None, "via": "unavailable", "error": "fetch in progress"})
    try:
        if _cache["res"] and time.time() - _cache["t"] < CACHE_S:
            return {**_cache["res"], "via": "cache"}
        res = {**_fetch(), "source_url": SOURCE_URL, "fetched_at": _now()}
        if res["via"] == "anchor":
            _cache.update(t=time.time(), res=res)
        else:
            log.warning("grid alerts unavailable: %s", res.get("error"))
        return res
    finally:
        _lock.release()


def get_grid_alerts(wait_s: float = 30.0) -> dict:
    """{source_url, fetched_at, status: normal|alert|emergency, alerts:[{type, zone, text, since, simulated}],
    raw_excerpt, via: anchor|cache|unavailable, simulated, demo_label}."""
    res = dict(_real(wait_s))
    res["alerts"] = list(res.get("alerts") or [])
    res["simulated"] = False
    res["demo_label"] = None
    d = DEMO_TYPES.get(_demo["type"] or "")
    if d:
        res["alerts"].append({"type": d["type"], "zone": d["zone"], "text": d["text"], "since": _now(), "simulated": True,
                              "label": DEMO_LABEL})
        rank = {"normal": 0, "alert": 1, "emergency": 2}
        if rank[d["status"]] > rank.get(res.get("status"), 0):
            res["status"] = d["status"]
        res["simulated"] = True
        res["demo_label"] = DEMO_LABEL
    return res


def set_demo(kind: str = "thunderstorm") -> dict:
    kind = (kind or "thunderstorm").strip().lower()
    if kind not in DEMO_TYPES:
        raise ValueError(f"unknown demo type {kind!r}; use one of {sorted(DEMO_TYPES)}")
    _demo["type"] = kind
    return {"demo": kind, "label": DEMO_LABEL}


def clear_demo() -> dict:
    _demo["type"] = None
    return {"demo": None}


def affecting(res: dict, zone: str | None) -> list[dict]:
    """Alerts that touch N.Y.C., the battery's zone, or the whole control area (NYCA)."""
    if res.get("status") not in ("alert", "emergency"):
        return []
    zones = {"N.Y.C.", "NYCA", zone or "N.Y.C."}
    return [a for a in res.get("alerts") or [] if a.get("zone") in zones]


def describe(a: dict) -> str:
    return f"NYISO {a['type']} for {a['zone']} (via Anchor" + (f", {DEMO_LABEL})" if a.get("simulated") else ")")


def check_row() -> dict | None:
    """Safety-check row for the plan: 'Grid alerts checked in live browser (Anchor)'. None if Anchor was unavailable."""
    res = get_grid_alerts(wait_s=20)
    if res["via"] == "unavailable" and not res["simulated"]:
        return None
    hits = res.get("alerts") or []
    detail = ("normal: " + (res.get("raw_excerpt") or "")[:140]) if not hits else "; ".join(
        f"{a['type']} {a['zone']}" + (f" ({DEMO_LABEL})" if a.get("simulated") else "") for a in hits)
    if hits and res["status"] != "normal":
        detail += f"; risk officer requires {ALERT_RESERVE_PCT:g}% reserve"
    return {"name": "Grid alerts checked in live browser (Anchor)", "ok": res["status"] == "normal",
            "detail": detail[:220], "via": res["via"], "simulated": res["simulated"], "source_url": SOURCE_URL}


def required_reserve(zone: str | None, wait_s: float = 10.0) -> tuple[float | None, str | None]:
    """(30.0, 'NYISO Thunderstorm Alert for N.Y.C. (via Anchor...)') if an alert touches N.Y.C./zone, else (None, None)."""
    hits = affecting(get_grid_alerts(wait_s=wait_s), zone)
    return (ALERT_RESERVE_PCT, describe(hits[0])) if hits else (None, None)


def grade_row(row: dict | None, sim: dict | None) -> dict | None:
    """PASS on an alert when the plan's simulated min SOC keeps the alert reserve; FAIL only when it does not."""
    if not row or row.get("ok") or not sim or sim.get("min_soc") is None:
        return row
    kept = sim["min_soc"] + 0.05 >= ALERT_RESERVE_PCT
    base = row["detail"].split("; risk officer requires")[0]
    return {**row, "ok": kept, "detail": (f"{base}; plan keeps {sim['min_soc']:.0f}% (needs {ALERT_RESERVE_PCT:g}%)")[:220]}
