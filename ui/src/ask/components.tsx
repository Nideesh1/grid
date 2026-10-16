/* Page visuals exposed as OpenUI components. Every prop is optional where a live fallback exists, and
   loose objects are accepted (extra keys pass through) so small backend shape differences still render.
   Positional argument order = zod object key order. */
import { defineComponent } from "@openuidev/react-lang";
import { useEffect, useState, type ReactNode } from "react";
import { z } from "zod";
import { API, MOCK, backtestDay, fmtInt, fmtUsd, getMemory, normalizePlan, type Backtest, type Fact, type Memory } from "../api";
import { LiveStrip, PlayTomorrow as PlayTomorrowView, ProfitCalendar, SizeSlider, ZoneMap as ZoneMapView, useLive } from "../Landing";
import { BidsTable as BidsTableView } from "../BatteryPanel";
import { MemoryPanel } from "../memory";
import type { Live } from "../live";
import { startVoice } from "../Voice";

/* eslint-disable @typescript-eslint/no-explicit-any */
const arr = <T,>(v: T[] | undefined | null): T[] => (Array.isArray(v) ? v.filter((x) => x != null) : []);
const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);

const Card = ({ tag, right, children, className = "" }: { tag?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) => (
  <section className={"ask-card " + className}>
    {(tag || right) && (
      <div className="ask-tag"><span>{tag}</span><i />{right != null && <span>{right}</span>}</div>
    )}
    {children}
  </section>
);

const HourRow = z.looseObject({
  hour: z.union([z.number(), z.string()]),
  price: z.number().nullable().optional(),
  charge_mw: z.number().nullable().optional(),
  discharge_mw: z.number().nullable().optional(),
  soc_pct: z.number().nullable().optional(),
});
const BatteryProps = z
  .looseObject({
    power_mw: z.number().nullable().optional(),
    energy_mwh: z.number().nullable().optional(),
    reserve_pct: z.number().nullable().optional(),
    zone: z.string().nullable().optional(),
  })
  .nullable()
  .optional();

const planFrom = (hours: unknown, extra: Record<string, unknown> = {}) => normalizePlan({ schedule: arr(hours as any[]), ...extra });

/* ---------------------------------------------------------------- LivePrice */
export const LivePrice = defineComponent({
  name: "LivePrice",
  description:
    "Live NYC power price right now (NYISO real-time 5-min LBMP) with a BUY / HOLD / SELL signal light and today's price sparkline. All args optional; with no args it reads live prices itself. Args: zone, price ($/MWh), signal (BUY|HOLD|SELL), percentile (0-100), ts.",
  props: z.object({
    zone: z.string().nullable().optional(),
    price: z.number().nullable().optional(),
    signal: z.string().nullable().optional(),
    percentile: z.number().nullable().optional(),
    ts: z.string().nullable().optional(),
  }),
  component: ({ props }) => {
    const live = useLive();
    let view: Live | null = live;
    if (live && isNum(props.price)) {
      const z0 = props.zone ?? "N.Y.C.";
      view = { ...live, ts: props.ts ?? live.ts, zones: [...live.zones.filter((z) => z.name !== z0), { name: z0, usd_mwh: props.price }] };
    }
    return (
      <Card tag="right now">
        <LiveStrip live={view} plan={null} />
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- ZoneMap */
export const ZoneMap = defineComponent({
  name: "ZoneMap",
  description:
    "Schematic map of New York's 11 NYISO zones colored by real-time price (cheap cyan to pricey red), battery pin on N.Y.C. Args: zones [{zone, usd_mwh}] (optional; read live if omitted), ts.",
  props: z.object({
    zones: z.array(z.looseObject({ zone: z.string().optional(), name: z.string().optional(), usd_mwh: z.number().nullable().optional(), price: z.number().nullable().optional() })).nullable().optional(),
    ts: z.string().nullable().optional(),
  }),
  component: ({ props }) => {
    const live = useLive();
    const given = arr(props.zones)
      .map((z: any) => ({ name: String(z.zone ?? z.name), usd_mwh: Number(z.usd_mwh ?? z.price) }))
      .filter((z) => isFinite(z.usd_mwh));
    const view: Live | null = given.length ? { ts: props.ts ?? live?.ts ?? "", zones: given, nycToday: live?.nycToday ?? [], source: "nyiso" } : live;
    return (
      <Card>
        <ZoneMapView live={view} />
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- PlayTomorrow */
export const PlayTomorrow = defineComponent({
  name: "PlayTomorrow",
  description:
    "Interactive 24-hour playback of the plan: drag or play through each hour; battery fill animates (green buying, gold selling) and a cash counter ticks spent, earned, wear and running profit, ending at the plan profit. Args: hours (pass HOURS), profit (USD), battery {power_mw, energy_mwh, reserve_pct}.",
  props: z.object({ hours: z.array(HourRow), profit: z.number().nullable().optional(), battery: BatteryProps }),
  component: ({ props }) => {
    const plan = planFrom(props.hours, { profit_usd: props.profit, reserve_pct: props.battery?.reserve_pct });
    if (!plan.rows.length) return <Card tag="play tomorrow"><p className="ask-fine">No hourly plan to play.</p></Card>;
    return (
      <Card className="wide" tag="play tomorrow" right={props.battery?.power_mw ? `${props.battery.power_mw} MW / ${props.battery.energy_mwh} MWh` : undefined}>
        {plan.hold ? <p className="hold-note">HOLD. No profitable hour under your rules.</p> : <PlayTomorrowView plan={plan} showSold={false} />}
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- HomesPowered */
export const HomesPowered = defineComponent({
  name: "HomesPowered",
  description:
    "Who the battery sold to: peak number of NYC homes powered (discharge MW / 1.1 kW average home) and MWh delivered during sell hours. Args: hours (pass HOURS), home_kw (optional, default 1.1).",
  props: z.object({ hours: z.array(HourRow), home_kw: z.number().nullable().optional() }),
  component: ({ props }) => {
    const plan = planFrom(props.hours);
    const kw = isNum(props.home_kw) && props.home_kw > 0 ? props.home_kw : 1.1;
    const sells = plan.rows.filter((r) => r.discharge_mw > 1e-6);
    const peak = Math.max(0, ...sells.map((r) => r.discharge_mw));
    const mwh = sells.reduce((a, r) => a + r.discharge_mw, 0);
    const hl = (h: number) => (h % 12 === 0 ? 12 : h % 12) + (h < 12 ? "am" : "pm");
    return (
      <Card tag="who you sold to">
        <div className="ask-mid mono">~{fmtInt((peak * 1000) / kw)} homes</div>
        <p className="ask-sum">NYC homes powered at full sell power{sells.length ? `, selling at ${sells.map((r) => hl(r.hour)).join(", ")}` : ""}.</p>
        <div className="ask-row"><span>MWh delivered</span><b className="mono">{mwh.toFixed(1)} MWh</b></div>
        <div className="ask-fine">Assumes {kw} kW average NYC home load.</div>
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- BidsTable */
export const BidsTable = defineComponent({
  name: "BidsTable",
  description:
    "Day-ahead bids to submit for tomorrow: hour, side (buy|sell), MW, price limit ($/MWh). Args: bids [{hour, side, mw, price_limit}] (pass BIDS), date, hours (optional HOURS; bids are derived from it if bids is empty).",
  props: z.object({
    bids: z.array(z.looseObject({ hour: z.union([z.number(), z.string()]), side: z.string(), mw: z.number(), price_limit: z.number().nullable().optional() })).nullable().optional(),
    date: z.string().nullable().optional(),
    hours: z.array(HourRow).nullable().optional(),
  }),
  component: ({ props }) => {
    const plan = planFrom(props.hours, { date: props.date });
    return (
      <Card className="wide" tag="your bids for tomorrow">
        <BidsTableView plan={plan} raw={props.bids} />
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- ScaleSlider */
export const ScaleSlider = defineComponent({
  name: "ScaleSlider",
  description:
    "'How big is your battery?' slider from 1 to 100 MW; scales profit tomorrow, homes at the peak and yearly dollars linearly from the plan (labelled linear estimate). Args: plan {profit_usd, hours (pass HOURS), avg_daily_profit}, battery {power_mw, energy_mwh}.",
  props: z.object({
    plan: z.looseObject({ profit_usd: z.number(), hours: z.array(HourRow).nullable().optional(), avg_daily_profit: z.number().nullable().optional() }),
    battery: BatteryProps,
  }),
  component: ({ props }) => {
    const p = props.plan ?? ({} as any);
    const plan = planFrom(p.hours ?? p.schedule, { profit_usd: p.profit_usd });
    const bt: Backtest | null = isNum(p.avg_daily_profit) ? { days: [{ date: "", baseline: 0, optimal: p.avg_daily_profit }], total: p.avg_daily_profit, total_baseline: 0 } : null;
    const battery = { power_mw: props.battery?.power_mw ?? 10, energy_mwh: props.battery?.energy_mwh ?? 40 };
    return (
      <Card tag="scale it">
        <SizeSlider plan={plan} backtest={bt} battery={battery} />
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- BacktestCalendar */
export const BacktestCalendar = defineComponent({
  name: "BacktestCalendar",
  description:
    "Calendar heatmap of daily realized profit over the last 30 real days (planned on day-ahead, settled on real-time), with agent vs simple-rule totals. Args: days [{date, profit, baseline}] (pass DAYS), totals {optimal, baseline} optional.",
  props: z.object({
    days: z.array(z.looseObject({ date: z.string() })),
    totals: z.looseObject({ optimal: z.number().nullable().optional(), baseline: z.number().nullable().optional() }).nullable().optional(),
  }),
  component: ({ props }) => {
    const days = arr(props.days).map((d: any) => {
      const b = backtestDay(d);
      return { ...b, optimal: isNum(d.profit) ? d.profit : b.optimal, baseline: isNum(d.baseline) ? d.baseline : b.baseline };
    });
    const data: Backtest = {
      days,
      total: props.totals?.optimal ?? days.reduce((a, d) => a + d.optimal, 0),
      total_baseline: props.totals?.baseline ?? days.reduce((a, d) => a + d.baseline, 0),
    };
    return (
      <Card className="wide" tag="last 30 real days" right={<span className="mono">{fmtUsd(data.total)} vs {fmtUsd(data.total_baseline)} rule</span>}>
        <ProfitCalendar data={data} />
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- MemoryTimeline */
export const MemoryTimeline = defineComponent({
  name: "MemoryTimeline",
  description:
    "Dispatcher memory (SurrealDB) with valid time: each fact's current value and the value it superseded (struck through), with source and time. Args: facts [{id, kind, key, value, source, valid_from, superseded_by}] (optional; reads /api/memory if omitted).",
  props: z.object({ facts: z.array(z.looseObject({ id: z.string().optional() })).nullable().optional() }),
  component: ({ props }) => {
    const given = arr(props.facts) as unknown as Fact[];
    const [mem, setMem] = useState<Memory | null>(given.length ? { facts: given } : null);
    useEffect(() => {
      if (!given.length) getMemory().then(setMem);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return (
      <Card className="wide" tag="dispatcher memory (SurrealDB)">
        <MemoryPanel data={mem} />
      </Card>
    );
  },
});

/* ---------------------------------------------------------------- ApproveCard */
export const ApproveCard = defineComponent({
  name: "ApproveCard",
  description:
    "Human approval for tomorrow's plan: big Approve button (POST /api/approve), the dispatcher's message, and a Talk to dispatcher voice button. Args: plan_id, profit (USD), message.",
  props: z.object({ plan_id: z.string(), profit: z.number().nullable().optional(), message: z.string().nullable().optional() }),
  component: ({ props }) => {
    const [state, setState] = useState<"idle" | "sending" | "approved" | "error">("idle");
    async function approve() {
      setState("sending");
      try {
        if (!MOCK) {
          const r = await fetch(`${API}/api/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan_id: props.plan_id, approved: true, who: "web" }) });
          if (!r.ok) throw new Error(String(r.status));
        }
        setState("approved");
      } catch {
        setState("error");
      }
    }
    return (
      <Card tag="approve" right={<span className="mono">{props.plan_id}</span>}>
        {props.message && <p className="ask-sum" style={{ marginTop: 0 }}>{props.message}</p>}
        <div className="approve" style={{ marginTop: 14 }}>
          <button className="primary" onClick={approve} disabled={state === "sending" || state === "approved"}>
            {state === "approved" ? "Approved. Bids queued for the day-ahead market" : state === "sending" ? "Sending..." : `Approve tomorrow's trades${isNum(props.profit) ? ` · ${fmtUsd(props.profit)}` : ""}`}
          </button>
          {state === "error" && <p className="fine red">Approval failed to reach the API. Try again.</p>}
          <button className="talk" onClick={startVoice}>Talk it through with the trader</button>
        </div>
      </Card>
    );
  },
});

export const pageComponents = [LivePrice, ZoneMap, PlayTomorrow, HomesPowered, BidsTable, ScaleSlider, BacktestCalendar, MemoryTimeline, ApproveCard];
