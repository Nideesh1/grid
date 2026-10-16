"""Anchor Browser (ANCHOR_API_KEY): optional cross-check of the public NYISO price page in a cloud browser.
Public pages only, no logins. Disabled unless the key is set; the demo never depends on it."""
from __future__ import annotations

import logging

import httpx

from grid.config import env, has

log = logging.getLogger("grid.anchor")
NYISO_PAGE = "https://www.nyiso.com/energy-market-operational-data"
_state = {"error": None, "last": None}


def enabled() -> bool:
    return has("ANCHOR_API_KEY")


def status() -> dict:
    return {"enabled": enabled(), "error": _state["error"], "last_check": _state["last"]}


def cross_check(url: str = NYISO_PAGE) -> dict | None:
    """Fetch a public page through Anchor's tools API and return a short text excerpt."""
    if not enabled():
        return None
    try:
        r = httpx.post("https://api.anchorbrowser.io/v1/tools/fetch-webpage",
                       headers={"anchor-api-key": env("ANCHOR_API_KEY"), "Content-Type": "application/json"},
                       json={"url": url, "format": "markdown"}, timeout=60)
        if r.status_code == 402:
            raise RuntimeError("Anchor account has insufficient credits (402)")
        r.raise_for_status()
        _state["last"] = {"url": url, "chars": len(r.text)}
        _state["error"] = None
        body = r.text
        try:
            j = r.json()
            body = j.get("content") or j.get("data") or j.get("markdown") or body if isinstance(j, dict) else body
        except Exception:  # noqa: BLE001
            pass
        return {"url": url, "excerpt": str(body)[:600], "full": str(body)}
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)[:120] if isinstance(e, RuntimeError) else type(e).__name__
        log.warning("anchor cross-check failed: %s", _state["error"])
        return None


def cross_check_price(d=None) -> dict | None:
    """Open NYISO's public real-time zonal LBMP file for date d in an Anchor cloud browser, read the latest N.Y.C.
    price and compare it with the same interval from our CSV pipeline (grid/prices.py). Returns a check row or None."""
    if not enabled():
        return None
    import csv
    import io
    from datetime import date as _date

    from grid import prices

    d = d or _date.today()
    ds = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
    url = f"http://mis.nyiso.com/public/csv/realtime/{ds}realtime_zone.csv"
    got = cross_check(url)
    if not got:
        return None
    try:
        text = got.get("full") or ""
        rows = [row for row in csv.reader(io.StringIO(text)) if len(row) > 3 and row[1] == "N.Y.C."]
        if not rows:
            return {"name": "Price cross-checked in live browser (Anchor)", "ok": False, "detail": "N.Y.C. row not found"}
        ts, live = rows[-1][0], float(rows[-1][3])
        ours = {p["ts"]: p["usd_mwh"] for p in prices.real_time(d)}
        from datetime import datetime

        key = datetime.strptime(ts, "%m/%d/%Y %H:%M:%S").isoformat()
        mine = ours.get(key)
        diff = None if mine is None else abs(live - float(mine))
        return {"name": "Price cross-checked in live browser (Anchor)", "ok": diff is not None and diff <= 1.0,
                "detail": f"{ts} N.Y.C. ${live:.2f}/MWh" + (f", CSV pipeline +-${diff:.2f}" if diff is not None else ", interval not yet in cache")}
    except Exception as e:  # noqa: BLE001
        _state["error"] = type(e).__name__
        return None
