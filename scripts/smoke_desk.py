"""Smoke the trading desk: tomorrow plain, a storm reserve note (veto -> replan), and an over-tight 60% floor."""
import asyncio
import sys
import time

sys.path.insert(0, ".")
import grid.config  # noqa: F401,E402
from grid.desk import stream_desk  # noqa: E402


async def one(note):
    t0 = time.time()
    res = {}
    async for ev in stream_desk(None, note):
        if ev["kind"] == "result":
            res = ev
        else:
            print(f"  [{time.time() - t0:5.1f}s] {ev['text'][:150]}")
    sim = res.get("simulation") or {}
    print(f"== note={note!r} verdict={res.get('verdict')} {res.get('elapsed_s')}s rounds={len(res.get('verdicts', []))} "
          f"profit={sim.get('profit_usd')} min_soc={sim.get('min_soc')} violations={sim.get('violations')} where={sim.get('where')}")
    print(f"   message: {res.get('message')}")
    return res


async def building():
    """--building: 0.5 MW / 2 MWh building battery in Long Island selling above $60, then restore the old profile."""
    from grid import profile

    old = profile.get_profile()
    try:
        profile.save_profile({"name": "building-battery", "power_mw": 0.5, "energy_mwh": 2, "zone": "LONGIL",
                              "sell_above": 60}, source="smoke", replace=True)
        r = await one("storm tomorrow, keep 30% reserve")
        profile.save_profile({"sell_above": 500}, source="smoke")
        h = await one(None)
        print("SMOKE", "OK" if r.get("message") and h.get("verdict") == "approve-hold" else "FAIL")
    finally:
        profile.save_profile(old, source="smoke-reset", replace=True)
        print("profile restored:", profile.get_profile()["name"], profile.get_profile()["zone"])


async def main():
    if "--building" in sys.argv:
        return await building()
    a = await one(None)
    b = await one("storm tomorrow, keep 30% reserve")
    c = await one("never let the battery go below 60%")
    ok = all(r.get("message") for r in (a, b, c)) and (b["simulation"]["min_soc"] >= 29.95)
    print("SMOKE", "OK" if ok else "FAIL")


asyncio.run(main())
