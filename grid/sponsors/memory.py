"""Agent memory with valid time and supersession.

Each fact: {id, kind, key, value, source, valid_from, valid_until, superseded_by}. remember(kind, key, ...) closes the
current fact for (kind, key) (valid_until=now, superseded_by=new id) and inserts the new one, e.g. each new day-ahead
forecast for an hour supersedes the previous one; "bus 12 must leave at 04:00" supersedes the earlier constraint.

Backend: SurrealDB (SURREAL_URL + SURREAL_TOKEN, HTTP /sql, optional SURREAL_NS / SURREAL_DB) when configured,
else local SQLite at data/memory.db with the same shape. Every call is a database span (db.system surrealdb|sqlite).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

import httpx
from opentelemetry import trace

from grid.config import DATA, env, has

log = logging.getLogger("grid.memory")
_tracer = trace.get_tracer("grid.memory")
_lock = threading.Lock()
DB_PATH = DATA / "memory.db"
_err: dict = {"surreal": None}


def backend() -> str:
    return "surrealdb" if has("SURREAL_URL", "SURREAL_TOKEN") and not _err["surreal"] else "sqlite"


def status() -> dict:
    return {"enabled": True, "backend": backend(), "surreal_keys_present": has("SURREAL_URL", "SURREAL_TOKEN"),
            "surreal_error": _err["surreal"]}


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


# ------------------------------------------------------------------ surrealdb (HTTP /sql)
def _surreal(sql: str, vars_: dict | None = None) -> list:
    url = env("SURREAL_URL").rstrip("/")
    url = url.replace("wss://", "https://").replace("ws://", "http://").removesuffix("/rpc")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {env('SURREAL_TOKEN')}",
               "surreal-ns": env("SURREAL_NS", "grid"), "surreal-db": env("SURREAL_DB", "grid"),
               "NS": env("SURREAL_NS", "grid"), "DB": env("SURREAL_DB", "grid")}
    for k, v in (vars_ or {}).items():
        sql = sql.replace(f"${k}", json.dumps(v))
    r = httpx.post(f"{url}/sql", content=sql, headers=headers, timeout=10)
    r.raise_for_status()
    out = r.json()
    for stmt in out:
        if stmt.get("status") != "OK":
            raise RuntimeError(str(stmt.get("result"))[:200])
    return out[-1].get("result") or []


def _surreal_remember(f: dict) -> None:
    _surreal("UPDATE fact SET valid_until = $t, superseded_by = $id WHERE kind = $kind AND key = $key AND valid_until = NONE;"
             "CREATE type::thing('fact', $id) CONTENT $f;",
             {"t": f["valid_from"], "id": f["id"], "kind": f["kind"], "key": f["key"],
              "f": {k: v for k, v in f.items() if k != "id"} | {"valid_until": None, "superseded_by": None}})


def _surreal_recall(kind, key, history) -> list[dict]:
    where = "kind = $kind" + (" AND key = $key" if key is not None else "") + ("" if history else " AND valid_until = NONE")
    rows = _surreal(f"SELECT * FROM fact WHERE {where} ORDER BY valid_from DESC LIMIT 200;", {"kind": kind, "key": key})
    for r in rows:
        r["id"] = str(r.get("id", "")).split(":", 1)[-1]
    return rows


# ------------------------------------------------------------------ public
def _span(op: str, kind: str):
    b = backend()
    return _tracer.start_as_current_span(f"{op} fact", kind=trace.SpanKind.CLIENT, attributes={
        "db.system": b, "db.operation": op, "db.collection.name": "fact", "db.namespace": "grid-memory",
        "grid.memory.kind": kind, "server.address": "surrealdb" if b == "surrealdb" else "memory.db"})


def remember(kind: str, key: str, value, source: str = "agent") -> dict:
    f = {"id": uuid.uuid4().hex[:12], "kind": kind, "key": str(key), "value": value, "source": source,
         "valid_from": _now(), "valid_until": None, "superseded_by": None}
    with _span("UPSERT", kind):
        if backend() == "surrealdb":
            try:
                _surreal_remember(f)
                return f
            except Exception as e:  # noqa: BLE001
                _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("surrealdb failed, using sqlite: %s", _err["surreal"])
        _sqlite_remember(f)
    return f


def recall(kind: str, key: str | None = None, history: bool = False) -> list[dict]:
    with _span("SELECT", kind):
        if backend() == "surrealdb":
            try:
                return _surreal_recall(kind, key, history)
            except Exception as e:  # noqa: BLE001
                _err["surreal"] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("surrealdb failed, using sqlite: %s", _err["surreal"])
        return _sqlite_recall(kind, key, history)
