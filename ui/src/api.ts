// API client + normalizers. Shapes follow CONTRACTS.md; every field is read defensively so
// small backend differences do not blank the page.
import { mockBacktest, mockMemory, mockPlanEvents } from "./mock";

export const API: string = (import.meta.env.VITE_API as string | undefined) ?? "http://localhost:8520";
export const MOCK = new URLSearchParams(window.location.search).get("mock") === "1";

export type PricePoint = { ts: string; usd_mwh: number };
export type Check = { name: string; ok: boolean; detail?: string };
export type Row = { hour: number; price: number | null; charge_mw: number; discharge_mw: number; soc_pct: number | null };

export type Plan = {
  plan_id?: string;
  date?: string;
  rows: Row[];
  profit_usd: number;
  baseline_profit_usd?: number;
  revenue_usd?: number;
  cost_usd?: number;
  backtest_total_usd?: number;
  degradation_usd?: number;
  mwh_discharged?: number;
  cycles?: number;
  min_soc?: number;
  max_soc?: number;
  end_soc?: number;
  reserve_pct?: number;
  violations: number;
  status?: string;
  hold: boolean;
  bids?: unknown;
  sandbox?: string; // Daytona sandbox id, or undefined = local
  checks: Check[];
  audio_url?: string;
  voice_text?: string;
};

export type BacktestDay = { date: string; baseline: number; optimal: number };
export type Backtest = { days: BacktestDay[]; total: number; total_baseline: number };
export type Progress = { n: number; of: number; sandbox?: string; text?: string };

export type StreamEvent = { kind: string; [k: string]: any };

/* eslint-disable @typescript-eslint/no-explicit-any */
const num = (v: any): number | undefined => (typeof v === "number" && isFinite(v) ? v : typeof v === "string" && v !== "" && isFinite(+v) ? +v : undefined);

/** First numeric value found at any of the dotted paths. */
function pick(o: any, paths: string[]): number | undefined {
  for (const p of paths) {
    const v = p.split(".").reduce((a: any, k) => (a == null ? undefined : a[k]), o);
    const n = num(v);
    if (n !== undefined) return n;
  }
  return undefined;
}
function pickStr(o: any, paths: string[]): string | undefined {
  for (const p of paths) {
    const v = p.split(".").reduce((a: any, k) => (a == null ? undefined : a[k]), o);
    if (typeof v === "string" && v) return v;
  }
  return undefined;
}

export function hourOf(x: any): number {
  if (typeof x === "number") return ((Math.floor(x) % 24) + 24) % 24;
  const s = String(x);
  const m = s.match(/[T ](\d{2}):/) ?? s.match(/^(\d{1,2}):\d{2}/);
  if (m) return +m[1];
  if (/^\d{1,2}$/.test(s)) return +s;
  return new Date(s).getHours();
}

const pct = (v: number | undefined) => (v === undefined ? undefined : Math.abs(v) <= 1.0001 ? v * 100 : v);

function rowsFrom(ev: any): Row[] {
  const raw = [ev.schedule, ev.optimal?.schedule, ev.plan?.schedule, ev.hours].find((x) => Array.isArray(x) && x.length) ?? [];
  const priceMap = new Map<number, number>();
  const pr = Array.isArray(ev.prices) ? ev.prices : ev.prices?.day_ahead ?? [];
  for (const p of pr) {
    const v = num(p.usd_mwh ?? p.price ?? p.lbmp);
    if (v !== undefined) priceMap.set(hourOf(p.hour ?? p.ts), v);
  }
  return raw.map((r: any, i: number) => {
    const hour = hourOf(r.hour ?? r.ts ?? r.time ?? i);
    const signed = num(r.mw ?? r.net_mw ?? r.power_mw);
    let charge = num(r.charge_mw ?? r.buy_mw ?? r.charge) ?? (signed !== undefined && signed < 0 ? -signed : 0);
    let discharge = num(r.discharge_mw ?? r.sell_mw ?? r.discharge) ?? (signed !== undefined && signed > 0 ? signed : 0);
    charge = Math.abs(charge);
    discharge = Math.abs(discharge);
    return {
      hour,
      price: num(r.price ?? r.usd_mwh ?? r.price_usd_mwh ?? r.lbmp) ?? priceMap.get(hour) ?? null,
      charge_mw: charge,
      discharge_mw: discharge,
      soc_pct: pct(num(r.soc_pct ?? r.soc ?? r.fill_pct)) ?? null,
    };
  });
}

export function normalizePlan(ev: any): Plan {
  const o = ev.optimal ?? ev.sim ?? {};
  const rows = rowsFrom(ev);
  const socs = rows.map((r) => r.soc_pct).filter((x): x is number => x !== null);
  const minSoc = pct(pick(ev, ["checks.min_soc", "optimal.min_soc", "sim.min_soc", "min_soc"])) ?? (socs.length ? Math.min(...socs) : undefined);
  const maxSoc = pct(pick(ev, ["checks.max_soc", "optimal.max_soc", "sim.max_soc", "max_soc"])) ?? (socs.length ? Math.max(...socs) : undefined);
  const endSoc = pct(pick(ev, ["checks.end_soc", "optimal.end_soc", "sim.end_soc", "end_soc"])) ?? (socs.length ? socs[socs.length - 1] : undefined);
  const cycles = pick(ev, ["checks.cycles", "optimal.cycles", "sim.cycles", "cycles"]);
  const reserve = pct(pick(ev, ["reserve_pct", "constraints.reserve_pct", "battery.min_soc_pct", "checks.reserve_pct"]));
  const violList = ev.checks?.violations ?? o.violations ?? ev.violations;
  const violations = Array.isArray(violList) ? violList.length : num(violList) ?? 0;
  const sandbox = pickStr(ev, ["sandbox_id", "optimal.sandbox_id", "checks.sandbox_id", "sandbox.id", "sandbox"]);
  const where = pickStr(ev, ["simulated_in", "optimal.where", "where"]);
  const local = !sandbox || /^local/i.test(sandbox) || where === "local";

  let checks: Check[];
  if (Array.isArray(ev.checks)) {
    checks = ev.checks.map((x: any) => ({ name: String(x.name ?? x.label ?? x.check), ok: !!(x.ok ?? x.pass ?? x.passed), detail: x.detail ?? x.value }));
  } else {
    const lo = reserve ?? 10;
    const both = rows.some((r) => r.charge_mw > 1e-6 && r.discharge_mw > 1e-6);
    checks = [
      { name: `Fill stays between ${Math.round(lo)}% and 90%`, ok: minSoc === undefined || (minSoc >= lo - 0.01 && (maxSoc ?? 0) <= 90.01), detail: minSoc !== undefined ? `${minSoc.toFixed(0)}% to ${(maxSoc ?? 0).toFixed(0)}%` : undefined },
      { name: "At most 1 full cycle (warranty)", ok: cycles === undefined || cycles <= 1.0001, detail: cycles !== undefined ? `${cycles.toFixed(2)} cycles` : undefined },
      { name: "Never buys and sells in the same hour", ok: !both, detail: both ? "overlap found" : "0 overlapping hours" },
      { name: "Ends at least 50% full", ok: endSoc === undefined || endSoc >= 49.99, detail: endSoc !== undefined ? `ends ${endSoc.toFixed(0)}%` : undefined },
      { name: local ? "Simulated locally" : "Simulated in Daytona", ok: violations === 0, detail: local ? "Daytona unavailable" : sandbox },
    ];
  }

  return {
    plan_id: ev.plan_id ?? ev.id,
    date: ev.date ?? ev.day ?? ev.night,
    rows,
    profit_usd: pick(ev, ["profit_usd", "profit.optimal", "profit.tomorrow_usd", "profit_tomorrow_usd", "tomorrow_usd", "optimal.profit_usd", "sim.profit_usd", "savings.profit_usd", "profit"]) ?? 0,
    baseline_profit_usd: pick(ev, ["baseline_profit_usd", "baseline_profit", "profit.baseline", "baseline.profit_usd"]),
    revenue_usd: pick(ev, ["revenue_usd", "optimal.revenue_usd", "sim.revenue_usd"]),
    cost_usd: pick(ev, ["cost_usd", "optimal.cost_usd", "sim.cost_usd"]),
    backtest_total_usd: pick(ev, ["backtest_total_usd", "backtest.total", "backtest.optimal_profit", "backtest.totals.optimal_profit", "last30_usd", "profit_30d_usd"]),
    degradation_usd: pick(ev, ["degradation_usd", "optimal.degradation_usd", "sim.degradation_usd"]),
    mwh_discharged: pick(ev, ["mwh_discharged", "optimal.mwh_discharged", "sim.mwh_discharged"]),
    cycles,
    min_soc: minSoc,
    max_soc: maxSoc,
    end_soc: endSoc,
    reserve_pct: reserve,
    violations,
    status: ev.status,
    hold: /hold/i.test(String(ev.verdict ?? ev.action ?? "")) || (rows.length > 0 && rows.every((r) => r.charge_mw < 1e-6 && r.discharge_mw < 1e-6)),
    bids: ev.bids ?? ev.optimal?.bids,
    sandbox: local ? undefined : sandbox,
    checks,
    audio_url: audioFrom(ev),
    voice_text: ev.voice_text ?? ev.message ?? ev.explanation,
  };
}

export function audioFrom(x: any): string | undefined {
  if (!x) return undefined;
  if (typeof x.audio_url === "string") return x.audio_url.startsWith("/") ? API + x.audio_url : x.audio_url;
  const b64 = x.audio_b64 ?? x.audio_base64 ?? x.audio;
  if (typeof b64 === "string" && b64.length > 100) return b64.startsWith("data:") ? b64 : `data:audio/mpeg;base64,${b64}`;
  return undefined;
}

export function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}
export function fmtUsd(n: number, digits = 0): string {
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (n < 0 ? "-$" : "$") + s;
}

export function backtestDay(d: any): BacktestDay {
  return {
    date: String(d.date ?? d.day),
    baseline: pick(d, ["baseline_profit", "baseline_profit_usd", "baseline_usd", "baseline"]) ?? 0,
    optimal: pick(d, ["optimal_profit", "optimal_profit_usd", "profit_usd", "optimal_usd", "optimal", "profit"]) ?? 0,
  };
}

export async function getBacktest(): Promise<Backtest | null> {
  let j: any;
  if (MOCK) j = mockBacktest;
  else {
    try {
      const r = await fetch(`${API}/api/backtest?days=30`);
      if (!r.ok) return null;
      j = await r.json();
    } catch {
      return null;
    }
  }
  const days: any[] = Array.isArray(j) ? j : j.days ?? j.results ?? j.per_day ?? [];
  const norm = days.map(backtestDay);
  const t = j.totals ?? j.total ?? {};
  return {
    days: norm,
    total: pick(t, ["optimal_profit", "optimal_profit_usd", "profit_usd", "optimal"]) ?? norm.reduce((a, d) => a + d.optimal, 0),
    total_baseline: pick(t, ["baseline_profit", "baseline_profit_usd", "baseline"]) ?? norm.reduce((a, d) => a + d.baseline, 0),
  };
}

/** Pull "night N/30 in sandbox X" progress out of any stream event. */
export function progressFrom(e: any): Progress | null {
  const n = num(e.n ?? e.night ?? e.i ?? e.done ?? e.day_index);
  const of = num(e.of ?? e.total ?? e.days ?? e.count);
  if (/backtest|progress/.test(String(e.kind)) && n !== undefined && of !== undefined)
    return { n, of, sandbox: e.sandbox_id ?? e.sandbox, text: e.text };
  const t = typeof e.text === "string" ? e.text : "";
  const m = t.match(/night\s+(\d+)\s*(?:\/|of)\s*(\d+)/i);
  if (m) return { n: +m[1], of: +m[2], sandbox: t.match(/sandbox\s+([\w-]+)/i)?.[1], text: t };
  return null;
}

let mockScale = 1;
export function setMockScale(powerMw: number) {
  mockScale = powerMw / 10;
}
function scaleMock(e: any): any {
  if (e.kind !== "plan" || mockScale === 1) return e;
  const k = mockScale;
  return {
    ...e,
    schedule: e.schedule.map((r: any) => ({ ...r, charge_mw: r.charge_mw * k, discharge_mw: r.discharge_mw * k })),
    profit_usd: e.profit_usd * k,
    revenue_usd: e.revenue_usd * k,
    cost_usd: e.cost_usd * k,
    baseline_profit_usd: e.baseline_profit_usd * k,
    backtest_total_usd: e.backtest_total_usd * k,
  };
}

async function readSSE(r: Response, onEvent: (e: StreamEvent) => void) {
  if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.search(/\r?\n\r?\n/)) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx).replace(/^\r?\n\r?\n/, "");
      const data = chunk
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (!data) continue;
      try {
        onEvent(JSON.parse(data));
      } catch {
        onEvent({ kind: "status", text: data });
      }
    }
  }
}

/** GET an SSE endpoint (e.g. /api/backtest/stream). */
export async function streamGet(path: string, onEvent: (e: StreamEvent) => void, signal?: AbortSignal): Promise<void> {
  if (MOCK) {
    for (let i = 1; i <= 30; i++) {
      await sleep(70);
      if (signal?.aborted) return;
      onEvent({ kind: "status", text: `night ${i}/30 simulated in sandbox sbx-${(4096 + i * 77).toString(16)}`, i, n: 30, sandbox_id: `sbx-${(4096 + i * 77).toString(16)}` });
    }
    onEvent({ kind: "backtest", ...(mockBacktest as any) });
    onEvent({ kind: "done" });
    return;
  }
  await readSSE(await fetch(`${API}${path}`, { headers: { Accept: "text/event-stream" }, signal }), onEvent);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** POST /api/plan and stream SSE events. */
export async function streamPlan(onEvent: (e: StreamEvent) => void, signal?: AbortSignal, fast = false): Promise<void> {
  if (MOCK) {
    for (const e of mockPlanEvents) {
      await sleep(e.kind === "status" ? 260 : e.kind === "backtest" ? 60 : 150);
      if (signal?.aborted) return;
      onEvent(scaleMock(e) as StreamEvent);
    }
    return;
  }
  const r = await fetch(`${API}/api/plan${fast ? "?fast=1" : ""}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(fast ? { fast: true } : {}),
    signal,
  });
  await readSSE(r, onEvent);
}

export async function approve(planId: string | undefined, approved: boolean): Promise<any> {
  if (MOCK) {
    await sleep(400);
    return { ok: true, plan_id: planId, approved };
  }
  const r = await fetch(`${API}/api/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: planId, approved }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json().catch(() => ({}));
}

export async function getJSON(path: string): Promise<any> {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text().catch(() => "")}`);
  return r.json();
}
export async function postJSON(path: string, body: unknown): Promise<any> {
  const r = await fetch(`${API}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text().catch(() => "")}`);
  return r.json();
}

export type Fact = {
  id: string;
  kind: string;
  key: string;
  value: any;
  source?: string;
  valid_from?: string;
  valid_until?: string | null;
  superseded_by?: string | null;
  text?: string;
};
export type Memory = { backend?: string; facts: Fact[] };

export async function getMemory(): Promise<Memory | null> {
  if (MOCK) return mockMemory as Memory;
  try {
    const j = await getJSON("/api/memory?limit=30");
    return { backend: j.status?.backend, facts: Array.isArray(j.facts) ? j.facts : [] };
  } catch {
    return null;
  }
}
