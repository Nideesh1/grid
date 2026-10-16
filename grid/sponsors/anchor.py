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
        r.raise_for_status()
        _state["last"] = {"url": url, "chars": len(r.text)}
        _state["error"] = None
        return {"url": url, "excerpt": r.text[:600]}
    except Exception as e:  # noqa: BLE001
        _state["error"] = type(e).__name__
        log.warning("anchor cross-check failed: %s", _state["error"])
        return None
