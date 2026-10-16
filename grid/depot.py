"""Depot model: a NYC battery-electric bus depot (constants approximate public specs).

- Fleet: 40 buses. MTA NYCT's battery-electric pilot/rollout buses (New Flyer Xcelsior CHARGE NG XE40 and
  Proterra ZX5) carry roughly 440-490 kWh (New Flyer XE40 "up to 525 kWh"; Proterra ZX5 450-675 kWh). We use 440 kWh.
- Chargers: 150 kW DC plug-in depot chargers, one per bus (MTA depot pilots at Michael J. Quill / Grand Avenue
  use 150 kW class plug-in chargers).
- Site power cap: 3,000 kW utility service limit (a single large feeder/transformer allotment).
- Schedule: pull-in around 19:00 at 20-40% SOC after a day of service, pull-out 05:00, must leave >= 90% SOC.
- Charging efficiency 0.93 grid-to-battery (DC charger + battery losses), applied to energy delivered to the pack.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Bus:
    id: int
    arrival_soc: float  # fraction 0..1
    arrival_hour: float = 19.0  # hours since midnight of the night's start date
    departure_hour: float = 29.0  # 05:00 next day = 24 + 5


@dataclass
class Depot:
    date: str
    buses: list[Bus] = field(default_factory=list)
    battery_kwh: float = 440.0
    charger_kw: float = 150.0
    site_cap_kw: float = 3000.0
    target_soc: float = 0.90
    efficiency: float = 0.93
    start_hour: int = 19
    end_hour: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Depot":
        d = dict(d)
        d["buses"] = [b if isinstance(b, Bus) else Bus(**b) for b in d.get("buses", [])]
        return Depot(**d)

    def energy_needed_kwh(self) -> float:
        """Battery-side kWh needed to bring every bus to target."""
        return sum(max(0.0, self.target_soc - b.arrival_soc) * self.battery_kwh for b in self.buses)


def make_depot(d=None, n_buses: int = 40, **overrides) -> Depot:
    """Depot for the night starting on date d; arrival SOCs are seeded by the date so a night is reproducible."""
    d = d or date.today()
    ds = d if isinstance(d, str) else d.isoformat()
    rng = random.Random(f"grid-depot-{ds}")
    buses = [Bus(id=i + 1, arrival_soc=round(rng.uniform(0.20, 0.40), 3)) for i in range(n_buses)]
    return Depot(date=ds, buses=buses, **overrides)


def apply_constraint(depot: Depot, bus_id: int, departure_hour: float | None = None, arrival_hour: float | None = None):
    """Per-bus override, e.g. 'bus 12 must leave at 04:00' -> departure_hour=28."""
    for b in depot.buses:
        if b.id == bus_id:
            if departure_hour is not None:
                b.departure_hour = departure_hour
            if arrival_hour is not None:
                b.arrival_hour = arrival_hour
    return depot
