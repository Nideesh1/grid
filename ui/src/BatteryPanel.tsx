import { useEffect, useState } from "react";
import { fmtUsd, type Plan } from "./api";
import { PRESETS, type Battery, type BatteryChange } from "./battery";
import { ZONES } from "./live";

const LABEL: Record<string, string> = {
  power_mw: "power MW",
  energy_mwh: "storage MWh",
  stored_mwh: "stored MWh",
  zone: "zone",
  reserve_pct: "reserve %",
  max_cycles: "cycles/day",
  efficiency_pct: "efficiency %",
  sell_above: "sell above $",
  buy_below: "buy below $",
};
const show = (v: unknown) => (v === null || v === undefined || v === "" ? "any" : String(v));

export function BatteryPanel({
  battery,
  history,
  source,
  saving,
  onSave,
}: {
  battery: Battery;
  history: BatteryChange[];
  source: "api" | "local";
  saving: boolean;
  onSave: (b: Battery) => void;
}) {
  const [f, setF] = useState<Battery>(battery);
  const [storedPct, setStoredPct] = useState(false);
  useEffect(() => setF(battery), [battery]);
  const set = <K extends keyof Battery>(k: K, v: Battery[K]) => setF((x) => ({ ...x, [k]: v, name: undefined }));
  const num = (k: keyof Battery, opt = false) => ({
    value: f[k] === null || f[k] === undefined ? "" : String(f[k]),
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => set(k, (opt && e.target.value === "" ? null : +e.target.value) as never),
  });
  const dirty = JSON.stringify({ ...f, name: undefined }) !== JSON.stringify({ ...battery, name: undefined });

  return (
    <section className="mybat">
      <div className="mb-head">
        <span className="kicker">My battery</span>
        <span className="fine mono">{source === "api" ? "saved to /api/battery" : "saved in this browser (API pending)"}</span>
      </div>
      <div className="presets">
        {PRESETS.map((p) => (
          <button key={p.name} className={`chip ${battery.name === p.name || (f.power_mw === p.power_mw && f.energy_mwh === p.energy_mwh) ? "on" : ""}`} onClick={() => setF(p)}>
            {p.name}
          </button>
        ))}
      </div>
      <div className="mb-grid">
        <label>Power MW<input type="number" step="0.1" min="0.1" {...num("power_mw")} /></label>
        <label>Storage MWh<input type="number" step="0.1" min="0.1" {...num("energy_mwh")} /></label>
        <label>
          Stored now{" "}
          <button className="unit-toggle" onClick={() => setStoredPct((x) => !x)}>{storedPct ? "%" : "MWh"}</button>
          {storedPct ? (
            <input type="number" min="0" max="100" value={Math.round((f.stored_mwh / f.energy_mwh) * 100)} onChange={(e) => set("stored_mwh", (+e.target.value / 100) * f.energy_mwh)} />
          ) : (
            <input type="number" step="0.1" min="0" {...num("stored_mwh")} />
          )}
        </label>
        <label>
          Zone
          <select value={f.zone} onChange={(e) => set("zone", e.target.value)}>
            {ZONES.map((z) => (
              <option key={z}>{z}</option>
            ))}
          </select>
        </label>
        <label>Reserve %<input type="number" min="0" max="90" {...num("reserve_pct")} /></label>
        <label>Max cycles/day<input type="number" step="0.1" min="0" {...num("max_cycles")} /></label>
        <label>Efficiency %<input type="number" min="50" max="100" {...num("efficiency_pct")} /></label>
        <label>Only sell above $<input type="number" placeholder="any" {...num("sell_above", true)} /></label>
        <label>Only buy below $<input type="number" placeholder="any" {...num("buy_below", true)} /></label>
      </div>
      <button className="primary save" disabled={!dirty || saving} onClick={() => onSave(f)}>
        {saving ? "Saving and re-planning..." : dirty ? "Save and re-plan" : "Saved"}
      </button>
      {history.length > 0 && (
        <div className="mb-hist">
          <span className="kicker">Profile history (SurrealDB)</span>
          <ul>
            {history.slice(0, 6).map((c, i) => (
              <li key={i}>
                <span className="mono k">{LABEL[c.field] ?? c.field}</span>
                <s>{show(c.old)}</s> <span className="arrow">→</span> <b>{show(c.new)}</b>
                <span className="mono t">{c.source ?? ""} {c.at ? new Date(c.at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export type Bid = { hour: number; side: "buy" | "sell"; mw: number; price_limit: number | null };

/* eslint-disable @typescript-eslint/no-explicit-any */
export function bidsFrom(plan: Plan, raw: any): { bids: Bid[]; derived: boolean } {
  if (Array.isArray(raw) && raw.length) {
    return {
      derived: false,
      bids: raw.map((b: any) => ({
        hour: typeof b.hour === "number" ? b.hour : parseInt(String(b.hour ?? b.ts).replace(/^.*T/, ""), 10),
        side: /sell|discharge/i.test(String(b.side)) ? "sell" : "buy",
        mw: +(b.mw ?? b.quantity_mw ?? 0),
        price_limit: b.price_limit ?? b.limit ?? b.price ?? null,
      })),
    };
  }
  const bids: Bid[] = [];
  for (const r of plan.rows) {
    if (r.charge_mw > 1e-6) bids.push({ hour: r.hour, side: "buy", mw: r.charge_mw, price_limit: r.price });
    if (r.discharge_mw > 1e-6) bids.push({ hour: r.hour, side: "sell", mw: r.discharge_mw, price_limit: r.price });
  }
  return { bids, derived: true };
}

function hr(h: number) {
  const f = (x: number) => (x % 12 === 0 ? 12 : x % 12) + (x % 24 < 12 ? "am" : "pm");
  return `${f(h)} to ${f(h + 1)}`;
}

export function BidsTable({ plan, raw }: { plan: Plan; raw: unknown }) {
  const { bids, derived } = bidsFrom(plan, raw);
  if (!bids.length)
    return <p className="hold-note">No bids tomorrow. Under your rules no hour is profitable, so the battery holds.</p>;
  return (
    <section className="bids">
      <table>
        <thead>
          <tr><th>Hour</th><th>Side</th><th className="r">MW</th><th className="r">Price limit</th></tr>
        </thead>
        <tbody>
          {bids.map((b, i) => (
            <tr key={i} className={b.side}>
              <td className="mono">{hr(b.hour)}</td>
              <td className="side">{b.side === "buy" ? "BUY" : "SELL"}</td>
              <td className="mono r">{b.mw.toFixed(2)}</td>
              <td className="mono r">{b.price_limit === null ? "market" : `${b.side === "buy" ? "≤" : "≥"} ${fmtUsd(+b.price_limit, 2)}/MWh`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="fine">
        Day-ahead bids{plan.date ? ` for ${plan.date}` : ""}. Submit via a NYISO market participant, your utility, or an aggregator.
        {derived ? " Derived from the plan schedule at the forecast price." : ""}
      </p>
    </section>
  );
}
