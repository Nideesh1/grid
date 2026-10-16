// Realistic mock data shaped like the CONTRACTS.md PIVOT API (battery arbitrage). Used with ?mock=1.
// Prices: typical October NYISO N.Y.C. day-ahead shape (morning bump, evening peak, overnight trough).

const DATE = "2026-10-17";
const DA: number[] = [
  27.1, 24.8, 22.6, 21.9, 22.4, 25.3, 31.8, 42.5, 48.9, 46.2, 43.7, 41.9,
  40.8, 41.5, 44.0, 49.6, 58.3, 74.9, 88.4, 92.7, 81.2, 63.5, 47.8, 35.6,
];

// 10 MW / 40 MWh, eta = sqrt(0.85) each way, SOC 10..90%, start/end 50%.
const ETA = Math.sqrt(0.85);
const CAP = 40;
function build(charge: Record<number, number>, discharge: Record<number, number>) {
  let e = CAP * 0.5;
  let revenue = 0;
  let cost = 0;
  let out = 0;
  const schedule = DA.map((price, hour) => {
    const c = charge[hour] ?? 0;
    const d = discharge[hour] ?? 0;
    e += c * ETA - d / ETA;
    revenue += d * price;
    cost += c * price;
    out += d;
    return { hour, price, charge_mw: c, discharge_mw: d, soc_pct: +((e / CAP) * 100).toFixed(1) };
  });
  const profit = revenue - cost - 8 * out;
  return { schedule, revenue_usd: +revenue.toFixed(2), cost_usd: +cost.toFixed(2), profit_usd: +profit.toFixed(2) };
}

// Optimal: fill to 90% in the 2-3am trough, sell into the 5-8pm peak down to 10%, refill to 50% late evening.
const optimal = build({ 2: 10, 3: 7.35, 22: 7.35, 23: 10 }, { 17: 9.5, 18: 10, 19: 10 });
// Naive rule: buy 01:00-05:00, sell 17:00-21:00, same energy back out.
const baseline = build({ 1: 4, 2: 4, 3: 4, 4: 4 }, { 17: 3.4, 18: 3.4, 19: 3.4, 20: 3.4 });

const socs = optimal.schedule.map((r) => r.soc_pct);
const discharged = optimal.schedule.reduce((a, r) => a + r.discharge_mw, 0);

function seeded(i: number): number {
  const x = Math.sin(i * 9301 + 49297) * 233280;
  return x - Math.floor(x);
}
const days = Array.from({ length: 30 }, (_, i) => {
  const d = new Date(Date.UTC(2026, 8, 16 + i));
  const opt = 700 + seeded(i) * 1100 + (seeded(i + 7) > 0.85 ? 1800 : 0);
  const base = opt * (0.35 + seeded(i + 50) * 0.35) - (seeded(i + 3) > 0.8 ? 400 : 0);
  return { date: d.toISOString().slice(0, 10), baseline_profit: +base.toFixed(2), optimal_profit: +opt.toFixed(2) };
});

export const mockBacktest = {
  days,
  totals: {
    baseline_profit: +days.reduce((a, d) => a + d.baseline_profit, 0).toFixed(2),
    optimal_profit: +days.reduce((a, d) => a + d.optimal_profit, 0).toFixed(2),
  },
};

const SBX = "sbx-7f3c2a91";
export const mockPlanEvents: Record<string, unknown>[] = [
  { kind: "status", text: `Fetching NYISO day-ahead prices for zone N.Y.C. (${DATE})` },
  { kind: "status", text: "Recalling operator constraints from SurrealDB memory: keep 10% reserve" },
  { kind: "status", text: "Battery: 10 MW / 40 MWh, 85% round trip, starts 50% full" },
  { kind: "status", text: "Solving LP (HiGHS): 24 hours, 1 cycle limit, $8/MWh degradation" },
  { kind: "status", text: `Simulating plan in Daytona sandbox ${SBX}` },
  ...[5, 10, 15, 20, 25, 30].map((n) => ({ kind: "backtest", n, of: 30, sandbox_id: SBX, text: `Simulating night ${n}/30 in sandbox ${SBX}` })),
  { kind: "status", text: "Simulation passed: 0 violations, 1.00 cycle, ends 50% full" },
  {
    kind: "plan",
    plan_id: "arb-2026-10-17-a1",
    date: DATE,
    schedule: optimal.schedule,
    profit_usd: optimal.profit_usd,
    revenue_usd: optimal.revenue_usd,
    cost_usd: optimal.cost_usd,
    baseline_profit_usd: baseline.profit_usd,
    backtest_total_usd: mockBacktest.totals.optimal_profit,
    optimal: { min_soc: Math.min(...socs) / 100, max_soc: Math.max(...socs) / 100, end_soc: socs[23] / 100, cycles: +(discharged / ETA / (CAP * 0.8)).toFixed(2), violations: [] },
    sandbox_id: SBX,
    simulated_in: "daytona",
    voice_text: `Tomorrow: buy at 2 and 3am, sell from 5 to 8pm, refill late evening. Profit about ${Math.round(optimal.profit_usd)} dollars, one cycle, ends half full. Approve?`,
  },
  { kind: "done" },
];

export const mockMemory = {
  backend: "surrealdb",
  facts: [
    { id: "f3", kind: "constraint", key: "reserve", value: { reserve_pct: 30, reason: "storm watch" }, source: "voice:operator", valid_from: "2026-10-16T17:02:10+00:00", valid_until: null, superseded_by: null },
    { id: "f2", kind: "constraint", key: "reserve", value: { reserve_pct: 10 }, source: "battery:default", valid_from: "2026-10-15T23:00:00+00:00", valid_until: "2026-10-16T17:02:10+00:00", superseded_by: "f3" },
    { id: "f5", kind: "price_forecast", key: "2026-10-17 19:00", value: { usd_mwh: 92.7 }, source: "nyiso:dam", valid_from: "2026-10-16T15:30:00+00:00", valid_until: null, superseded_by: null },
    { id: "f4", kind: "price_forecast", key: "2026-10-17 19:00", value: { usd_mwh: 84.1 }, source: "nyiso:dam", valid_from: "2026-10-15T15:30:00+00:00", valid_until: "2026-10-16T15:30:00+00:00", superseded_by: "f5" },
  ],
};
