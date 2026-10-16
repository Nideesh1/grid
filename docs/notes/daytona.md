# Daytona notes (verified 2026-10-16, SDK `daytona` from PyPI, API v0.223)

Run: `uv run --with daytona --with python-dotenv python x.py` (reads DAYTONA_API_KEY from .env).

## Measured on our org (Tier: 10 vCPU total, default sandbox = 1 CPU / 1 GiB / 3 GiB disk)
| op | time |
|---|---|
| create (default python snapshot `daytonaio/sandbox:0.8.0`) | ~1.0 s |
| upload / download small file | 0.37 s / 0.47 s |
| process.exec | ~1.0 s ; code_run ~0.5 s |
| numpy + scipy | ALREADY in default image (scipy 1.17.0); `pip install` is a 0.9 s no-op |
| scipy linprog inside sandbox | 1.4 s first import |
| stop / start | 1.0 s / 0.9 s |
| dynamic Image (debian_slim + pip scipy), first build | 61 s ; same image again (cached) 0.9 s |
| 8 sandboxes in parallel (AsyncDaytona, create+run+delete) | 2.9 s wall |
| 30 LP runs, semaphore 8 + retry on CPU-limit | 13.5 s wall, 30/30 ok |
| git.clone (Hello-World) | 0.7 s |

## Limits / gotchas
- **10 vCPU org cap**: 10+ concurrent 1-CPU creates fail `Total CPU limit exceeded`. Deletes free quota with lag, so use concurrency 8 and retry.
- `d.snapshot.create(...)` and `d.volume.get(...)` -> **Access denied** on this tier. Use `CreateSandboxFromImageParams(image=...)` instead (Daytona auto-caches the built image).
- `d.list()` returns a generator; filter via `ListSandboxesQuery(labels={...})`.
- Inside sandbox `nproc`=64 and `free` shows host RAM; the real limit is the cgroup (1 CPU/1 GiB).
- Rate limit header: 600 sandbox-creates / min.
- `ephemeral=True` = deleted on stop. Defaults: auto_stop 15 min, auto_archive 7 days.

## Snippets
```python
from dotenv import load_dotenv; load_dotenv(".env")
from daytona import (Daytona, AsyncDaytona, CreateSandboxFromSnapshotParams,
    CreateSandboxFromImageParams, Image, Resources, ListSandboxesQuery)
d = Daytona()
sb = d.create(CreateSandboxFromSnapshotParams(language="python",
        labels={"app": "grid", "night": "2026-10-15"},
        auto_stop_interval=15, auto_delete_interval=60))   # minutes; or ephemeral=True
sb.fs.upload_file(json.dumps(plan).encode(), "/home/daytona/plan.json")
r = sb.process.code_run("import json; print(json.load(open('/home/daytona/plan.json')))")
print(r.exit_code, r.result)
r = sb.process.exec("pip install -q pandapower", timeout=180)   # shell cmd
data = sb.fs.download_file("/home/daytona/plan.json")            # bytes
d.delete(sb)
```
Pre-baked image (first build ~60 s, then ~1 s):
```python
img = Image.debian_slim("3.12").pip_install(["numpy", "scipy", "pandas"])
sb = d.create(CreateSandboxFromImageParams(image=img, resources=Resources(cpu=2, memory=2),
        ephemeral=True), timeout=300, on_snapshot_create_logs=print)
```
Preview URL (expose a port):
```python
sb.process.exec("nohup python3 -m http.server 8080 --directory /home/daytona >/tmp/h.log 2>&1 &")
p = sb.get_preview_link(8080)   # https://8080-<id>.daytonaproxy01.net, needs header
urllib.request.urlopen(urllib.request.Request(p.url, headers={"x-daytona-preview-token": p.token}))
s = sb.create_signed_preview_url(8080, expires_in_seconds=600)  # s.url works with no header (browser-shareable)
```
Parallel fan-out (async):
```python
async def fan(scenarios, conc=8):
    sem = asyncio.Semaphore(conc)
    async with AsyncDaytona() as ad:
        async def one(code):
            async with sem:
                for _ in range(5):
                    try: sb = await ad.create(CreateSandboxFromSnapshotParams(language="python", ephemeral=True, labels={"app": "grid"})); break
                    except Exception as e:
                        if "CPU limit" not in str(e): raise
                        await asyncio.sleep(1.5)
                try: return (await sb.process.code_run(code)).result
                finally: await ad.delete(sb)
        return await asyncio.gather(*[one(c) for c in scenarios])
```
Other verified: `sb.git.clone(url, path)`, `sb.stop()/start()`, `sb.set_labels`, `sb.computer_use.start()` (starts Xvfb + xfce4 + x11vnc + noVNC: a full desktop in the sandbox). Cleanup: `for s in d.list(ListSandboxesQuery(labels={"app":"grid"})): d.delete(s)`.
Also in SDK (not tested): `sb.fork()`, `sb.create_snapshot()`, `network_block_all` / `domain_allow_list` (egress lockdown), `secrets`, `resize`, `get_metrics`, `create_ssh_access`, GPU types, warm pools.

## Proposed usages for the energy dispatcher
1. **Backtest / what-if fan-out**: one ephemeral sandbox per night or scenario (30 nights, or N weather/price/outage what-ifs), each solving the 40-bus LP with scipy. Measured 30 LPs in 13.5 s wall on a 10-CPU tier (8 concurrent). Show a grid of 30 sandbox IDs lighting up live in the UI.
2. **Agent-written analysis code run safely**: the LLM writes Python (e.g. "why was bus 17 charged at 2am?"), we run it with `code_run` in a sandbox with `network_block_all=True`, return stdout and charts. Untrusted code never touches our server. ~1 s create + 0.5 s run.
3. **Pre-baked sim image + live preview**: `Image.debian_slim().pip_install([...our sim deps])` (61 s once, then 0.9 s), and the sandbox serves the plan visualization on a signed preview URL judges can open in a browser.
