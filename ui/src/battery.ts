// The user's battery profile. GET/POST /api/battery (backend in progress); falls back to a local copy.
/* eslint-disable @typescript-eslint/no-explicit-any */
import { API, MOCK } from "./api";

export type Battery = {
  name?: string;
  power_mw: number;
  energy_mwh: number;
  stored_mwh: number;
  zone: string;
  reserve_pct: number;
  max_cycles: number;
  efficiency_pct: number;
  sell_above: number | null;
  buy_below: number | null;
};
export type BatteryChange = { field: string; old: unknown; new: unknown; at?: string; source?: string };

export const PRESETS: Battery[] = [
  { name: "Grid battery 10 MW / 40 MWh", power_mw: 10, energy_mwh: 40, stored_mwh: 20, zone: "N.Y.C.", reserve_pct: 10, max_cycles: 1, efficiency_pct: 85, sell_above: null, buy_below: null },
  { name: "Building 0.5 MW / 2 MWh", power_mw: 0.5, energy_mwh: 2, stored_mwh: 1, zone: "N.Y.C.", reserve_pct: 20, max_cycles: 1, efficiency_pct: 88, sell_above: null, buy_below: null },
  { name: "Warehouse 2 MW / 8 MWh", power_mw: 2, energy_mwh: 8, stored_mwh: 4, zone: "LONGIL", reserve_pct: 10, max_cycles: 1, efficiency_pct: 86, sell_above: null, buy_below: null },
];
export const DEFAULT = PRESETS[0];

const LS = "grid.battery";
const LSH = "grid.battery.history";
const n = (v: any, d: number) => (typeof v === "number" && isFinite(v) ? v : typeof v === "string" && v !== "" && isFinite(+v) ? +v : d);
const nn = (v: any) => (v === null || v === undefined || v === "" ? null : isFinite(+v) ? +v : null);

export function normBattery(j: any): Battery {
  const b = j?.battery ?? j?.profile ?? j ?? {};
  const energy = n(b.energy_mwh ?? b.capacity_mwh, DEFAULT.energy_mwh);
  let stored = n(b.stored_mwh, NaN);
  if (!isFinite(stored)) stored = (n(b.soc_pct ?? b.stored_pct, 50) / 100) * energy;
  return {
    name: b.name,
    power_mw: n(b.power_mw, DEFAULT.power_mw),
    energy_mwh: energy,
    stored_mwh: stored,
    zone: b.zone ?? DEFAULT.zone,
    reserve_pct: n(b.reserve_pct, DEFAULT.reserve_pct),
    max_cycles: n(b.max_cycles ?? b.cycles, DEFAULT.max_cycles),
    efficiency_pct: n(b.efficiency_pct ?? (b.efficiency !== undefined ? b.efficiency * (b.efficiency <= 1 ? 100 : 1) : undefined), DEFAULT.efficiency_pct),
    sell_above: nn(b.sell_above ?? b.min_sell_price),
    buy_below: nn(b.buy_below ?? b.max_buy_price),
  };
}

function readLocal<T>(k: string, d: T): T {
  try {
    const s = localStorage.getItem(k);
    return s ? (JSON.parse(s) as T) : d;
  } catch {
    return d;
  }
}
function writeLocal(k: string, v: unknown) {
  try {
    localStorage.setItem(k, JSON.stringify(v));
  } catch {
    /* ignore */
  }
}

export function diff(a: Battery, b: Battery, source = "you"): BatteryChange[] {
  const at = new Date().toISOString();
  return (Object.keys(b) as (keyof Battery)[]).filter((k) => k !== "name" && a[k] !== b[k]).map((k) => ({ field: k, old: a[k], new: b[k], at, source }));
}

/** History rows from whatever the backend returns: [{field, old, new}] or facts with superseded values. */
function normHistory(j: any): BatteryChange[] | null {
  const h = j?.history ?? j?.changes;
  if (!Array.isArray(h)) return null;
  const out: BatteryChange[] = [];
  for (const x of h) {
    if (x.field !== undefined) out.push({ field: x.field, old: x.old ?? x.old_value, new: x.new ?? x.new_value, at: x.at ?? x.valid_from, source: x.source });
    else if (x.value && x.previous) {
      const a = normBattery(x.previous);
      const b = normBattery(x.value);
      out.push(...diff(a, b, x.source).map((c) => ({ ...c, at: x.valid_from ?? c.at })));
    }
  }
  return out;
}

export async function loadBattery(): Promise<{ battery: Battery; history: BatteryChange[]; source: "api" | "local" }> {
  if (!MOCK) {
    try {
      const r = await fetch(`${API}/api/battery`);
      if (r.ok) {
        const j = await r.json();
        return { battery: normBattery(j), history: normHistory(j) ?? readLocal(LSH, []), source: "api" };
      }
    } catch {
      /* fall through */
    }
  }
  return { battery: readLocal(LS, DEFAULT), history: readLocal(LSH, []), source: "local" };
}

export async function saveBattery(prev: Battery, next: Battery): Promise<{ history: BatteryChange[]; source: "api" | "local" }> {
  const local = [...diff(prev, next), ...readLocal<BatteryChange[]>(LSH, [])].slice(0, 30);
  writeLocal(LS, next);
  writeLocal(LSH, local);
  if (!MOCK) {
    try {
      const r = await fetch(`${API}/api/battery`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...next, soc_pct: (next.stored_mwh / next.energy_mwh) * 100 }) });
      if (r.ok) {
        const j = await r.json().catch(() => ({}));
        return { history: normHistory(j) ?? local, source: "api" };
      }
    } catch {
      /* fall through */
    }
  }
  return { history: local, source: "local" };
}
