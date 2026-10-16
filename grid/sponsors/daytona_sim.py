"""Daytona: run optimize.simulate() for a plan inside an isolated Daytona sandbox.

Uploads grid/{optimize,depot,bill}.py + inputs.json, runs a tiny runner, parses JSON between markers.
One sandbox per API process (lazy, reused, deleted on shutdown). Fallback: local subprocess, flagged "local".
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from grid.config import ROOT, env, has

log = logging.getLogger("grid.daytona")
MODULES = ["optimize.py", "depot.py", "bill.py"]
MARK = "@@GRID_RESULT@@"
RUNNER = f"""
import json, sys
sys.path.insert(0, '.')
from grid.optimize import simulate
from grid.depot import Depot
inp = json.load(open('inputs.json'))
res = simulate(inp['plan'], Depot.from_dict(inp['depot']), inp['prices'])
print('{MARK}' + json.dumps(res, default=float))
"""

_lock = threading.Lock()
_state: dict = {"client": None, "sandbox": None, "error": None, "runs": 0}


def enabled() -> bool:
    return has("DAYTONA_API_KEY")


def status() -> dict:
    sb = _state["sandbox"]
    return {"enabled": enabled(), "sandbox_id": getattr(sb, "id", None), "runs": _state["runs"], "error": _state["error"]}


def _sandbox():
    if _state["sandbox"] is not None:
        return _state["sandbox"]
    from daytona import Daytona, DaytonaConfig

    _state["client"] = Daytona(DaytonaConfig(api_key=env("DAYTONA_API_KEY")))
    sb = _state["client"].create()
    sb.process.exec("mkdir -p grid && touch grid/__init__.py")
    _state["sandbox"] = sb
    log.info("daytona sandbox %s created", sb.id)
    return sb


def sandbox_id() -> str | None:
    """Create (or reuse) the sandbox and return its id; used for the status line before the run."""
    if not enabled():
        return None
    with _lock:
        try:
            return _sandbox().id
        except Exception as e:  # noqa: BLE001
            _state["error"] = type(e).__name__
            return None


def _payload(plan, depot, prices) -> bytes:
    d = depot.to_dict() if hasattr(depot, "to_dict") else depot
    return json.dumps({"plan": plan, "depot": d, "prices": prices}, default=float).encode()


def _parse(out: str) -> dict:
    for line in (out or "").splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise RuntimeError(f"no result in sandbox output: {(out or '')[-400:]}")


def _local(plan, depot, prices) -> dict:
    with tempfile.TemporaryDirectory() as td:
        Path(td, "inputs.json").write_bytes(_payload(plan, depot, prices))
        Path(td, "run.py").write_text(RUNNER.replace("sys.path.insert(0, '.')", f"sys.path.insert(0, {str(ROOT)!r})"))
        p = subprocess.run([sys.executable, "run.py"], cwd=td, capture_output=True, text=True, timeout=120)
        return _parse(p.stdout + p.stderr)


def simulate_in_sandbox(plan, depot, prices) -> dict:
    """Returns simulate() output plus {"where": "daytona"|"local", "sandbox_id", "elapsed_s"}."""
    t0 = time.time()
    try:
        import agentglow

        jobcm = agentglow.job(f"sim-{depot.date if hasattr(depot, 'date') else 'night'}", kind="daytona-sim")
    except Exception:  # noqa: BLE001
        jobcm = None
    try:
        if jobcm:
            jobcm.__enter__()
        if enabled():
            try:
                with _lock:
                    sb = _sandbox()
                    files = [(_payload(plan, depot, prices), "inputs.json"), (RUNNER.encode(), "run.py")]
                    for m in MODULES:
                        files.append(((ROOT / "grid" / m).read_bytes(), f"grid/{m}"))
                    from daytona import FileUpload

                    sb.fs.upload_files([FileUpload(source=s, destination=d) for s, d in files])
                    r = sb.process.exec("python3 run.py", timeout=120)
                    res = _parse(r.result)
                    _state["runs"] += 1
                    _state["error"] = None
                    return {**res, "where": "daytona", "sandbox_id": sb.id, "elapsed_s": round(time.time() - t0, 2)}
            except Exception as e:  # noqa: BLE001
                _state["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                log.warning("daytona run failed, falling back to local: %s", _state["error"])
                _state["sandbox"] = None if "not found" in str(e).lower() else _state["sandbox"]
        res = _local(plan, depot, prices)
        return {**res, "where": "local", "sandbox_id": None, "elapsed_s": round(time.time() - t0, 2)}
    finally:
        if jobcm:
            try:
                jobcm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def shutdown() -> None:
    sb, c = _state["sandbox"], _state["client"]
    _state["sandbox"] = None
    if sb is not None and c is not None:
        try:
            c.delete(sb)
            log.info("daytona sandbox %s deleted", sb.id)
        except Exception as e:  # noqa: BLE001
            log.warning("daytona delete failed: %s", type(e).__name__)


# ------------------------------------------------------------------ generic runner + parallel fan-out
ARB_MODULES = ["optimize.py", "battery.py", "bill.py", "depot.py"]
ARB_RUNNER = f"""
import json, sys
sys.path.insert(0, '.')
from grid import optimize as o
inp = json.load(open('inputs.json'))
def arb_day(d, da, rt, reserve_pct=10, cycles=1.0):
    b, p = o.arb_baseline(da), o.arb_optimal(da, reserve_pct, cycles)
    sb, sp = o.arb_simulate(b, rt), o.arb_simulate(p, rt)
    return {{"date": d, "baseline_profit": sb["profit_usd"], "optimal_profit": sp["profit_usd"],
            "optimal_profit_da": p["expected_profit_usd"], "cycles": sp["cycles"],
            "violations": len([x for x in sp["violations"] if "missing" not in x])}}
fn = arb_day if inp['fn'] == 'arb_day' else getattr(o, inp['fn'])
res = fn(*inp.get('args', []), **inp.get('kwargs', {{}}))
print('{MARK}' + json.dumps(res, default=float))
"""


def _files(inputs: dict) -> list:
    from daytona import FileUpload

    files = [FileUpload(source=json.dumps(inputs, default=float).encode(), destination="inputs.json"),
             FileUpload(source=ARB_RUNNER.encode(), destination="run.py"),
             FileUpload(source=b"", destination="grid/__init__.py")]
    for m in ARB_MODULES:
        p = ROOT / "grid" / m
        if p.exists():
            files.append(FileUpload(source=p.read_bytes(), destination=f"grid/{m}"))
    return files


def _local_call(inputs: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        Path(td, "inputs.json").write_text(json.dumps(inputs, default=float))
        Path(td, "run.py").write_text(ARB_RUNNER.replace("sys.path.insert(0, '.')", f"sys.path.insert(0, {str(ROOT)!r})"))
        p = subprocess.run([sys.executable, "run.py"], cwd=td, capture_output=True, text=True, timeout=120)
        return _parse(p.stdout + p.stderr)


def call(fn: str, *args, **kwargs) -> dict:
    """Run grid.optimize.<fn>(*args, **kwargs) in the shared verification sandbox; local fallback flagged."""
    t0 = time.time()
    inputs = {"fn": fn, "args": list(args), "kwargs": kwargs}
    try:
        import agentglow

        jobcm = agentglow.job(f"verify-{fn}", kind="daytona-sim")
    except Exception:  # noqa: BLE001
        jobcm = _nullcm()
    with jobcm:
        if enabled():
            try:
                with _lock:
                    sb = _sandbox()
                    sb.fs.upload_files(_files(inputs))
                    r = sb.process.exec("python3 run.py", timeout=120)
                    res = _parse(r.result)
                    _state["runs"] += 1
                    _state["error"] = None
                    return {"result": res, "where": "daytona", "sandbox_id": sb.id, "elapsed_s": round(time.time() - t0, 2)}
            except Exception as e:  # noqa: BLE001
                _state["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                log.warning("daytona call failed, local fallback: %s", _state["error"])
        return {"result": _local_call(inputs), "where": "local", "sandbox_id": None, "elapsed_s": round(time.time() - t0, 2)}


class _nullcm:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fan_out(jobs: list[tuple[str, str, list, dict]], conc: int = 8, on_progress=None) -> list[dict]:
    """jobs: [(label, fn, args, kwargs)]. One ephemeral sandbox per job, `conc` at a time (org cap 10 vCPU), retry on
    CPU-limit errors. on_progress(i_done, n, label, sandbox_id, where). Returns [{label, result, sandbox_id, where}]."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n = len(jobs)
    done = [0]
    lock = threading.Lock()
    client = None
    if enabled():
        try:
            from daytona import Daytona, DaytonaConfig

            client = Daytona(DaytonaConfig(api_key=env("DAYTONA_API_KEY")))
        except Exception as e:  # noqa: BLE001
            _state["error"] = type(e).__name__

    def one(job):
        label, fn, args, kwargs = job
        inputs = {"fn": fn, "args": args, "kwargs": kwargs}
        try:
            import agentglow

            jcm = agentglow.job(label, kind="daytona-sim")
        except Exception:  # noqa: BLE001
            jcm = _nullcm()
        with jcm:
            if client is not None:
                sb = None
                try:
                    from daytona import CreateSandboxFromSnapshotParams

                    for attempt in range(8):
                        try:
                            sb = client.create(CreateSandboxFromSnapshotParams(language="python", ephemeral=True,
                                                                               labels={"app": "grid", "job": label[:60]}))
                            break
                        except Exception as e:  # noqa: BLE001
                            if "limit" not in str(e).lower() or attempt == 7:
                                raise
                            time.sleep(1.5 + attempt)
                    sb.fs.upload_files(_files(inputs))
                    r = sb.process.exec("mkdir -p grid; python3 run.py", timeout=120)
                    out = {"label": label, "result": _parse(r.result), "sandbox_id": sb.id, "where": "daytona"}
                except Exception as e:  # noqa: BLE001
                    log.warning("fan-out %s failed in daytona (%s), local fallback", label, type(e).__name__)
                    out = {"label": label, "result": _local_call(inputs), "sandbox_id": None, "where": "local"}
                finally:
                    if sb is not None:
                        threading.Thread(target=lambda s=sb: _safe_delete(client, s), daemon=True).start()
            else:
                out = {"label": label, "result": _local_call(inputs), "sandbox_id": None, "where": "local"}
        with lock:
            done[0] += 1
            k = done[0]
        if on_progress:
            on_progress(k, n, label, out["sandbox_id"], out["where"])
        return out

    from opentelemetry import context as otel_ctx

    ctx = otel_ctx.get_current()

    def wrapped(job):
        tok = otel_ctx.attach(ctx)
        try:
            return one(job)
        finally:
            otel_ctx.detach(tok)

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(wrapped, j) for j in jobs]
        for f in as_completed(futs):
            r = f.result()
            results[r["label"]] = r
    return [results[j[0]] for j in jobs]


def _safe_delete(client, sb) -> None:
    try:
        client.delete(sb)
    except Exception:  # noqa: BLE001
        pass
