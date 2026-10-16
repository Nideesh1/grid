"""Anchor Browser (ANCHOR_API_KEY): optional cross-check of the public NYISO price page in a cloud browser.
Public pages only, no logins. Disabled unless the key is set; the demo never depends on it."""
from __future__ import annotations

import logging

import threading

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
API = "https://api.anchorbrowser.io"
CACHE_S = 300
_check_lock = threading.Lock()
_cache: dict = {"t": 0.0, "row": None}


def _h() -> dict:
    return {"anchor-api-key": env("ANCHOR_API_KEY"), "Content-Type": "application/json"}


def _delete(sid: str) -> None:
    try:
        httpx.delete(f"{API}/v1/sessions/{sid}", headers=_h(), timeout=15)
    except Exception:  # noqa: BLE001
        log.warning("anchor session delete failed for one session")


def cleanup_stale(max_age_s: float = 300) -> int:
    """Delete running sessions older than max_age_s (startup hygiene; the account allows 5 concurrent)."""
    if not has("ANCHOR_API_KEY"):
        return 0
    from datetime import datetime, timezone

    n = 0
    try:
        items = httpx.get(f"{API}/v1/sessions/all/status", headers=_h(), timeout=15).json().get("data", {}).get("items", [])
        now = datetime.now(timezone.utc)
        for it in items:
            sid = it.get("id") or it.get("session_id")
            started = it.get("created_at") or it.get("start_time") or it.get("started_at")
            age = None
            try:
                age = (now - datetime.fromisoformat(str(started).replace("Z", "+00:00"))).total_seconds()
            except Exception:  # noqa: BLE001
                pass
            if sid and (age is None or age > max_age_s):
                _delete(sid)
                n += 1
    except Exception as e:  # noqa: BLE001
        log.info("anchor cleanup skipped: %s", type(e).__name__)
    if n:
        log.info("anchor: deleted %d stale sessions", n)
    return n


def _browse(box: dict, url: str = DASHBOARD, fmt: str = "html") -> None:
    """Worker: create a short-lived session, render `url`, record the session id so the caller can delete it."""
    try:
        r = httpx.post(f"{API}/v1/sessions", headers=_h(), timeout=20,
                       json={"session": {"timeout": {"max_duration": 2, "idle_timeout": 1}}})
        if r.status_code in (402, 429):
            raise RuntimeError(f"Anchor {r.status_code}: {r.text[:80]}")
        r.raise_for_status()
        box["sid"] = r.json()["data"]["id"]
        if box.get("abandoned"):
            return
        r2 = httpx.post(f"{API}/v1/tools/fetch-webpage", params={"sessionId": box["sid"]}, headers=_h(), timeout=30,
                        json={"url": url, "format": fmt})
        r2.raise_for_status()
        box["html"] = r2.text
    except Exception as e:  # noqa: BLE001
        box["error"] = str(e)[:120] if isinstance(e, RuntimeError) else type(e).__name__
    finally:
        if box.get("sid") and not box.get("deleted"):
            box["deleted"] = True
            _delete(box["sid"])


def cross_check_price(timeout: float = 15.0) -> dict | None:
    """Render NYISO's public real-time dashboard in an Anchor cloud browser (one short-lived session, always deleted),
    read the NYC-J zone price on its map and compare it with our CSV pipeline (today's day-ahead price for the current
    hour and the latest real-time 5-min price). One check at a time; result cached 5 minutes."""
    import re
    import time as _t
    from datetime import date as _date, datetime

    if not enabled():
        return None
    if _cache["row"] and _t.time() - _cache["t"] < CACHE_S:
        return _cache["row"]
    if not _check_lock.acquire(blocking=False):
        return _cache["row"]  # another check is running: never open a second browser
    box: dict = {}
    try:
        th = threading.Thread(target=_browse, args=(box,), daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():  # timed out: make sure the session dies even though the worker is still waiting
            box["abandoned"] = True
            if box.get("sid") and not box.get("deleted"):
                box["deleted"] = True
                _delete(box["sid"])
            _state["error"] = f"timeout after {timeout:g}s"
            return None
        if box.get("error") or not box.get("html"):
            _state["error"] = box.get("error") or "empty page"
            return None
        m = re.search(r'ib-title">\s*NYC[^<]*</div>\s*<div class="ib-price">\s*\$?(-?\d+(?:\.\d+)?)', box["html"])
        if not m:
            _state["error"] = "NYC price not found on dashboard"
            return None
        live = float(m.group(1))
        from grid import prices

        hour = datetime.now().hour
        da = {p["ts"][11:13]: p["usd_mwh"] for p in prices.day_ahead(_date.today())}
        rt = prices.real_time(_date.today())
        cands = {}
        if f"{hour:02d}" in da:
            cands["day-ahead this hour"] = float(da[f"{hour:02d}"])
        if rt:
            cands["latest real-time"] = float(rt[-1]["usd_mwh"])
        if not cands:
            return None
        kind, ours = min(cands.items(), key=lambda kv: abs(kv[1] - live))
        diff = abs(live - ours)
        row = {"name": "Price cross-checked in live browser (Anchor)", "ok": diff <= 1.0,
               "detail": f"NYISO dashboard NYC ${live:.2f}/MWh vs our CSV {kind} ${ours:.2f} (+-${diff:.2f})"}
        _state.update(error=None, last={"url": DASHBOARD, "price": live, "matched": kind})
        _cache.update(t=_t.time(), row=row)
        return row
    finally:
        _check_lock.release()


def render_page(url: str, fmt: str = "html", timeout: float = 25.0) -> tuple[str | None, str | None]:
    """Render one public page in a short-lived Anchor session (always deleted, also on timeout).
    Returns (text, None) or (None, error). Caller is responsible for its own locking/caching."""
    if not enabled():
        return None, "anchor disabled"
    box: dict = {}
    th = threading.Thread(target=_browse, args=(box, url, fmt), daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        box["abandoned"] = True
        if box.get("sid") and not box.get("deleted"):
            box["deleted"] = True
            _delete(box["sid"])
        _state["error"] = f"timeout after {timeout:g}s"
        return None, _state["error"]
    if box.get("error") or not box.get("html"):
        _state["error"] = box.get("error") or "empty page"
        return None, _state["error"]
    _state.update(error=None, last={"url": url, "chars": len(box["html"])})
    return box["html"], None
