"""Smoke test: tonight on day-ahead prices, last night on real-time prices. Usage: uv run scripts/smoke_optimize.py [YYYY-MM-DD]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import date, timedelta

from grid.depot import make_depot
from grid.optimize import run_night
from grid.prices import night


def show(label: str, d: date, kind: str) -> None:
    rows = night(d, kind)
    if not rows:
        print(f"{label}: no {kind} prices for {d}")
        return
    dep = make_depot(d)
    r = run_night(dep, rows)
    b, o, s = r["baseline"], r["optimal"], r["savings"]
    proxy = " (tomorrow DA missing, proxy)" if any(p.get("proxy") for p in rows) else ""
    print(f"== {label}: night of {d} on {kind.upper()} N.Y.C. prices, {len(rows)} rows{proxy}")
    print(f"   price range ${min(p['usd_mwh'] for p in rows):.2f}-${max(p['usd_mwh'] for p in rows):.2f}/MWh, "
          f"need {dep.energy_needed_kwh():,.0f} kWh to pack, LP solve {r['optimal_plan']['solve_s']}s")
    for name, x in (("baseline", b), ("optimal ", o)):
        print(f"   {name}: ${x['cost_usd']:,.2f} (energy ${x['energy_usd']:,.2f} + delivery ${x['delivery_usd']:,.2f}"
              f" + demand/night ${x['demand_night_usd']:,.2f}) peak {x['peak_kw']:,.0f} kW,"
              f" avg ${x['avg_usd_mwh']}/MWh, min dep SOC {x['min_soc_at_departure']:.1%}, violations {len(x['violations'])}")
    print(f"   savings tonight ${s['tonight_usd']:,.2f} ({s['pct']}%), monthly ${s['monthly_usd']:,.2f}"
          f" (energy ${s['energy_tonight_usd']:,.2f}/night, demand ${s['demand_monthly_usd']:,.2f}/mo)")
    print("   optimal schedule:", ", ".join(f"{h['hour']} {h['kw']:.0f}" for h in r["optimal_plan"]["schedule"]))


if __name__ == "__main__":
    today = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    show("tonight", today, "da")
    show("last night", today - timedelta(days=1), "rt")
