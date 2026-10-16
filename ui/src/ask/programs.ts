// Client-built OpenUI programs (same components the server uses). Used for the instant first answer
// (fast optimizer path), for voice tool results, and for ?mock=1.
/* eslint-disable @typescript-eslint/no-explicit-any */
import { normalizePlan } from "../api";

export type Stmts = Record<string, string>;
const j = (v: unknown) => JSON.stringify(v ?? null);

function hours(ev: any) {
  const p = normalizePlan(ev);
  return p.rows.map((r) => ({ hour: r.hour, price: r.price, charge_mw: +r.charge_mw.toFixed(2), discharge_mw: +r.discharge_mw.toFixed(2), soc_pct: r.soc_pct }));
}

function windows(ev: any): string {
  const p = normalizePlan(ev);
  const hl = (h: number) => (h % 12 === 0 ? 12 : h % 12) + (h % 24 < 12 ? "am" : "pm");
  const runs = (pred: (r: (typeof p.rows)[number]) => boolean) => {
    const out: string[] = [];
    let start: number | null = null;
    p.rows.forEach((r, i) => {
      if (pred(r) && start === null) start = r.hour;
      const next = p.rows[i + 1];
      if (start !== null && (!next || !pred(next))) {
        out.push(`${hl(start)} to ${hl(r.hour + 1)}`);
        start = null;
      }
    });
    return out;
  };
  const b = runs((r) => r.charge_mw > 1e-6);
  const s = runs((r) => r.discharge_mw > 1e-6);
  if (!b.length && !s.length) return "Holds all day: no hour is profitable under your rules.";
  return `Buys ${b.join(", ") || "never"}; sells ${s.join(", ") || "never"}.`;
}

export function planProgram(ev: any, label = "profit tomorrow"): Stmts {
  const p = normalizePlan(ev);
  const date = p.date ? `, ${p.date}` : "";
  const st: Stmts = {
    headline: `headline = ProfitHeadline(${j(+p.profit_usd.toFixed(2))}, ${j(label + date)}, ${j(windows(ev))})`,
    chart: `chart = BuySellChart(${j("Buy low, sell high")}, ${j(hours(ev))})`,
    checks: `checks = SafetyChecks(${j(p.checks.map((c) => ({ name: c.name, ok: c.ok, detail: c.detail ?? null })))})`,
    approve: `approve = ApproveCard(${j(p.plan_id ?? "")}, ${j(+p.profit_usd.toFixed(2))}, ${j(p.voice_text ?? null)})`,
  };
  st.root = `root = Stack([headline, chart, checks, approve])`;
  return st;
}

export function playProgram(ev: any, battery: any): Stmts {
  const p = normalizePlan(ev);
  return {
    headline: `headline = ProfitHeadline(${j(+p.profit_usd.toFixed(2))}, ${j("profit tomorrow, hour by hour")}, ${j(windows(ev))})`,
    play: `play = PlayTomorrow(${j(hours(ev))}, ${j(+p.profit_usd.toFixed(2))}, ${j(battery)})`,
    homes: `homes = HomesPowered(${j(hours(ev))})`,
    root: `root = Stack([headline, play, homes])`,
  };
}

export function gridProgram(): Stmts {
  return { now: "now = LivePrice()", map: "map = ZoneMap()", root: "root = Stack([now, map])" };
}

export function bidsProgram(ev: any): Stmts {
  const p = normalizePlan(ev);
  return {
    bids: `bids = BidsTable(${j(p.bids ?? [])}, ${j(p.date ?? null)}, ${j(hours(ev))})`,
    approve: `approve = ApproveCard(${j(p.plan_id ?? "")}, ${j(+p.profit_usd.toFixed(2))}, ${j(p.voice_text ?? null)})`,
    root: "root = Stack([bids, approve])",
  };
}

export function backtestProgram(bt: { days: { date: string; optimal: number; baseline: number }[]; total: number; total_baseline: number }): Stmts {
  const days = bt.days.map((d) => ({ date: d.date, profit: +d.optimal.toFixed(2), baseline: +d.baseline.toFixed(2) }));
  return {
    headline: `headline = ProfitHeadline(${j(+bt.total.toFixed(2))}, ${j(`profit over the last ${days.length} real days`)}, ${j(`The simple buy-at-night, sell-at-evening rule made ${"$" + Math.round(bt.total_baseline).toLocaleString("en-US")}.`)})`,
    cal: `cal = BacktestCalendar(${j(days)}, ${j({ optimal: +bt.total.toFixed(2), baseline: +bt.total_baseline.toFixed(2) })})`,
    root: "root = Stack([headline, cal])",
  };
}

export function memoryProgram(): Stmts {
  return { mem: "mem = MemoryTimeline()", root: "root = Stack([mem])" };
}

/** Which client program answers a query (mock mode, and fallback when the API cannot answer). */
export function route(q: string): "grid" | "play" | "bids" | "backtest" | "memory" | "plan" {
  const s = q.toLowerCase();
  if (/grid|zone|map|right now|price now/.test(s)) return "grid";
  if (/play|hour by hour|scrub/.test(s)) return "play";
  if (/bid/.test(s)) return "bids";
  if (/30 days|last month|history|backtest|last week|how did we do/.test(s)) return "backtest";
  if (/memory|remember|changed/.test(s)) return "memory";
  return "plan";
}
