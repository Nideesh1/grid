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
    return has("SURREAL_URL", "SURREAL_TOKEN", "SURREAL_CONTEXT")


def backend() -> str:
    return "surrealdb" if surreal_on() else "sqlite"


def status() -> dict:
    return {"enabled": True, "backend": "surrealdb-agent-memory+sqlite-ledger" if surreal_on() else "sqlite",
            "surreal_keys_present": has("SURREAL_URL", "SURREAL_TOKEN"), "surreal_context_set": has("SURREAL_CONTEXT"),
            "surreal_error": _err["surreal"], "surreal_writes": _err.get("writes", 0), "last_query": _last["query"]}


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


# ------------------------------------------------------------------ SurrealDB Agent Memory (hosted REST)
# POST {SURREAL_URL}/api/v1/{SURREAL_CONTEXT}/facts  {text, infer, scopes, observed_at}  (snake_case write body)
# POST {SURREAL_URL}/api/v1/{SURREAL_CONTEXT}/query  {query, k, lens}                    (camelCase read body)
# Agent Memory extracts entities/attributes and supersedes older attribute values itself; the local SQLite ledger
# keeps the exact valid_from/valid_until/superseded_by chain the UI shows. Writes go to both; recall prefers Agent Memory.
SCOPE = "grid/depot"
_last: dict = {"query": None}


def _am_url(path: str) -> str:
    return f"{env('SURREAL_URL').rstrip('/')}/api/v1/{env('SURREAL_CONTEXT')}/{path}"


def _am_headers() -> dict:
    return {"Authorization": f"Bearer {env('SURREAL_TOKEN')}", "Content-Type": "application/json"}


def _fact_text(f: dict) -> str:
    v, k = f["value"], f["key"]
    if f["kind"] == "constraint":
        parts = []
        if v.get("departure_hour") is not None:
            h = v["departure_hour"] % 24
            parts.append(f"must depart at {int(h):02d}:{int(round(h % 1 * 60)):02d}")
        if v.get("arrival_hour") is not None:
            h = v["arrival_hour"] % 24
            parts.append(f"arrives at {int(h):02d}:{int(round(h % 1 * 60)):02d}")
        return f"Depot constraint: bus {v.get('bus_id')} {' and '.join(parts) or 'has default schedule'} (source {f['source']})."
    if f["kind"] == "price_forecast":
        return f"NYISO day-ahead price forecast for N.Y.C. at {k} is ${v.get('usd_mwh')}/MWh (source {f['source']})."
    if f["kind"] == "plan":
        return (f"Charging plan {v.get('plan_id')} for the night of {k} is {v.get('status')}: saves "
                f"${v.get('tonight_usd')} tonight and ${v.get('monthly_usd')} per month (source {f['source']}).")
    return f"{f['kind']} {k}: {json.dumps(v)} (source {f['source']})."


def _am_remember(f: dict) -> None:
    body = {"text": _fact_text(f), "infer": "full", "scopes": [[SCOPE]], "observed_at": f["valid_from"]}
    r = httpx.post(_am_url("facts"), headers=_am_headers(), json=body, timeout=20)
    if r.status_code in (400, 403, 422) and "scope" in r.text.lower():  # scope not registered: write unscoped
        body.pop("scopes")
        r = httpx.post(_am_url("facts"), headers=_am_headers(), json=body, timeout=20)
    r.raise_for_status()


def ask(question: str, k: int = 8) -> dict:
    """Natural-language recall from Agent Memory (e.g. 'what changed since last night?'); SQLite diff fallback."""
    with _span("QUERY", "recall"):
        if surreal_on():
            try:
                r = httpx.post(_am_url("query"), headers=_am_headers(), json={"query": question, "k": k}, timeout=30)
                r.raise_for_status()
                j = r.json()
                hits = [{"text": h.get("text") or h.get("content") or h.get("value") or json.dumps(h)[:200],
                         "source": h.get("source"), "occurredAt": h.get("occurredAt")} for h in j.get("hits", [])]
                _last["query"] = {"question": question, "tier": j.get("tier"), "n": len(hits)}
                return {"backend": "surrealdb-agent-memory", "tier": j.get("tier"), "hits": hits}
            except Exception as e:  # noqa: BLE001
                _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("agent memory query failed: %s", _err["surreal"])
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


def remember(kind: str, key: str, value, source: str = "agent", mirror: bool = True) -> dict:
    f = {"id": uuid.uuid4().hex[:12], "kind": kind, "key": str(key), "value": value, "source": source,
         "valid_from": _now(), "valid_until": None, "superseded_by": None}
    with _span("UPSERT", kind):
        _sqlite_remember(f)
    if mirror and surreal_on():
        ctx = otel_context.get_current()

        def _bg():
            tok = otel_context.attach(ctx)
            try:
                with _tracer.start_as_current_span("POST facts", kind=trace.SpanKind.CLIENT, attributes={
                        "db.system": "surrealdb", "db.operation": "REMEMBER", "db.collection.name": "facts",
                        "db.namespace": "agent-memory", "grid.memory.kind": kind, "server.address": "surreal.cloud"}):
                    _am_remember(f)
                _err["writes"] = _err.get("writes", 0) + 1
                _err["surreal"] = None
            except Exception as e:  # noqa: BLE001
                _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("agent memory write failed (sqlite ledger kept): %s", _err["surreal"])
            finally:
                otel_context.detach(tok)

        threading.Thread(target=_bg, daemon=True).start()
    return f


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
        r["text"] = _fact_text(r)
    return rows
