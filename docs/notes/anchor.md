# Anchor Browser: notes (2026-10-16)

Docs: https://docs.anchorbrowser.io (index: /llms.txt, append `.md` to any page). Daytona guide: /integrations/sandboxes/daytona.md

## Status with our key: BLOCKED. The account has no credits.
- The key is valid: `GET /v1/sessions/all/status` returns `200 {"data":{"count":0,"items":[]}}` and `GET /v1/profiles` returns 200.
- Every billable call returns **402**:
  - `POST /v1/sessions` (including an empty body `{}`) returns `{"error":"Insufficient credits balance"}`.
  - `POST /v1/tools/fetch-webpage` returns the same 402.
  - `POST /v1/tools/screenshot` returns the same 402.
- Fix: add credits or a plan at app.anchorbrowser.io. The free tier is supposed to grant $5/mo, so this account's balance seems to be used up or was never granted.
  Alternative: the keyless **Agent Access** trial key (`POST /v1/agent-access`). It requires a puzzle challenge with a 120s TTL, and the trial is capped at 1 credit and a 60-minute session. We did not try it, because it creates a new credential and the user should decide.
- So nothing below has been exercised end to end. It comes from the docs and the installed SDK (`anchorbrowser` on PyPI; the SDK call reached the API and returned the 402).

## API (from docs)
- Base `https://api.anchorbrowser.io`, header `anchor-api-key: <ANCHOR_API_KEY>` (`sk-...`).
- Create: `POST /v1/sessions` with body `{"session":{"initial_url":"https://...","timeout":{"max_duration":5,"idle_timeout":2}}}` (minutes).
  The response's `data` holds `id`, `cdp_url` and `live_view_url`.
- End: `DELETE /v1/sessions/{session_id}`. **Closing Playwright only disconnects; the session keeps billing until it is deleted or times out.**
- Live view: `data.live_view_url` (`https://live.anchorbrowser.io/inspector.html?sessionId=...`) can be embedded in an iframe.
  **Never log `cdp_url`: it has the API key in its query string.**
- Sessionless tools:
  - `POST /v1/tools/fetch-webpage` with `{"url":...,"format":"markdown"}`.
  - `POST /v1/tools/screenshot` with `{"url":...}`. Add `?sessionId=` to reuse a session. It returns `image/png`.
- MCP is at `https://api.anchorbrowser.io/mcp`.
- Pricing:
  - $0.01 per session creation plus $0.05 per browser-hour, billed per minute.
  - AI steps cost $0.01–0.10.
  - Proxy is $8/GB; egress without a proxy is $0.20/GB.
  - Free tier: 5 concurrent browsers and 5 session creations per minute.
- Hygiene: use one session per agent run rather than one per page, and always delete it in `finally`.

## Minimal Python (SDK reached the API and got the 402; everything after create_session is untested)
`uv run --with anchorbrowser --with playwright --with python-dotenv python x.py` (connect_over_cdp needs no local browser download)
```python
import os
from anchorbrowser import Anchorbrowser
from playwright.sync_api import sync_playwright
c = Anchorbrowser(api_key=os.environ["ANCHOR_API_KEY"])
s = c.sessions.create_session(session={"timeout": {"max_duration": 5, "idle_timeout": 2}})
print("live view:", s.data.live_view_url)
try:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(s.data.cdp_url)
        ctx = b.contexts[0]; page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.nyiso.com/real-time-dashboard", wait_until="domcontentloaded")
        print(page.title()); page.screenshot(path="nyiso.png")
        page.goto("http://mis.nyiso.com/public/realtime/realtime_zone_lbmp.csv")   # plain CSV, NYC rows say "N.Y.C."
        print([l for l in page.inner_text("body").splitlines() if "N.Y.C." in l][:1])
        b.close()
finally:
    c.sessions.delete_session(s.data.id)
```
A full test script is at `$CLAUDE_JOB_DIR/tmp/anchor_test.py` (/Users/nideeshterapalli/.claude/jobs/e5885773/tmp/anchor_test.py). Rerun it once credits exist.
Note: NYISO's `mis.nyiso.com/public` CSVs need no browser at all. A plain httpx GET works for the price data, so use Anchor only for the dashboard, live view and the demo.
