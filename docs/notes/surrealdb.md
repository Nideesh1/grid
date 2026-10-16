# SurrealDB Agent Memory (hosted, formerly "Spectron"): verified notes (2026-10-16)

Docs: https://surrealdb.com/docs/agent-memory (index: https://surrealdb.com/docs/llms.txt). Append `.md` to any docs URL to get raw markdown.
Key pages: quickstarts/hosted, reference/rest-api, reference/sdk-python, reasoning/temporal-validity.

## What works with our key (verified with live calls)
- Host `SURREAL_URL` (`$SURREAL_URL`), key `SURREAL_TOKEN` (`sp-...`).
- Auth: `Authorization: Bearer <SURREAL_TOKEN>`.
- **MCP at `POST {SURREAL_URL}/mcp` (JSON-RPC, streamable HTTP) works WITHOUT a context id.** The key is bound to its context.
  No session header is needed: each POST is stateless. Send `Accept: application/json, text/event-stream`.
  Tools: `remember, recall, context, reflect, forget, upload, inspect`.
- **REST `/api/v1/{context_id}/...` is BLOCKED.** We don't know our context id. Every guess returns 401 `invalid api key`, because the key is
  valid only for its own context. The id is shown in SurrealDB Studio, under the context's Settings / API keys. Once the id is set as `SURREAL_CONTEXT`,
  REST also unlocks `/entities/{type}/{name}/history`, `/uncertainty/{id}/resolve`, and `/query` with `asOf`, none of which MCP exposes.
  `GET /api/v1/health` returns 200 without auth.

## Write a fact (MCP `remember`)
Free text with LLM extraction: about 4s.
```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"remember","arguments":{
  "text":"Depot rule: bus 12 departs at 05:00","infer":"full","labels":["source=depot-rulebook"]}}}
```
Response, abridged from `structuredContent`:
```json
{"extraction":{"entities":[{"entity_type":"product","name":"bus 12","is_new":true}],
 "attributes":[{"entity_id":"['product', 'bus_12']","id":"gfrd5ev2o12j5sf8stwv","key":"departure_time","value":"05:00"}],
 "corrections":[],"uncertainties":[]},"mode":"full","session_id":"...","turn_id":"...","trace_id":"..."}
```
Structured triple, deterministic with no LLM: about 1s. **This is the recommended path.**
```json
{"name":"remember","arguments":{"infer":"triples","labels":["source=depot-memo-2026-10-16"],"triples":[{
  "entity":{"type":"vehicle","name":"bus 14"},"key":"departure_time","value":"04:00",
  "occurred_at":"2026-10-16T00:00:00Z",          // sets valid_from (valid time)
  "source_clause":"bus 14 departs at 04:00 starting 2026-10-16","confidence":0.95}]}}
```
Fields:
- Provenance goes in `labels` (`key=value`, filterable on recall) and `source_clause` (verbatim text).
- `observed_at` (RFC 3339) sets known time.
- `scope` takes a DNF list of paths. Paths must be registered first, so we skipped scopes.

## Supersession (verified)
- **Triple then triple on the same entity and key supersedes automatically.** The second write returned
  `"corrections":[{"key":"departure_time","old_value":"05:00","new_value":"04:00"}]`. After that, `inspect` shows only the 04:00 attribute,
  with `valid_from: 2026-10-16T00:00:00Z` and `valid_until: null`. The old value no longer appears in `inspect` or `recall`. Per the docs it is kept with `valid_until` set and can be read through REST `.../history`, which we have not verified.
- **Mixed provenance does NOT supersede.** For bus 12, the first write was an LLM "full" turn and the correction was a triple. The response returned
  `"uncertainties":[{"about":"entity.departure_time (turn vs upsert)","reason":"cross-provenance contradiction: prior turn fact = \"05:00\"; new upsert fact = \"04:00\""}]`
  and both attributes stay live. Fixing it needs REST `POST /uncertainty/{id}/resolve` with `{"acceptedValue":"04:00"}`, which requires the context id.
- A free-text correction ("Correction to depot rule: bus 12 departs at 04:00 starting 2026-10-16 (previously 05:00).")
  extracted **no attribute at all**. The LLM extraction is flaky for corrections. Use triples.

## Recall (MCP `recall`, about 2s; the first cold query took 5.6s at tier `escalated`)
```json
{"name":"recall","arguments":{"query":"when does bus 12 depart?","k":5}}
```
```json
{"tier":"hybrid","query_ms":2185,"trace_id":"cpdvlwemy8oenu0bkzcg","hits":[
 {"id":"attribute:cxk1kp4jubzr0yavvz7c","source":"attribute","occurredAt":"2026-10-16T00:00:00Z","score":0.49,"text":"bus 12's departure time is 04:00"},
 {"id":"memory_chunk:pg676jm9u191ras7btc9","source":"memory_chunk","score":0.38,"text":"Depot rule: bus 12 departs at 05:00"}, ...]}
```
- MCP `context` returns a markdown block ready to put in a system prompt. Under "Preferences by entity" it lists the live attributes, and for bus 12 it showed BOTH 04:00 and 05:00 because of the unresolved conflict.
- `inspect` with `ref: "entity:<type>/<name>"` returns the live attributes with `valid_from`, `valid_until` and `created_at`. With `ref: "trace:<id>"` it returns the tier reasoning.
- Raw text chunks (`memory_chunk`) from "full" writes keep appearing in recall even after supersession. Filter on `source=="attribute"` when you want current truth.

## Gotchas
- Entity types get normalized: `vehicle` was stored as `other` (`entity:other/bus_14`). The `product` type came from the LLM. Inspect still resolves the name you passed.
- Write bodies use snake_case (`observed_at`). REST read bodies use camelCase (`asOf`, `scopeView`). Unknown fields are silently ignored.
- Writes are durable, shared memory in the context. Test entities bus 12, 14 and 15 now exist there.

## Minimal Python (verified): plain httpx against MCP, no context id needed
```python
import os, httpx
URL = os.environ["SURREAL_URL"].rstrip("/") + "/mcp"
H = {"Authorization": f"Bearer {os.environ['SURREAL_TOKEN']}", "Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}
def call(tool, **args):
    r = httpx.post(URL, headers=H, timeout=60, json={"jsonrpc":"2.0","id":1,"method":"tools/call",
                                                     "params":{"name":tool,"arguments":args}})
    r.raise_for_status(); res = r.json()["result"]
    if res.get("isError"): raise RuntimeError(res["content"][0]["text"])
    return res["structuredContent"]

call("remember", infer="triples", labels=["source=nyiso-dam"], triples=[{"entity":{"type":"zone","name":"NYC"},
     "key":"da_price_2026-10-17T14", "value":"42.10", "occurred_at":"2026-10-16T10:00:00Z", "source_clause":"..."}])
hits = call("recall", query="NYC day-ahead price", k=5)["hits"]
```
Official SDK (not tested, because it needs a context id): `uv add --prerelease allow 'surrealdb[memory]'`, then
`from surrealdb.memory import Memory; m = Memory(context=CTX, endpoint=SURREAL_URL, api_key=KEY)`. It provides `remember`, `recall`, `entities.history` and `query_context`.
