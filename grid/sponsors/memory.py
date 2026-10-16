"""Agent memory with valid time and supersession.

Each fact: {id, kind, key, value, source, valid_from, valid_until, superseded_by}. remember(kind, key, ...) closes the
current fact for (kind, key) (valid_until=now, superseded_by=new id) and inserts the new one, e.g. each new day-ahead
forecast for an hour supersedes the previous one; "bus 12 must leave at 04:00" supersedes the earlier constraint.

Backend: local SQLite ledger (exact supersession chain) always, mirrored to SurrealDB Agent Memory (SURREAL_URL,
SURREAL_TOKEN, SURREAL_CONTEXT) when configured. Every call is a database span (db.system surrealdb|sqlite).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace

from grid.config import DATA, env, has

log = logging.getLogger("grid.memory")
_tracer = trace.get_tracer("grid.memory")
_lock = threading.Lock()
DB_PATH = DATA / "memory.db"
_err: dict = {"surreal": None}


def surreal_on() -> bool:
    return has("SURREAL_URL", "SURREAL_TOKEN")


def backend() -> str:
    return "surrealdb" if surreal_on() else "sqlite"


def status() -> dict:
    return {"enabled": True, "backend": "surrealdb-agent-memory+sqlite-ledger" if surreal_on() else "sqlite",
            "transport": "mcp" if surreal_on() else None, "surreal_error": _err["surreal"],
            "surreal_writes": _err.get("writes", 0), "last_query": _last["query"], "corrections": _last["corrections"][:5]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ sqlite
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS fact (id TEXT PRIMARY KEY, kind TEXT, key TEXT, value TEXT, source TEXT,
                 valid_from TEXT, valid_until TEXT, superseded_by TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS fact_kk ON fact(kind, key, valid_until)")
    return c


def _sqlite_remember(f: dict) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE fact SET valid_until=?, superseded_by=? WHERE kind=? AND key=? AND valid_until IS NULL",
                  (f["valid_from"], f["id"], f["kind"], f["key"]))
        c.execute("INSERT INTO fact VALUES (?,?,?,?,?,?,?,?)", (f["id"], f["kind"], f["key"], json.dumps(f["value"]),
                                                               f["source"], f["valid_from"], None, None))


def _sqlite_recall(kind, key, history) -> list[dict]:
    q, args = "SELECT * FROM fact WHERE kind=?", [kind]
    if key is not None:
        q += " AND key=?"
        args.append(key)
    if not history:
        q += " AND valid_until IS NULL"
    q += " ORDER BY valid_from DESC LIMIT 200"
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(q, args)]
    for r in rows:
        r["value"] = json.loads(r["value"])
    return rows


# ------------------------------------------------------------------ SurrealDB Agent Memory (hosted, MCP JSON-RPC)
# POST {SURREAL_URL}/mcp tools/call remember {infer:"triples", triples:[{entity:{type,name}, key, value, occurred_at,
# source_clause}], labels:[source=...]} -> Agent Memory supersedes the previous value of (entity, key) itself and
# returns "corrections" [{key, old_value, new_value}]. recall {query, k} -> ranked hits (source=="attribute" = live).
# The local SQLite ledger keeps the full valid_from/valid_until/superseded_by chain for the UI panel.
_last: dict = {"query": None, "corrections": [], "writes": 0}
ENTITY = {"type": "battery", "name": "nyc-battery-1"}


def _mcp(tool: str, **args) -> dict:
    r = httpx.post(env("SURREAL_URL").rstrip("/") + "/mcp", timeout=30, headers={
        "Authorization": f"Bearer {env('SURREAL_TOKEN')}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    r.raise_for_status()
    text = r.text
    if text.lstrip().startswith(("event:", "data:")):  # streamable HTTP may answer as SSE
        text = next(ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:"))
    res = json.loads(text)["result"]
    if res.get("isError"):
        raise RuntimeError(res["content"][0]["text"][:200])
    return res.get("structuredContent") or {}


def _triple(f: dict) -> dict:
    v, k = f["value"], f["key"]
    if f["kind"] == "constraint":
        return {"entity": ENTITY, "key": k, "value": json.dumps(v) if not isinstance(v, (str, int, float)) else str(v),
                "source_clause": str(v.get("text") if isinstance(v, dict) and v.get("text") else f"{k} = {v}")}
    if f["kind"] == "price_forecast":
        return {"entity": {"type": "zone", "name": "NYISO N.Y.C."}, "key": f"da_price_{k[:13]}",
                "value": f"{v.get('usd_mwh')}", "source_clause": f"day-ahead LBMP {k} = ${v.get('usd_mwh')}/MWh"}
    if f["kind"] == "battery":
        name, _, field = k.partition(":")
        return {"entity": {"type": "battery", "name": name}, "key": field, "value": str(v.get("value") if isinstance(v, dict) else v),
                "source_clause": (v.get("text") if isinstance(v, dict) else None) or f"{name} {field} = {v}"}
    if f["kind"] == "plan":
        return {"entity": {"type": "plan", "name": f"trading day {k}"}, "key": "status",
                "value": f"{v.get('status')} plan {v.get('plan_id')} profit ${v.get('profit_usd', v.get('tonight_usd'))}",
                "source_clause": f"plan {v.get('plan_id')} for {k} is {v.get('status')}"}
    return {"entity": {"type": "note", "name": f["kind"]}, "key": k, "value": json.dumps(v)[:300], "source_clause": f["kind"]}


def _am_remember(f: dict) -> dict:
    t = {**_triple(f), "occurred_at": f["valid_from"], "confidence": 0.95}
    out = _mcp("remember", infer="triples", labels=[f"source={f['source']}", f"kind={f['kind']}"], triples=[t])
    corr = (out.get("extraction") or {}).get("corrections") or []
    if corr:
        _last["corrections"] = (corr + _last["corrections"])[:10]
    return out


def ask(question: str, k: int = 8) -> dict:
    """Natural-language recall from Agent Memory (e.g. 'what changed since last night?'); SQLite diff fallback."""
    with _span("QUERY", "recall"):
        if surreal_on():
            try:
                j = _mcp("recall", query=question, k=k)
                hits = [{"text": h.get("text"), "source": h.get("source"), "occurredAt": h.get("occurredAt"),
                         "score": h.get("score")} for h in j.get("hits", [])]
                _last["query"] = {"question": question, "tier": j.get("tier"), "n": len(hits)}
                return {"backend": "surrealdb-agent-memory", "tier": j.get("tier"), "hits": hits,
                        "corrections": _last["corrections"]}
            except Exception as e:  # noqa: BLE001
                _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("agent memory recall failed: %s", _err["surreal"])
        return {"backend": "sqlite", "tier": "ledger", "hits": [{"text": c} for c in changes_since()]}


def changes_since(hours: float = 24) -> list[str]:
    """Superseded facts in the last N hours, as plain sentences (old -> new)."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT o.kind, o.key, o.value AS old, n.value AS new, n.source FROM fact o JOIN fact n ON n.id = o.superseded_by "
            "WHERE o.valid_until >= ? ORDER BY o.valid_until DESC LIMIT 20", (cutoff,))]
    out = []
    for r in rows:
        if r["old"] != r["new"]:
            out.append(f"{r['kind']} {r['key']}: {r['old']} -> {r['new']} ({r['source']})")
    return out


# ------------------------------------------------------------------ public
def _span(op: str, kind: str):
    b = "surrealdb" if op == "QUERY" and surreal_on() else "sqlite"
    return _tracer.start_as_current_span(f"{op} fact", kind=trace.SpanKind.CLIENT, attributes={
        "db.system": b, "db.operation": op, "db.collection.name": "fact", "db.namespace": "grid-memory",
        "grid.memory.kind": kind, "server.address": "surrealdb" if b == "surrealdb" else "memory.db"})


def remember(kind: str, key: str, value, source: str = "agent", mirror: bool | None = None) -> dict:
    f = {"id": uuid.uuid4().hex[:12], "kind": kind, "key": str(key), "value": value, "source": source,
         "valid_from": _now(), "valid_until": None, "superseded_by": None}
    with _span("UPSERT", kind):
        _sqlite_remember(f)
    if mirror is None:
        mirror = kind != "price_forecast"  # forecasts: mirrored in one batch by remember_forecasts()
    if mirror and surreal_on():
        ctx = otel_context.get_current()

        def _bg():
            tok = otel_context.attach(ctx)
            try:
                with _tracer.start_as_current_span("POST facts", kind=trace.SpanKind.CLIENT, attributes={
                        "db.system": "surrealdb", "db.operation": "REMEMBER", "db.collection.name": "facts",
                        "db.namespace": "agent-memory", "grid.memory.kind": kind, "server.address": "surreal.cloud"}) as dsp:
                    out = _am_remember(f)
                    dsp.set_attribute("grid.memory.corrections", len((out.get("extraction") or {}).get("corrections") or []))
                _err["writes"] = _err.get("writes", 0) + 1
                _err["surreal"] = None
            except Exception as e:  # noqa: BLE001
                _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("agent memory write failed (sqlite ledger kept): %s", _err["surreal"])
            finally:
                otel_context.detach(tok)

        threading.Thread(target=_bg, daemon=True).start()
    return f


def remember_forecasts(rows: list[dict], source: str) -> None:
    """Hourly day-ahead forecasts: SQLite ledger per hour + ONE Agent Memory write with all triples (each hour's new
    forecast supersedes the previous forecast for that hour)."""
    facts = [remember("price_forecast", h["ts"], {"usd_mwh": h["usd_mwh"]}, source, mirror=False) for h in rows]
    if not (surreal_on() and facts):
        return
    ctx = otel_context.get_current()

    def _bg():
        tok = otel_context.attach(ctx)
        try:
            with _tracer.start_as_current_span("POST facts", kind=trace.SpanKind.CLIENT, attributes={
                    "db.system": "surrealdb", "db.operation": "REMEMBER", "db.collection.name": "facts",
                    "db.namespace": "agent-memory", "grid.memory.kind": "price_forecast", "server.address": "surreal.cloud"}):
                triples = [{**_triple(f), "occurred_at": f["valid_from"], "confidence": 0.9} for f in facts]
                out = _mcp("remember", infer="triples", labels=[f"source={source[:60]}", "kind=price_forecast"], triples=triples)
                corr = (out.get("extraction") or {}).get("corrections") or []
                if corr:
                    _last["corrections"] = (corr + _last["corrections"])[:10]
            _err["writes"] = _err.get("writes", 0) + 1
        except Exception as e:  # noqa: BLE001
            _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
        finally:
            otel_context.detach(tok)

    threading.Thread(target=_bg, daemon=True).start()


def recall(kind: str, key: str | None = None, history: bool = False) -> list[dict]:
    with _span("SELECT", kind):
        return _sqlite_recall(kind, key, history)


def recent(limit: int = 20) -> list[dict]:
    """Last facts across kinds (with superseded history) for the UI panel; price forecasts collapsed."""
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM fact WHERE kind != 'price_forecast' OR superseded_by IS NOT NULL ORDER BY valid_from DESC LIMIT ?", (limit,))]
    for r in rows:
        r["value"] = json.loads(r["value"])
        r["text"] = (r["value"].get("text") if isinstance(r["value"], dict) and r["value"].get("text") else _triple(r)["source_clause"])
    return rows
