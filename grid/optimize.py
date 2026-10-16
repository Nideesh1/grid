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


# =====================================================================================================
# Grid battery arbitrage (PIVOT). Self-contained like the depot code: plans embed the battery params, so
# arb_simulate(plan, prices) needs nothing but this file in a Daytona sandbox.
# Plan shape: {kind, date, battery:{...}, constraints:{...}, hours:[{hour, ts, price, charge_mw, discharge_mw,
#              soc_pct}], status, expected_profit_usd}
# MW over one hour == MWh. charge_mw / discharge_mw are grid-side (AC) power.
# =====================================================================================================

_ARB_DEFAULT = {"power_mw": 10.0, "energy_mwh": 40.0, "rte": 0.85, "soc_min_pct": 10.0, "soc_max_pct": 90.0,
                "start_soc_pct": 50.0, "end_soc_pct": 50.0, "max_cycles": 1.0, "degradation_usd_mwh": 8.0}


def _battery_dict(battery=None) -> dict:
    if battery is None:
        try:
            from grid.battery import DEFAULT
            return DEFAULT.to_dict()
        except ImportError:  # pragma: no cover
            return dict(_ARB_DEFAULT)
    return battery if isinstance(battery, dict) else battery.to_dict()


def hourly_prices(prices: list[dict]) -> list[dict]:
    """Average any-resolution [{ts, usd_mwh}] into hour-beginning rows [{ts, hour, usd_mwh}], sorted.
    Real-time rows are interval-ending (00:05 belongs to hour 00); pass them as-is from grid.prices.real_time()."""
    buckets: dict[datetime, list[float]] = {}
    stamps = [datetime.fromisoformat(p["ts"]) for p in prices]
    sub_hourly = any(t.minute or t.second for t in stamps)  # real-time 5-min, interval-ending
    for p, ts in zip(prices, stamps):
        if sub_hourly:
            ts -= timedelta(minutes=5)  # 01:00 ends the 00:55 interval -> hour 00
        h = ts.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(h, []).append(float(p["usd_mwh"]))
    return [{"ts": h.isoformat(), "hour": h.hour, "usd_mwh": round(float(np.mean(v)), 2)}
            for h, v in sorted(buckets.items())]


def _arb_plan(kind, hp, bat, cons, ch, dis, status) -> dict:
    eta, E = bat["rte"] ** 0.5, bat["energy_mwh"]
    soc = bat["start_soc_pct"] / 100 * E
    hours = []
    for i, p in enumerate(hp):
        soc += ch[i] * eta - dis[i] / eta
        hours.append({"hour": p["hour"], "ts": p["ts"], "price": p["usd_mwh"],
                      "charge_mw": round(float(ch[i]), 3), "discharge_mw": round(float(dis[i]), 3),
                      "soc_pct": round(100 * soc / E, 2)})
    exp = sum(h["discharge_mw"] * h["price"] - h["charge_mw"] * h["price"]
              - h["discharge_mw"] * bat["degradation_usd_mwh"] for h in hours)
    return {"kind": kind, "date": hp[0]["ts"][:10] if hp else None, "battery": bat, "constraints": cons,
            "hours": hours, "status": status, "expected_profit_usd": round(exp, 2)}


def arb_baseline(prices: list[dict], battery=None) -> dict:
    """Naive clock rule: charge at full power 01:00-05:00, discharge at full power 17:00-21:00,
    clipped to the SOC window and so the day ends at the end SOC."""
    bat = _battery_dict(battery)
    hp = hourly_prices(prices)
    eta, E, P = bat["rte"] ** 0.5, bat["energy_mwh"], bat["power_mw"]
    lo, hi = bat["soc_min_pct"] / 100 * E, bat["soc_max_pct"] / 100 * E
    end = bat["end_soc_pct"] / 100 * E
    soc = bat["start_soc_pct"] / 100 * E
    ch, dis = np.zeros(len(hp)), np.zeros(len(hp))
    for i, p in enumerate(hp):
        if 1 <= p["hour"] < 5:
            ch[i] = min(P, max(0.0, hi - soc) / eta)
        elif 17 <= p["hour"] < 21:
            dis[i] = min(P, max(0.0, soc - max(lo, end)) * eta)
        soc += ch[i] * eta - dis[i] / eta
    return _arb_plan("baseline", hp, bat, {}, ch, dis, "ok")


def arb_optimal(prices: list[dict], reserve_pct: float = 10, cycles: float = 1.0, battery=None,
                no_discharge_before: int | None = None, no_charge_after: int | None = None) -> dict:
    """MILP (HiGHS) over hourly prices: max sum(d*p) - sum(c*p) - deg*d.
    s.t. soc_t = soc_{t-1} + c_t*eta - d_t/eta; max(soc_min, reserve) <= soc_t <= soc_max; soc_end = end SOC;
    sum(d_t/eta) <= cycles * energy_mwh; c_t <= P*u_t, d_t <= P*(1-u_t), u_t binary (no simultaneous charge+discharge).
    reserve_pct raises the floor (capped at the start SOC so the day stays feasible).
    no_discharge_before=18 means "don't sell before 6pm"; no_charge_after blocks buying from that hour on."""
    from scipy.optimize import Bounds, LinearConstraint, milp

    bat = _battery_dict(battery)
    hp = hourly_prices(prices)
    cons = {"reserve_pct": reserve_pct, "cycles": cycles, "no_discharge_before": no_discharge_before,
            "no_charge_after": no_charge_after}
    T = len(hp)
    eta, E, P = bat["rte"] ** 0.5, bat["energy_mwh"], bat["power_mw"]
    floor_pct = min(max(bat["soc_min_pct"], reserve_pct), bat["start_soc_pct"], bat["end_soc_pct"])
    lo, hi = floor_pct / 100 * E, bat["soc_max_pct"] / 100 * E
    s0, s_end = bat["start_soc_pct"] / 100 * E, bat["end_soc_pct"] / 100 * E
    price = np.array([p["usd_mwh"] for p in hp])
    # x = [c_0..c_T-1, d_0..d_T-1, u_0..u_T-1]
    c = np.concatenate([price, -(price - bat["degradation_usd_mwh"]), np.zeros(T)])  # minimize negative profit
    ub_c = np.full(T, P)
    ub_d = np.full(T, P)
    for i, p in enumerate(hp):
        if no_discharge_before is not None and p["hour"] < no_discharge_before:
            ub_d[i] = 0.0
        if no_charge_after is not None and p["hour"] >= no_charge_after:
            ub_c[i] = 0.0
    bounds = Bounds(np.concatenate([np.zeros(2 * T), np.zeros(T)]), np.concatenate([ub_c, ub_d, np.ones(T)]))
    L = np.tril(np.ones((T, T)))  # cumulative sums -> soc_t - s0
    Z = np.zeros((T, T))
    I = np.eye(T)
    A_soc = np.hstack([L * eta, -L / eta, Z])
    A_end = np.concatenate([np.ones(T) * eta, -np.ones(T) / eta, np.zeros(T)])[None, :]
    A_cyc = np.concatenate([np.zeros(T), np.ones(T) / eta, np.zeros(T)])[None, :]
    A_uc = np.hstack([I, Z, -P * I])  # c - P u <= 0
    A_ud = np.hstack([Z, I, P * I])   # d + P u <= P
    constraints = [
        LinearConstraint(A_soc, lo - s0, hi - s0),
        LinearConstraint(A_end, s_end - s0, s_end - s0),
        LinearConstraint(A_cyc, 0, cycles * E),
        LinearConstraint(A_uc, -np.inf, 0),
        LinearConstraint(A_ud, -np.inf, P),
    ]
    integrality = np.concatenate([np.zeros(2 * T), np.ones(T)])
    res = milp(c, constraints=constraints, bounds=bounds, integrality=integrality)
    if res.x is None:
        return _arb_plan("optimal", hp, bat, cons, np.zeros(T), np.zeros(T), f"infeasible: {res.message}")
    x = np.where(np.abs(res.x) < 1e-6, 0.0, res.x)
    return _arb_plan("optimal", hp, bat, cons, x[:T], x[T:2 * T], "ok")


def arb_simulate(plan: dict, prices: list[dict] | None = None) -> dict:
    """Pure replay of an arbitrage plan. Settles on `prices` (any resolution, e.g. real-time 5-min, averaged
    to hours) or, if None, on the plan's own day-ahead prices. Re-derives SOC and checks every limit."""
    bat = plan["battery"]
    cons = plan.get("constraints") or {}
    eta, E, P = bat["rte"] ** 0.5, bat["energy_mwh"], bat["power_mw"]
    floor_pct = min(max(bat["soc_min_pct"], cons.get("reserve_pct") or 0), bat["start_soc_pct"], bat["end_soc_pct"])
    cyc_lim = cons.get("cycles", bat["max_cycles"]) if cons else bat["max_cycles"]
    settle = {h["ts"]: h["usd_mwh"] for h in hourly_prices(prices)} if prices else {}
    v: list[str] = []
    soc = bat["start_soc_pct"] / 100 * E
    socs, rev, cost, dis_cells, missing = [], 0.0, 0.0, 0.0, 0
    for h in plan["hours"]:
        c, d = h["charge_mw"], h["discharge_mw"]
        if c < -1e-6 or d < -1e-6:
            v.append(f"negative power at {h['hour']:02d}:00")
        if c > P + 1e-6 or d > P + 1e-6:
            v.append(f"power above {P} MW at {h['hour']:02d}:00")
        if c > 1e-6 and d > 1e-6:
            v.append(f"simultaneous charge and discharge at {h['hour']:02d}:00")
        nd = cons.get("no_discharge_before")
        if nd is not None and h["hour"] < nd and d > 1e-6:
            v.append(f"discharge before {nd:02d}:00 at {h['hour']:02d}:00")
        p = settle.get(h["ts"], h["price"]) if prices else h["price"]
        if prices and h["ts"] not in settle:
            missing += 1
        soc += c * eta - d / eta
        socs.append(100 * soc / E)
        rev += d * p
        cost += c * p
        dis_cells += d / eta
    for i, s in enumerate(socs):
        if s < floor_pct - 0.05 or s > bat["soc_max_pct"] + 0.05:
            v.append(f"SOC {s:.1f}% outside {floor_pct:.0f}-{bat['soc_max_pct']:.0f}% at {plan['hours'][i]['hour']:02d}:00")
    end = socs[-1] if socs else bat["start_soc_pct"]
    if abs(end - bat["end_soc_pct"]) > 0.05:
        v.append(f"end SOC {end:.1f}% != {bat['end_soc_pct']:.0f}%")
    cycles = dis_cells / E
    if cycles > cyc_lim + 1e-3:
        v.append(f"{cycles:.2f} cycles > limit {cyc_lim}")
    if missing:
        v.append(f"{missing} hours missing settlement prices (used plan price)")
    deg = sum(h["discharge_mw"] for h in plan["hours"]) * bat["degradation_usd_mwh"]
    return {
        "kind": plan.get("kind"),
        "settled_on": "plan" if not prices else "given prices",
        "revenue_usd": round(rev, 2),
        "cost_usd": round(cost, 2),
        "degradation_usd": round(deg, 2),
        "profit_usd": round(rev - cost - deg, 2),
        "mwh_charged": round(sum(h["charge_mw"] for h in plan["hours"]), 2),
        "mwh_discharged": round(sum(h["discharge_mw"] for h in plan["hours"]), 2),
        "cycles": round(cycles, 3),
        "min_soc": round(min(socs), 2) if socs else None,
        "max_soc": round(max(socs), 2) if socs else None,
        "end_soc": round(end, 2),
        "violations": v,
    }


def arb_backtest(days: int = 30, end=None, reserve_pct: float = 10, cycles: float = 1.0) -> dict:
    """Last `days` complete days (ending yesterday): plan on day-ahead, settle on real-time hourly averages."""
    from datetime import date as _date

    from grid.prices import day_ahead, real_time

    end_d = _date.fromisoformat(str(end)) if end else _date.today() - timedelta(days=1)
    out = []
    tb = to = 0.0
    for i in range(days):
        d = end_d - timedelta(days=days - 1 - i)
        da, rt = day_ahead(d), real_time(d)
        if len(da) < 23 or len(rt) < 250:
            continue
        b, o = arb_baseline(da), arb_optimal(da, reserve_pct, cycles)
        sb, so = arb_simulate(b, rt), arb_simulate(o, rt)
        tb += sb["profit_usd"]
        to += so["profit_usd"]
        out.append({"date": d.isoformat(), "baseline_profit": sb["profit_usd"], "optimal_profit": so["profit_usd"],
                    "optimal_profit_da": o["expected_profit_usd"], "cycles": so["cycles"],
                    "violations": len([x for x in so["violations"] if "missing" not in x])})
    return {"days": out, "totals": {"days": len(out), "baseline_profit": round(tb, 2),
                                    "optimal_profit": round(to, 2), "uplift": round(to - tb, 2)}}
