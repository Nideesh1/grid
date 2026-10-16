"""Grid battery model: a 4-hour lithium-ion storage plant in New York City (constants approximate public specs).

Typical NYC 4-hour storage projects: NYSERDA's Bulk Storage Incentive and Con Edison's storage programs fund
projects of this shape, e.g. East River Energy Storage (Astoria, 100 MW / 400 MWh), Ravenswood (316 MW / 2,528 MWh
planned, 8-hour), and the many 5-20 MW / 4-hour retail storage sites in the five boroughs. We model one 10 MW / 40 MWh
unit (4-hour duration).

- Round-trip efficiency 85% (AC-AC, lithium-ion + inverter + transformer, per NREL ATB / Lazard LCOS), split as
  charge efficiency sqrt(0.85) and discharge efficiency sqrt(0.85).
- SOC window 10%..90% (manufacturer warranty / augmentation practice); an operator reserve can raise the minimum.
- Start and end each day at 50% SOC so days are independent and the next day is not starved.
- At most 1 full cycle (40 MWh discharged from the cells) per day, a common warranty throughput limit.
- Degradation cost $8/MWh discharged (cell replacement cost spread over warranted throughput, NREL/Lazard range $5-15).
- Never charge and discharge in the same hour.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Battery:
    power_mw: float = 10.0
    energy_mwh: float = 40.0
    rte: float = 0.85
    soc_min_pct: float = 10.0
    soc_max_pct: float = 90.0
    start_soc_pct: float = 50.0
    end_soc_pct: float = 50.0
    max_cycles: float = 1.0
    degradation_usd_mwh: float = 8.0

    @property
    def eta(self) -> float:
        return self.rte ** 0.5

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT = Battery()
