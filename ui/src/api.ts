// API client + normalizers. Shapes follow CONTRACTS.md; every field is read defensively so
// small backend differences do not blank the page.
import { mockBacktest, mockPlanEvents, mockPrices } from "./mock";

export const API: string = (import.meta.env.VITE_API as string | undefined) ?? "http://localhost:8520";
export const MOCK = new URLSearchParams(window.location.search).get("mock") === "1";

export type PricePoint = { ts: string; usd_mwh: number };
export type HourLoad = { hour: number; kw: number };
export type Check = { name: string; ok: boolean; detail?: string };

export type Plan = {
  plan_id?: string;
  baseline: HourLoad[];
  optimal: HourLoad[];
  prices: PricePoint[];
  tonight_usd: number;
  monthly_usd: number;
  pct?: number;
  baseline_cost?: number;
  optimal_cost?: number;
  min_soc?: number;
  peak_kw?: number;
  baseline_peak_kw?: number;
  cap_kw: number;
  violations: number;
  sandbox?: string; // Daytona sandbox id, or undefined = local
  checks: Check[];
  audio_url?: string;
  voice_text?: string;
};

export type BacktestDay = { date: string; baseline_usd: number; optimal_usd: number; saved_usd: number };
export type Backtest = { days: BacktestDay[]; total_saved: number; total_baseline?: number };

export type StreamEvent =
  | { kind: "status"; text: string }
  | { kind: "plan"; [k: string]: unknown }
  | { kind: "done" }
  | { kind: "error"; text?: string };

/* eslint-disable @typescript-eslint/no-explicit-any */
const num = (v: any): number | undefined => (typeof v === "number" && isFinite(v) ? v : typeof v === "string" && v !== "" && isFinite(+v) ? +v : undefined);

function hourOf(x: any): number {
  if (typeof x === "number") return ((x % 24) + 24) % 24;
  const s = String(x);
  const m = s.match(/[T ](\d{2}):/);
  if (m) return +m[1];
  if (/^\d{1,2}$/.test(s)) return +s;
  return new Date(s).getHours();
}

function loads(v: any): HourLoad[] {
  if (!v) return [];
  const arr = Array.isArray(v) ? v : v.schedule ?? v.load ?? v.hourly ?? v.kw ?? [];
  if (!Array.isArray(arr)) return [];
  return arr.map((p: any, i: number) =>
    typeof p === "number" ? { hour: i, kw: p } : { hour: hourOf(p.hour ?? p.ts ?? p.time ?? i), kw: num(p.kw ?? p.total_kw ?? p.load_kw) ?? 0 },
  );
}

export function normalizePlan(ev: any, prices: PricePoint[]): Plan {
  const b = ev.baseline ?? {};
  const o = ev.optimal ?? {};
  const s = ev.savings ?? {};
  const c = ev.checks ?? {};
  let optimal = loads(ev.schedule);
  if (!optimal.length) optimal = loads(o);
  const baseline = loads(b);
  const cap = num(ev.cap_kw ?? c.cap_kw ?? o.cap_kw) ?? 3000;
  const minSoc = num(c.min_soc ?? c.min_soc_at_departure ?? o.min_soc_at_departure ?? o.sim?.min_soc_at_departure);
  const peak = num(c.peak_kw ?? o.peak_kw ?? o.sim?.peak_kw) ?? (optimal.length ? Math.max(...optimal.map((x) => x.kw)) : undefined);
  const violList = c.violations ?? o.violations ?? o.sim?.violations;
  const violations = Array.isArray(violList) ? violList.length : num(violList) ?? 0;
  const sandbox: string | undefined =
    c.sandbox_id ?? c.sandbox ?? ev.sandbox_id ?? ev.sandbox?.id ?? (typeof ev.sandbox === "string" ? ev.sandbox : undefined);
  const sandboxIsLocal = !sandbox || /^local/i.test(String(sandbox)) || c.where === "local" || ev.sandbox?.kind === "local";

  // Checks: use backend list if given, else derive.
  let checks: Check[] = [];
  if (Array.isArray(c)) {
    checks = c.map((x: any) => ({ name: String(x.name ?? x.label ?? x.check), ok: !!(x.ok ?? x.pass ?? x.passed), detail: x.detail ?? x.value }));
  } else {
    const soc = minSoc !== undefined ? (minSoc <= 1.0001 ? minSoc * 100 : minSoc) : undefined;
    checks = [
      { name: "Every bus at 90% or more by 5:00am", ok: c.all_buses_charged ?? (soc === undefined ? true : soc >= 90 - 1e-6), detail: soc !== undefined ? `min SOC ${soc.toFixed(1)}%` : undefined },
      { name: "Site peak within cap", ok: c.peak_ok ?? (peak === undefined ? true : peak <= cap + 1e-6), detail: peak !== undefined ? `${fmtInt(peak)} / ${fmtInt(cap)} kW` : undefined },
      { name: sandboxIsLocal ? "Simulated locally" : "Simulated in Daytona sandbox", ok: true, detail: sandboxIsLocal ? "Daytona unavailable, local subprocess" : String(sandbox) },
      { name: "Constraint violations", ok: violations === 0, detail: String(violations) },
    ];
  }

  const evPrices: PricePoint[] = Array.isArray(ev.prices) ? ev.prices : Array.isArray(ev.prices?.day_ahead) ? ev.prices.day_ahead : prices;

  return {
    plan_id: ev.plan_id ?? ev.id,
    baseline,
    optimal,
    prices: evPrices,
    tonight_usd: num(s.tonight_usd) ?? 0,
    monthly_usd: num(s.monthly_usd) ?? 0,
    pct: num(s.pct),
    baseline_cost: num(b.cost_usd ?? b.sim?.cost_usd),
    optimal_cost: num(o.cost_usd ?? o.sim?.cost_usd),
    min_soc: minSoc,
    peak_kw: peak,
    baseline_peak_kw: num(b.peak_kw) ?? (baseline.length ? Math.max(...baseline.map((x) => x.kw)) : undefined),
    cap_kw: cap,
    violations,
    sandbox: sandboxIsLocal ? undefined : String(sandbox),
    checks,
    audio_url: audioFrom(ev),
    voice_text: ev.voice_text ?? ev.script ?? ev.explanation,
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
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export async function getPrices(): Promise<PricePoint[]> {
  if (MOCK) return mockPrices;
  try {
    const r = await fetch(`${API}/api/prices`);
    const j = await r.json();
    return Array.isArray(j) ? j : j.day_ahead ?? j.dam ?? j.prices ?? [];
  } catch {
    return [];
  }
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
  const norm = days.map((d) => ({
    date: String(d.date),
    baseline_usd: num(d.baseline_usd) ?? 0,
    optimal_usd: num(d.optimal_usd) ?? 0,
    saved_usd: num(d.saved_usd) ?? (num(d.baseline_usd) ?? 0) - (num(d.optimal_usd) ?? 0),
  }));
  const t = j.totals ?? j.total ?? {};
  return {
    days: norm,
    total_saved: num(t.saved_usd ?? t) ?? norm.reduce((a, d) => a + d.saved_usd, 0),
    total_baseline: num(t.baseline_usd),
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** POST /api/plan and stream SSE events. */
export async function streamPlan(onEvent: (e: StreamEvent) => void, signal?: AbortSignal): Promise<void> {
  if (MOCK) {
    for (const e of mockPlanEvents) {
      await sleep(e.kind === "status" ? 280 : 150);
      if (signal?.aborted) return;
      onEvent(e as StreamEvent);
    }
    return;
  }
  const r = await fetch(`${API}/api/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ cap_kw: 3000 }),
    signal,
  });
  if (!r.ok || !r.body) throw new Error(`plan request failed: HTTP ${r.status}`);
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
