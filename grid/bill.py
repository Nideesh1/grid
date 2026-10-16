"""Electricity bill model for the depot (approximate, documented).

- Energy: wholesale NYISO N.Y.C. zonal LBMP passed through, sum(kWh_t * LBMP_t / 1000).
- Delivery adder: $0.05/kWh flat. Approximates Con Edison delivery + supply adders for a large commercial
  service; real SC9 delivery is a mix of per-kWh and per-kW charges.
- Demand charge: $30/kW-month on the month's highest 15-min average kW. Approximates Con Edison SC9 Rate II
  demand charges (roughly $25-45/kW-month summed across time-of-use periods). Real tariffs differ by season/period.
- For a single night we pro-rate the demand charge as $30/kW / 30 nights, so one night "owns" 1/30 of the month.
"""

from __future__ import annotations

DELIVERY_USD_KWH = 0.05
DEMAND_USD_KW_MONTH = 30.0
NIGHTS_PER_MONTH = 30
DEMAND_USD_KW_NIGHT = DEMAND_USD_KW_MONTH / NIGHTS_PER_MONTH


def energy_cost(kwh: list[float], usd_mwh: list[float]) -> float:
    """Wholesale energy cost for aligned per-slot kWh and LBMP lists."""
    return float(sum(k * p / 1000.0 for k, p in zip(kwh, usd_mwh)))


def delivery_cost(kwh: list[float]) -> float:
    return float(sum(kwh) * DELIVERY_USD_KWH)


def demand_charge(peak_kw: float, nights: float = 1) -> float:
    """Demand charge pro-rated over `nights` (30 = a full month)."""
    return float(peak_kw * DEMAND_USD_KW_NIGHT * nights)


def bill(kwh: list[float], usd_mwh: list[float], peak_kw: float) -> dict:
    """One night's bill. total_usd includes the pro-rated demand charge; monthly_demand_usd is the full month."""
    e = energy_cost(kwh, usd_mwh)
    dlv = delivery_cost(kwh)
    dem = demand_charge(peak_kw)
    return {
        "kwh": round(float(sum(kwh)), 1),
        "energy_usd": round(e, 2),
        "delivery_usd": round(dlv, 2),
        "demand_night_usd": round(dem, 2),
        "monthly_demand_usd": round(peak_kw * DEMAND_USD_KW_MONTH, 2),
        "total_usd": round(e + dlv + dem, 2),
    }
