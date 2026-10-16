// Realistic mock data shaped like the CONTRACTS.md API. Used with ?mock=1.
// Prices: typical October NYISO N.Y.C. day-ahead shape (evening peak, overnight trough).

const DATE = "2026-10-16";
const NEXT = "2026-10-17";
const DA: number[] = [
  // 00..23 $/MWh
  27.1, 24.8, 22.6, 21.9, 22.4, 25.3, 31.8, 42.5, 48.9, 46.2, 43.7, 41.9,
  40.8, 41.5, 44.0, 49.6, 58.3, 74.9, 88.4, 92.7, 81.2, 63.5, 47.8, 35.6,
];

export const mockPrices = [
  ...DA.slice(12).map((p, i) => ({ ts: `${DATE}T${String(i + 12).padStart(2, "0")}:00:00`, usd_mwh: p })),
  ...DA.slice(0, 12).map((p, i) => ({ ts: `${NEXT}T${String(i).padStart(2, "0")}:00:00`, usd_mwh: p })),
];

// Baseline: 40 buses x 150 kW plug in at 19:00, uncapped, until full.
const baselineSchedule = [
  { hour: 19, kw: 6000 },
  { hour: 20, kw: 5640 },
  { hour: 21, kw: 2160 },
];
// Optimal: >= 90% by 05:00, <= 3,000 kW, in the overnight trough.
const optimalSchedule = [
  { hour: 22, kw: 0 },
  { hour: 23, kw: 420 },
  { hour: 0, kw: 1680 },
  { hour: 1, kw: 2760 },
  { hour: 2, kw: 2940 },
  { hour: 3, kw: 2940 },
  { hour: 4, kw: 1820 },
];

export const mockPlanEvents = [
  { kind: "status", text: "Fetching NYISO day-ahead prices for zone N.Y.C. (2026-10-17)" },
  { kind: "status", text: "Recalling depot constraints from SurrealDB memory: bus 12 departs 04:00" },
  { kind: "status", text: "Depot state: 40 buses, arriving 19:00 at 21% to 39% SOC" },
  { kind: "status", text: "Building baseline: plug in at 7pm, full power" },
  { kind: "status", text: "Solving LP (HiGHS): 40 buses x 10 hours, site cap 3,000 kW" },
  { kind: "status", text: "Simulating plan in Daytona sandbox sbx-7f3c2a91" },
  { kind: "status", text: "Simulation passed: 0 violations, min SOC 90.4%, peak 2,940 kW" },
  { kind: "status", text: "Pricing savings against real NYISO LBMP" },
  {
    kind: "plan",
    plan_id: "plan-2026-10-16-n1",
    baseline: { schedule: baselineSchedule, cost_usd: 1648.22, peak_kw: 6000 },
    optimal: { schedule: optimalSchedule, cost_usd: 834.77, peak_kw: 2940, min_soc_at_departure: 0.904, violations: [] },
    schedule: optimalSchedule,
    savings: { tonight_usd: 813.45, monthly_usd: 114403.5, pct: 49.4 },
    checks: { min_soc: 0.904, peak_kw: 2940, cap_kw: 3000, violations: 0, sandbox_id: "sbx-7f3c2a91" },
    voice_text: "Tonight's plan: hold charging until 11pm, then charge through 4am under the 3,000 kilowatt cap. Every bus leaves at 90 percent or better. Saves 813 dollars versus plugging in at 7. Approve?",
  },
  { kind: "done" },
];

function seeded(i: number): number {
  const x = Math.sin(i * 9301 + 49297) * 233280;
  return x - Math.floor(x);
}

const days = Array.from({ length: 30 }, (_, i) => {
  const d = new Date(Date.UTC(2026, 8, 16 + i));
  const base = 1350 + seeded(i) * 700 + (d.getUTCDay() % 6 === 0 ? -120 : 0);
  const saved = base * (0.36 + seeded(i + 50) * 0.2);
  return {
    date: d.toISOString().slice(0, 10),
    baseline_usd: +base.toFixed(2),
    optimal_usd: +(base - saved).toFixed(2),
    saved_usd: +saved.toFixed(2),
  };
});

export const mockBacktest = {
  days,
  totals: {
    baseline_usd: +days.reduce((a, d) => a + d.baseline_usd, 0).toFixed(2),
    optimal_usd: +days.reduce((a, d) => a + d.optimal_usd, 0).toFixed(2),
    saved_usd: +days.reduce((a, d) => a + d.saved_usd, 0).toFixed(2),
  },
};
