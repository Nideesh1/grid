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
    """Off unless ANCHOR_ENABLED=1 (the account has no credits; keeps plans from paying a failed call)."""
    return has("ANCHOR_API_KEY") and env("ANCHOR_ENABLED", "1") != "0"


def status() -> dict:
    return {"enabled": enabled(), "key_present": has("ANCHOR_API_KEY"),
            "note": None if enabled() else "disabled (ANCHOR_ENABLED=0 or no key)", "error": _state["error"], "last_check": _state["last"]}


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


DASHBOARD = "https://www.nyiso.com/real-time-dashboard"


def cross_check_price(timeout: float = 8.0) -> dict | None:
    """Open NYISO's public real-time dashboard in an Anchor cloud browser (fetch-webpage tool, no login), read the
    N.Y.C. zone price shown there and compare with the latest 5-min price from our CSV pipeline (grid/prices.py).
    Returns a check row, or None if Anchor is off / slow / the page has no N.Y.C. price."""
    if not enabled():
        return None
    import re
    from datetime import date as _date

    from grid import prices

    try:
        r = httpx.post("https://api.anchorbrowser.io/v1/tools/fetch-webpage", timeout=timeout,
                       headers={"anchor-api-key": env("ANCHOR_API_KEY"), "Content-Type": "application/json"},
                       json={"url": DASHBOARD, "format": "markdown"})
        if r.status_code in (402, 429):
            raise RuntimeError(f"Anchor {r.status_code}: {r.text[:80]}")
        r.raise_for_status()
        text = r.text
        _state["last"] = {"url": DASHBOARD, "chars": len(text)}
        m = re.search(r"N\.?\s?Y\.?\s?C\.?[^0-9$-]{0,80}\$?\s*(-?\d{1,4}(?:\.\d{1,2})?)", text)
        if not m:
            _state["error"] = "N.Y.C. price not found on dashboard"
            return None
        live = float(m.group(1))
        rt = prices.real_time(_date.today())
        ours = float(rt[-1]["usd_mwh"]) if rt else None
        diff = None if ours is None else abs(live - ours)
        _state["error"] = None
        return {"name": "Price cross-checked in live browser (Anchor)", "ok": diff is not None and diff <= 10.0,
                "detail": f"NYISO dashboard N.Y.C. ${live:.2f}/MWh vs CSV ${ours:.2f} (+-${diff:.2f})" if diff is not None
                else f"NYISO dashboard N.Y.C. ${live:.2f}/MWh"}
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)[:120] if isinstance(e, RuntimeError) else type(e).__name__
        log.info("anchor cross-check skipped: %s", _state["error"])
        return None
