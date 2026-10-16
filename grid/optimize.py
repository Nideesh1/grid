"""Charging optimizer for the depot: baseline vs LP-optimal plans, a pure simulator, and savings math.

This module is self-contained (numpy + scipy only at import time) so it can be uploaded alone to a Daytona
sandbox and run `simulate(plan, depot, prices)` there. `depot` may be a grid.depot.Depot or its dict form;
`prices` is [{ts, usd_mwh}] (any resolution, interval-beginning timestamps), averaged into 15-min slots.

Plan shape (JSON-serializable):
  {kind, slot_minutes, slots:[iso], bus_ids:[int], kw:[[kW per slot] per bus], site_kw:[...],
   schedule:[{hour, kw}], cap_kw, status}
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import numpy as np

try:  # canonical constants live in grid/bill.py; fall back to copies when running standalone in a sandbox
    from grid.bill import DELIVERY_USD_KWH, DEMAND_USD_KW_MONTH, DEMAND_USD_KW_NIGHT, NIGHTS_PER_MONTH
except ImportError:  # pragma: no cover
    DELIVERY_USD_KWH, DEMAND_USD_KW_MONTH, NIGHTS_PER_MONTH = 0.05, 30.0, 30
    DEMAND_USD_KW_NIGHT = DEMAND_USD_KW_MONTH / NIGHTS_PER_MONTH

SLOT_MIN = 15
DT_H = SLOT_MIN / 60.0
TOL = 1e-3


# ---------- helpers ----------

def _depot_dict(depot) -> dict:
    return depot if isinstance(depot, dict) else depot.to_dict()


def _slots(dep: dict) -> list[datetime]:
    d0 = datetime.fromisoformat(dep["date"])
    t0 = d0 + timedelta(hours=dep.get("start_hour", 19))
    t1 = d0 + timedelta(days=1, hours=dep.get("end_hour", 5))
    n = int((t1 - t0).total_seconds() // (SLOT_MIN * 60))
    return [t0 + timedelta(minutes=SLOT_MIN * i) for i in range(n)]


def slot_prices(prices: list[dict], slots: list[datetime]) -> np.ndarray:
    """Average price rows into slots; slots with no row inside (hourly data) take the latest earlier price."""
    rows = sorted(((datetime.fromisoformat(p["ts"]), float(p["usd_mwh"])) for p in prices), key=lambda r: r[0])
    if not rows:
        raise ValueError("no prices")
    out = np.empty(len(slots))
    j, last = 0, rows[0][1]
    for i, s in enumerate(slots):
        e = s + timedelta(minutes=SLOT_MIN)
        while j < len(rows) and rows[j][0] < s:
            last = rows[j][1]
            j += 1
        k, vals = j, []
        while k < len(rows) and rows[k][0] < e:
            vals.append(rows[k][1])
            k += 1
        out[i] = float(np.mean(vals)) if vals else last
    return out


def _windows(dep: dict, slots: list[datetime]) -> np.ndarray:
    """Boolean [bus, slot]: bus is plugged in for the whole slot."""
    d0 = datetime.fromisoformat(dep["date"])
    m = np.zeros((len(dep["buses"]), len(slots)), dtype=bool)
    for b, bus in enumerate(dep["buses"]):
        a = d0 + timedelta(hours=bus["arrival_hour"])
        z = d0 + timedelta(hours=bus["departure_hour"])
        for i, s in enumerate(slots):
            m[b, i] = s >= a and s + timedelta(minutes=SLOT_MIN) <= z
    return m


def _schedule(slots: list[datetime], site_kw: np.ndarray) -> list[dict]:
    hours: dict[str, list[float]] = {}
    for s, kw in zip(slots, site_kw):
        hours.setdefault(s.strftime("%H:00"), []).append(float(kw))
    return [{"hour": h, "kw": round(float(np.mean(v)), 1)} for h, v in hours.items()]


def _plan(kind, dep, slots, kw: np.ndarray, cap_kw, status, solve_s=None) -> dict:
    kw = np.clip(kw, 0, None)
    site = kw.sum(axis=0)
    return {
        "kind": kind,
        "date": dep["date"],
        "slot_minutes": SLOT_MIN,
        "slots": [s.isoformat() for s in slots],
        "bus_ids": [b["id"] for b in dep["buses"]],
        "kw": np.round(kw, 3).tolist(),
        "site_kw": np.round(site, 1).tolist(),
        "schedule": _schedule(slots, site),
        "cap_kw": cap_kw,
        "status": status,
        "solve_s": solve_s,
    }


# ---------- plans ----------

def baseline(depot, prices=None, cap_kw: float | None = None) -> dict:
    """'Plug in at 7pm': every bus draws full charger power from arrival until it reaches target SOC.

    The site's load-management throttles all chargers proportionally if the total would exceed the site cap
    (a 6 MW unmanaged draw is not physically allowed on a 3 MW service). `prices` is unused (signature parity).
    """
    dep = _depot_dict(depot)
    cap = cap_kw or dep["site_cap_kw"]
    slots = _slots(dep)
    win = _windows(dep, slots)
    eff, cap_kwh = dep["efficiency"], dep["battery_kwh"]
    need = np.array([max(0.0, dep["target_soc"] - b["arrival_soc"]) * cap_kwh for b in dep["buses"]])  # battery kWh
    kw = np.zeros((len(dep["buses"]), len(slots)))
    for t in range(len(slots)):
        want = np.where(win[:, t], np.minimum(dep["charger_kw"], need / (DT_H * eff)), 0.0)
        tot = want.sum()
        if tot > cap:
            want *= cap / tot
        kw[:, t] = want
        need = np.maximum(0.0, need - want * DT_H * eff)
    return _plan("baseline", dep, slots, kw, cap, "ok")


def optimal(depot, prices, cap_kw: float | None = None) -> dict:
    """LP (HiGHS via scipy.linprog): min sum_t kWh_t*(LBMP_t/1000 + delivery) + peak_kW * demand$/kW/night.

    s.t. 0 <= p[b,t] <= charger_kW while plugged in (else 0); sum_b p[b,t] <= peak <= cap;
         target <= SOC at departure <= 100%.
    """
    from scipy.optimize import linprog
    from scipy.sparse import coo_matrix

    t_start = time.perf_counter()
    dep = _depot_dict(depot)
    cap = cap_kw or dep["site_cap_kw"]
    slots = _slots(dep)
    price = slot_prices(prices, slots)
    win = _windows(dep, slots)
    B, T = win.shape
    n = B * T + 1  # p[b,t] flattened b*T+t, then peak
    eff, cap_kwh = dep["efficiency"], dep["battery_kwh"]

    c = np.empty(n)
    c[:-1] = np.tile(DT_H * (price / 1000.0 + DELIVERY_USD_KWH), B)
    c[-1] = DEMAND_USD_KW_NIGHT
    bounds = [(0.0, dep["charger_kw"] if win[b, t] else 0.0) for b in range(B) for t in range(T)]
    bounds.append((0.0, cap))

    rows, cols, vals, rhs = [], [], [], []
    # site: sum_b p[b,t] - peak <= 0
    for t in range(T):
        rows += [t] * (B + 1)
        cols += [b * T + t for b in range(B)] + [n - 1]
        vals += [1.0] * B + [-1.0]
        rhs.append(0.0)
    # per bus energy: -sum p*dt*eff <= -(target-soc0)*kWh ; sum p*dt*eff <= (1-soc0)*kWh
    r = T
    for b, bus in enumerate(dep["buses"]):
        idx = [b * T + t for t in range(T)]
        rows += [r] * T + [r + 1] * T
        cols += idx + idx
        vals += [-DT_H * eff] * T + [DT_H * eff] * T
        rhs += [-max(0.0, dep["target_soc"] - bus["arrival_soc"]) * cap_kwh, (1.0 - bus["arrival_soc"]) * cap_kwh]
        r += 2
    A = coo_matrix((vals, (rows, cols)), shape=(r, n)).tocsr()
    res = linprog(c, A_ub=A, b_ub=np.array(rhs), bounds=bounds, method="highs")
    solve_s = round(time.perf_counter() - t_start, 3)
    if res.status != 0:
        return _plan("optimal", dep, slots, np.zeros((B, T)), cap, f"infeasible: {res.message}", solve_s)
    kw = res.x[:-1].reshape(B, T)
    return _plan("optimal", dep, slots, kw, cap, "ok", solve_s)


# ---------- simulation + savings ----------

def simulate(plan: dict, depot, prices) -> dict:
    """Replay a plan against the depot and prices. Pure: recomputes SOC, cost and peak, lists every violation."""
    dep = _depot_dict(depot)
    slots = [datetime.fromisoformat(s) for s in plan["slots"]]
    kw = np.asarray(plan["kw"], dtype=float)
    price = slot_prices(prices, slots)
    win = _windows(dep, slots)
    cap = plan.get("cap_kw") or dep["site_cap_kw"]
    eff, cap_kwh = dep["efficiency"], dep["battery_kwh"]
    violations: list[str] = []

    if kw.shape != win.shape:
        return {"cost_usd": None, "peak_kw": None, "min_soc_at_departure": None,
                "violations": [f"plan shape {kw.shape} != depot/slots {win.shape}"]}
    if (kw < -TOL).any():
        violations.append("negative charging power")
    over = np.argwhere(kw > dep["charger_kw"] + 0.01)
    for b, t in over[:5]:
        violations.append(f"bus {dep['buses'][b]['id']} exceeds charger limit at {slots[t]:%H:%M}")
    outside = np.argwhere((kw > TOL) & ~win)
    for b, t in outside[:5]:
        violations.append(f"bus {dep['buses'][b]['id']} charging while not plugged in at {slots[t]:%H:%M}")
    site = kw.sum(axis=0)
    for t in np.where(site > cap + 0.5)[0][:5]:
        violations.append(f"site load {site[t]:.0f} kW exceeds cap {cap:.0f} kW at {slots[t]:%H:%M}")

    soc0 = np.array([b["arrival_soc"] for b in dep["buses"]])
    traj = soc0[:, None] + np.cumsum(kw * DT_H * eff, axis=1) / cap_kwh
    dep_soc = traj[:, -1] if traj.size else soc0
    for b, s in enumerate(dep_soc):
        if s < dep["target_soc"] - 1e-4:
            violations.append(f"bus {dep['buses'][b]['id']} departs at {s:.1%} < target {dep['target_soc']:.0%}")
        if traj[b].max() > 1.0 + 1e-4:
            violations.append(f"bus {dep['buses'][b]['id']} overcharged to {traj[b].max():.1%}")

    kwh = site * DT_H
    energy = float((kwh * price / 1000.0).sum())
    delivery = float(kwh.sum() * DELIVERY_USD_KWH)
    peak = float(site.max()) if site.size else 0.0
    demand_night = peak * DEMAND_USD_KW_NIGHT
    return {
        "kind": plan.get("kind"),
        "cost_usd": round(energy + delivery + demand_night, 2),
        "energy_usd": round(energy, 2),
        "delivery_usd": round(delivery, 2),
        "demand_night_usd": round(demand_night, 2),
        "monthly_demand_usd": round(peak * DEMAND_USD_KW_MONTH, 2),
        "kwh": round(float(kwh.sum()), 1),
        "avg_usd_mwh": round(float((kwh * price).sum() / max(kwh.sum(), 1e-9)), 2),
        "peak_kw": round(peak, 1),
        "min_soc_at_departure": round(float(dep_soc.min()), 4),
        "violations": violations,
    }


def savings(baseline: dict, plan: dict) -> dict:
    """Savings from two simulate() results. tonight = night cost delta (incl. pro-rated demand charge);
    monthly = energy+delivery delta x 30 nights + full-month demand-charge delta on the peak kW."""
    var_b = baseline["energy_usd"] + baseline["delivery_usd"]
    var_p = plan["energy_usd"] + plan["delivery_usd"]
    tonight = baseline["cost_usd"] - plan["cost_usd"]
    demand_delta = (baseline["peak_kw"] - plan["peak_kw"]) * DEMAND_USD_KW_MONTH
    monthly = (var_b - var_p) * NIGHTS_PER_MONTH + demand_delta
    return {
        "tonight_usd": round(tonight, 2),
        "monthly_usd": round(monthly, 2),
        "energy_tonight_usd": round(var_b - var_p, 2),
        "demand_monthly_usd": round(demand_delta, 2),
        "pct": round(100.0 * tonight / baseline["cost_usd"], 1) if baseline["cost_usd"] else 0.0,
    }


def run_night(depot, plan_prices, settle_prices=None, cap_kw: float | None = None) -> dict:
    """Baseline vs optimal for one night. Plan on `plan_prices`, settle both on `settle_prices` (default same)."""
    settle = settle_prices or plan_prices
    b = baseline(depot, plan_prices, cap_kw)
    o = optimal(depot, plan_prices, cap_kw)
    sb, so = simulate(b, depot, settle), simulate(o, depot, settle)
    return {"baseline_plan": b, "optimal_plan": o, "baseline": sb, "optimal": so, "savings": savings(sb, so)}


def backtest(days: int = 30, mode: str = "rt", cap_kw: float | None = None, end=None) -> dict:
    """Baseline vs optimal over the last `days` nights of NYISO N.Y.C. prices.

    mode="rt": perfect foresight, plan and settle on real-time prices.
    mode="da": plan on that night's day-ahead prices, settle on real-time (what a real dispatcher could do).
    """
    from grid.depot import make_depot
    from grid.prices import history, night

    rt = history(days, "rt", end=end)
    out, tot_b, tot_o = [], 0.0, 0.0
    for d, rt_rows in rt.items():
        dep = make_depot(d)
        plan_rows = rt_rows if mode == "rt" else (night(d, "da") or rt_rows)
        r = run_night(dep, plan_rows, rt_rows, cap_kw)
        bu, op = r["baseline"]["cost_usd"], r["optimal"]["cost_usd"]
        tot_b, tot_o = tot_b + bu, tot_o + op
        out.append({
            "date": d, "baseline_usd": bu, "optimal_usd": op, "saved_usd": round(bu - op, 2),
            "baseline_peak_kw": r["baseline"]["peak_kw"], "optimal_peak_kw": r["optimal"]["peak_kw"],
            "violations": len(r["optimal"]["violations"]),
        })
    return {
        "mode": mode,
        "days": out,
        "totals": {
            "nights": len(out),
            "baseline_usd": round(tot_b, 2),
            "optimal_usd": round(tot_o, 2),
            "saved_usd": round(tot_b - tot_o, 2),
            "pct": round(100 * (tot_b - tot_o) / tot_b, 1) if tot_b else 0.0,
        },
    }
