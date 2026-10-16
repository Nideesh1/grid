// Landing sections: live price + signal, NYISO zone map, "Play tomorrow" scrubber, homes served,
// battery size slider, 30-day profit calendar.
import { useEffect, useMemo, useState } from "react";
import { fmtInt, fmtUsd, type Backtest, type Plan } from "./api";
import { getLive, type Live } from "./live";

const GREEN = "#38f28a";
const GOLD = "#f2b544";
const RED = "#ff5b3d";
const CYAN = "#3fd0e0";
const HOME_KW = 1.1; // average NYC household load, kW (assumption, shown in UI)

function lerp(a: string, b: string, t: number): string {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return "#" + pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, "0")).join("");
}
/** cheap cyan -> gold -> red */
function heat(t: number): string {
  const x = Math.max(0, Math.min(1, t));
  return x < 0.5 ? lerp(CYAN, GOLD, x * 2) : lerp(GOLD, RED, (x - 0.5) * 2);
}
function hourLabel(h: number): string {
  if (h === 0 || h === 24) return "12am";
  if (h === 12) return "12pm";
  return h < 12 ? `${h}am` : `${h - 12}pm`;
}
function tsLabel(ts: string): string {
  const m = ts.match(/(\d{1,2}):(\d{2})/);
  if (!m) return ts;
  const h = +m[1];
  return `${h % 12 === 0 ? 12 : h % 12}:${m[2]}${h < 12 ? "am" : "pm"}`;
}

/* ------------------------------------------------------------------ live strip + map */

export function useLive(): Live | null {
  const [live, setLive] = useState<Live | null>(null);
  useEffect(() => {
    let on = true;
    const load = () => getLive().then((l) => on && setLive(l));
    load();
    const t = setInterval(load, 60_000);
    return () => {
      on = false;
      clearInterval(t);
    };
  }, []);
  return live;
}

export function LiveStrip({ live, plan }: { live: Live | null; plan: Plan | null }) {
  if (!live) return <section className="live-strip"><p className="fine">Reading NYISO real-time prices...</p></section>;
  const nyc = live.zones.find((z) => z.name === "N.Y.C.");
  const now = nyc?.usd_mwh ?? live.nycToday[live.nycToday.length - 1]?.usd_mwh;
  const series = live.nycToday.map((p) => p.usd_mwh);
  const pool = [...series, ...(plan?.rows.map((r) => r.price).filter((x): x is number => x !== null) ?? [])];
  const rank = now !== undefined && pool.length ? pool.filter((v) => v < now).length / pool.length : 0.5;
  const signal = rank <= 0.3 ? "BUY" : rank >= 0.7 ? "SELL" : "HOLD";
  const lo = Math.min(...series);
  const hi = Math.max(...series);
  const W = 420;
  const H = 54;
  const path = series.map((v, i) => `${i ? "L" : "M"}${((i / Math.max(1, series.length - 1)) * W).toFixed(1)},${(H - ((v - lo) / Math.max(1e-6, hi - lo)) * H).toFixed(1)}`).join("");

  return (
    <section className="live-strip">
      <div className="now">
        <span className="kicker">NYC power right now</span>
        <span className="now-price mono">{now !== undefined ? `$${now.toFixed(2)}` : "n/a"}<small>/MWh</small></span>
        <span className="fine mono">
          N.Y.C. real-time 5-min LBMP · {tsLabel(live.ts)} · {live.source === "mock" ? "sample data" : "NYISO"}
        </span>
      </div>
      <div className={`signal ${signal.toLowerCase()}`}>
        <span className="lamp" />
        <span className="sig-word">{signal}</span>
        <span className="fine">{Math.round(rank * 100)}th percentile of today's prices</span>
      </div>
      {series.length > 2 && (
        <div className="spark">
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" width="100%" height={H}>
            <path d={path} fill="none" stroke="#e8e6e1" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
          </svg>
          <div className="spark-axis mono">
            <span>12am</span>
            <span>
              low ${lo.toFixed(0)} · high ${hi.toFixed(0)}
            </span>
            <span>{tsLabel(live.ts)}</span>
          </div>
        </div>
      )}
    </section>
  );
}

// Schematic tile layout of the 11 NYISO load zones (rough geography, not to scale). Units: 100px.
const TILES: Record<string, { x: number; y: number; w: number; h: number; label: string }> = {
  WEST: { x: 0, y: 1.7, w: 1.25, h: 1.25, label: "West" },
  GENESE: { x: 1.25, y: 1.5, w: 1.05, h: 1.25, label: "Genesee" },
  CENTRL: { x: 2.3, y: 1.35, w: 1.25, h: 1.6, label: "Central" },
  NORTH: { x: 3.55, y: 0, w: 1.55, h: 1.25, label: "North" },
  "MHK VL": { x: 3.55, y: 1.25, w: 1.2, h: 1.35, label: "Mohawk V." },
  CAPITL: { x: 4.75, y: 1.25, w: 0.95, h: 1.9, label: "Capital" },
  "HUD VL": { x: 3.95, y: 3.15, w: 1.45, h: 1.05, label: "Hudson V." },
  MILLWD: { x: 4.35, y: 4.2, w: 1.0, h: 0.5, label: "Millwood" },
  DUNWOD: { x: 4.35, y: 4.7, w: 1.0, h: 0.45, label: "Dunwoodie" },
  "N.Y.C.": { x: 3.95, y: 5.15, w: 1.2, h: 0.85, label: "N.Y.C." },
  LONGIL: { x: 5.15, y: 4.95, w: 1.85, h: 0.6, label: "Long Island" },
};

export function ZoneMap({ live }: { live: Live | null }) {
  const [hover, setHover] = useState<string | null>(null);
  const zones = live?.zones ?? [];
  const ps = zones.map((z) => z.usd_mwh);
  const lo = Math.min(...ps);
  const hi = Math.max(...ps);
  const price = (n: string) => zones.find((z) => z.name === n)?.usd_mwh;
  const U = 100;
  const nyc = TILES["N.Y.C."];
  const hz = hover ? zones.find((z) => z.name === hover) : null;
  return (
    <section className="zonemap">
      <div className="zm-head">
        <span className="kicker">New York grid, real-time price by zone</span>
        <span className="fine mono">{hz ? `${hz.name}  $${hz.usd_mwh.toFixed(2)}/MWh` : "hover a zone"}</span>
      </div>
      <svg viewBox={`-10 -10 ${7.2 * U} ${6.25 * U}`} width="100%" role="img" aria-label="Schematic map of NYISO zones colored by real-time price">
        <defs>
          <filter id="glow" x="-200%" y="-200%" width="500%" height="500%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>
        {Object.entries(TILES).map(([name, t]) => {
          const p = price(name);
          const fill = p === undefined ? "#1a1a18" : heat(hi > lo ? (p - lo) / (hi - lo) : 0.5);
          return (
            <g key={name} onMouseEnter={() => setHover(name)} onMouseLeave={() => setHover(null)} style={{ cursor: "default" }}>
              <title>{`${name}: ${p !== undefined ? `$${p.toFixed(2)}/MWh` : "no data"}`}</title>
              <rect x={t.x * U} y={t.y * U} width={t.w * U - 3} height={t.h * U - 3} fill={fill} fillOpacity={hover === name ? 0.95 : 0.78} stroke={hover === name ? "#e8e6e1" : "#0a0a09"} strokeWidth={hover === name ? 2 : 1} />
              <text x={t.x * U + 8} y={t.y * U + 18} className="zm-label">{t.label}</text>
              {p !== undefined && (
                <text x={t.x * U + 8} y={t.y * U + 36} className="zm-price">${p.toFixed(0)}</text>
              )}
            </g>
          );
        })}
        <g transform={`translate(${(nyc.x + nyc.w - 0.28) * U}, ${(nyc.y + nyc.h - 0.3) * U})`}>
          <circle r={16} fill={GREEN} opacity={0.6} filter="url(#glow)" className="pin-glow" />
          <rect x={-7} y={-11} width={14} height={22} fill="#0a0a09" stroke={GREEN} strokeWidth={2} />
          <rect x={-3} y={-14} width={6} height={3} fill={GREEN} />
          <rect x={-4} y={-2} width={8} height={10} fill={GREEN} />
        </g>
      </svg>
      <div className="zm-legend mono">
        <span className="ramp" style={{ background: `linear-gradient(90deg, ${CYAN}, ${GOLD}, ${RED})` }} />
        <span>
          ${lo.toFixed(0)} to ${hi.toFixed(0)}/MWh · schematic, not to scale · battery at N.Y.C.
          {live?.source === "mock" ? " · sample data" : ""}
        </span>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ play tomorrow */

export function PlayTomorrow({ plan, showSold = true }: { plan: Plan; showSold?: boolean }) {
  const rows = plan.rows;
  const [h, setH] = useState(rows.length); // hours elapsed (0..24)
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      setH((x) => {
        if (x >= rows.length) {
          setPlaying(false);
          return x;
        }
        return x + 1;
      });
    }, 450);
    return () => clearInterval(t);
  }, [playing, rows.length]);

  const totalOut = rows.reduce((a, r) => a + r.discharge_mw, 0);
  const degRate = plan.degradation_usd !== undefined && totalOut > 0 ? plan.degradation_usd / totalOut : 8;
  const upto = rows.slice(0, h);
  const spent = upto.reduce((a, r) => a + r.charge_mw * (r.price ?? 0), 0);
  const earned = upto.reduce((a, r) => a + r.discharge_mw * (r.price ?? 0), 0);
  const wear = upto.reduce((a, r) => a + r.discharge_mw * degRate, 0);
  const profit = earned - spent - wear;
  const cur = h > 0 ? rows[h - 1] : null;
  const soc = cur?.soc_pct ?? rows[0]?.soc_pct ?? 50;
  const mode = cur && cur.charge_mw > 1e-6 ? "buy" : cur && cur.discharge_mw > 1e-6 ? "sell" : "idle";
  const homes = cur && cur.discharge_mw > 0 ? (cur.discharge_mw * 1000) / HOME_KW : 0;
  const delivered = upto.reduce((a, r) => a + r.discharge_mw, 0);
  const sellHours = rows.filter((r) => r.discharge_mw > 1e-6).map((r) => r.hour);
  const peakHomes = Math.max(0, ...rows.map((r) => (r.discharge_mw * 1000) / HOME_KW));

  return (
    <section className="play">
      <div className="play-top">
        <button className="secondary play-btn" onClick={() => { if (h >= rows.length) setH(0); setPlaying((p) => !p); }}>
          {playing ? "❚❚ Pause" : h >= rows.length ? "▶ Replay tomorrow" : "▶ Play tomorrow"}
        </button>
        <input className="scrub" type="range" min={0} max={rows.length} step={1} value={h} onChange={(e) => { setPlaying(false); setH(+e.target.value); }} />
        <span className="mono clock">{h === 0 ? "start of day" : `${hourLabel(rows[h - 1].hour)} to ${hourLabel(rows[h - 1].hour + 1)}`}</span>
      </div>
      <div className={`play-body ${showSold ? "" : "two"}`}>
        <div className={`battery ${mode}`}>
          <div className="cap" />
          <div className="shell">
            <div className="fill" style={{ height: `${soc}%` }} />
            <div className="lim" style={{ bottom: "90%" }} />
            <div className="lim" style={{ bottom: `${plan.reserve_pct ?? 10}%` }} />
          </div>
          <span className="mono pct">{soc.toFixed(0)}%</span>
          <span className="mode">{mode === "buy" ? `BUYING ${cur!.charge_mw.toFixed(1)} MW` : mode === "sell" ? `SELLING ${cur!.discharge_mw.toFixed(1)} MW` : "HOLDING"}</span>
          {cur?.price != null && <span className="mono fine">${cur.price.toFixed(2)}/MWh</span>}
        </div>
        <div className="cash">
          <div className="row"><span>Spent on buys</span><b className="mono" style={{ color: GREEN }}>{fmtUsd(-spent)}</b></div>
          <div className="row"><span>Earned on sells</span><b className="mono" style={{ color: GOLD }}>{fmtUsd(earned)}</b></div>
          <div className="row"><span>Battery wear</span><b className="mono dim">{fmtUsd(-wear)}</b></div>
          <div className="row total"><span>Running profit</span><b className="mono">{fmtUsd(profit)}</b></div>
          {h >= rows.length && <p className="fine">Ends at the plan's {fmtUsd(plan.profit_usd)}.</p>}
        </div>
        {showSold && <div className="sold">
          <span className="kicker">Who you sold to</span>
          {mode === "sell" ? (
            <>
              <span className="homes mono">~{fmtInt(homes)}</span>
              <span>NYC homes powered right now</span>
            </>
          ) : (
            <>
              <span className="homes mono dim">~{fmtInt(peakHomes)}</span>
              <span>NYC homes at full sell power{sellHours.length ? ` (sells ${sellHours.map((x) => hourLabel(x)).join(", ")})` : ""}</span>
            </>
          )}
          <span className="mono delivered">{delivered.toFixed(1)} of {totalOut.toFixed(1)} MWh delivered when prices peaked</span>
          <span className="fine">Assumes {HOME_KW} kW average NYC home load.</span>
        </div>}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ size slider */

export function SizeSlider({ plan, backtest, battery }: { plan: Plan; backtest: Backtest | null; battery: { power_mw: number; energy_mwh: number } }) {
  const base = battery.power_mw || 10;
  const hours = battery.energy_mwh / base || 4;
  const [mw, setMw] = useState(base);
  useEffect(() => setMw(base), [base]);
  const k = mw / base;
  const perDay = backtest && backtest.days.length ? backtest.total / backtest.days.length : plan.profit_usd;
  const peakMw = Math.max(0, ...plan.rows.map((r) => r.discharge_mw));
  return (
    <section className="size">
      <div className="size-head">
        <span className="kicker">How big is your battery?</span>
        <span className="mono size-val">{mw} MW / {+(mw * hours).toFixed(1)} MWh</span>
      </div>
      <input className="scrub" type="range" min={Math.min(1, base)} max={100} step={mw < 1 ? 0.1 : 1} value={mw} onChange={(e) => setMw(+e.target.value)} />
      <div className="size-grid">
        <div><b className="mono">{fmtUsd(plan.profit_usd * k)}</b><span>profit tomorrow</span></div>
        <div><b className="mono">~{fmtInt((peakMw * k * 1000) / HOME_KW)}</b><span>homes at the peak</span></div>
        <div><b className="mono">{fmtUsd(perDay * 365 * k)}</b><span>per year, at the 30-day average</span></div>
      </div>
      <p className="fine">Linear estimate scaled from your {base} MW plan ({+hours.toFixed(1)}-hour duration). Large batteries move prices; real profit grows slower.</p>
    </section>
  );
}

/* ------------------------------------------------------------------ calendar heatmap */

export function ProfitCalendar({ data }: { data: Backtest | null }) {
  const cells = useMemo(() => {
    if (!data?.days.length) return [];
    const first = new Date(data.days[0].date + "T12:00:00");
    const lead = first.getDay();
    return [...Array(lead).fill(null), ...data.days];
  }, [data]);
  if (!data?.days.length) return null;
  const max = Math.max(...data.days.map((d) => d.optimal), 1);
  const weeks = Math.ceil(cells.length / 7);
  return (
    <section className="cal">
      <div className="cal-grid" style={{ gridTemplateColumns: `28px repeat(${weeks}, 44px)` }}>
        {["S", "M", "T", "W", "T", "F", "S"].map((d, r) => (
          <div key={`l${r}`} className="cal-dow mono" style={{ gridColumn: 1, gridRow: r + 1 }}>{d}</div>
        ))}
        {cells.map((d, i) =>
          d ? (
            <div
              key={i}
              className="cal-cell"
              style={{ gridColumn: Math.floor(i / 7) + 2, gridRow: (i % 7) + 1, background: d.optimal <= 0 ? "#2a1512" : lerp("#1d1a12", GOLD, Math.sqrt(d.optimal / max)) }}
              title={`${d.date}: agent ${fmtUsd(d.optimal, 2)} · rule ${fmtUsd(d.baseline, 2)}`}
            >
              <span className="mono">{+d.date.slice(8)}</span>
            </div>
          ) : (
            <div key={i} style={{ gridColumn: Math.floor(i / 7) + 2, gridRow: (i % 7) + 1 }} />
          ),
        )}
      </div>
      <div className="cal-totals">
        <div><b className="mono" style={{ color: GOLD }}>{fmtUsd(data.total)}</b><span>agent, {data.days.length} real days</span></div>
        <div><b className="mono dim">{fmtUsd(data.total_baseline)}</b><span>simple buy-at-night / sell-at-evening rule</span></div>
        <div><b className="mono" style={{ color: GREEN }}>{data.total_baseline ? `${((data.total / data.total_baseline - 1) * 100).toFixed(0)}%` : "n/a"}</b><span>more than the rule</span></div>
      </div>
    </section>
  );
}
