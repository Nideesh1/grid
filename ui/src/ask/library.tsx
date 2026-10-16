/* OpenUI component library for "Ask the trader". The API streams OpenUI Lang statements against these; <Renderer> draws them.
   Positional argument order = zod object key order. */
import { createLibrary, defineComponent } from "@openuidev/react-lang";
import { useMemo, type ReactNode } from "react";
import { Bar, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { z } from "zod";
import { pageComponents } from "./components";

const GREEN = "#38f28a";
const GOLD = "#e8b64a";
const RED = "#ff5b3d";
const MUTED = "#6f6c66";
const TIP = { background: "#0d0d0c", border: "1px solid #2a2926", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 12 };

const arr = <T,>(v: T[] | undefined | null): T[] => (Array.isArray(v) ? v.filter((x) => x != null) : []);
const num = (v: unknown): v is number => typeof v === "number" && isFinite(v);
const usd = (v: number | null | undefined) =>
  !num(v) ? "-" : `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 })}`;
const hourLabel = (h: number) => (h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`);

const Card = ({ tag, right, children, className = "" }: { tag?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) => (
  <section className={"ask-card " + className}>
    {(tag || right) && (
      <div className="ask-tag"><span>{tag}</span><i />{right != null && <span>{right}</span>}</div>
    )}
    {children}
  </section>
);

const Stack = defineComponent({
  name: "Stack",
  description: "Root vertical container; children render top to bottom. Always the root.",
  props: z.object({ children: z.array(z.any()) }),
  component: ({ props, renderNode }) => <div className="ask-stack">{renderNode(props.children)}</div>,
});

const ProfitHeadline = defineComponent({
  name: "ProfitHeadline",
  description: "The one dollar figure that answers the question, shown huge (USD), a short label and a one-sentence answer. Always first, exactly once.",
  props: z.object({
    value: z.number(),
    label: z.string().describe("short label, e.g. 'profit tomorrow with a 30% reserve'"),
    summary: z.string().describe("one sentence answer to the question"),
  }),
  component: ({ props }) => (
    <Card className="ask-headline" tag={props.label}>
      <div className={"ask-big " + (num(props.value) && props.value < 0 ? "neg" : "")}>{usd(props.value)}</div>
      {props.summary && <p className="ask-sum">{props.summary}</p>}
    </Card>
  ),
});

const HourRow = z.object({
  hour: z.number(),
  price: z.number().nullable().optional(),
  charge_mw: z.number(),
  discharge_mw: z.number(),
  soc_pct: z.number().nullable().optional(),
});

const BuySellChart = defineComponent({
  name: "BuySellChart",
  description: "Hourly plan: price line ($/MWh), green buy bars (charging MW, below zero), gold sell bars (discharging MW, above zero), SOC % line. Pass HOURS (placeholder) as hours.",
  props: z.object({ title: z.string(), hours: z.array(HourRow) }),
  component: ({ props }) => {
    const data = useMemo(
      () => arr(props.hours).sort((a, b) => a.hour - b.hour).map((h) => ({ ...h, label: hourLabel(h.hour), buy: -(h.charge_mw || 0), sell: h.discharge_mw || 0 })),
      [props.hours],
    );
    const ps = data.map((d) => d.price).filter(num);
    const mw = Math.max(0.1, ...data.map((d) => Math.max(-d.buy, d.sell)));
    const dom = Math.ceil(mw * 1.1 * 10) / 10;
    const hi = ps.length ? Math.max(...ps) : 0;
    return (
      <Card className="wide" tag="buy and sell by hour" right={<span className="ask-legend"><b style={{ color: GREEN }}>buy</b> <b style={{ color: GOLD }}>sell</b> <b style={{ color: RED }}>price</b> <b style={{ color: "#e8e6e1" }}>SOC</b></span>}>
        <h3>{props.title}</h3>
        <ResponsiveContainer width="100%" height={260}>
          <ComposedChart data={data} margin={{ top: 12, right: 4, bottom: 0, left: -8 }}>
            <CartesianGrid stroke="#1c1b19" vertical={false} />
            <XAxis dataKey="label" stroke={MUTED} tickLine={false} tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }} interval={2} />
            <YAxis yAxisId="mw" stroke={MUTED} tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }} width={44} domain={[-dom, dom]} ticks={[-dom, -dom / 2, 0, dom / 2, dom]} tickFormatter={(v) => `${+(+v).toFixed(1)}MW`} />
            <YAxis yAxisId="p" orientation="right" stroke={MUTED} tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }} width={44} tickFormatter={(v) => `$${v}`} />
            <YAxis yAxisId="soc" hide domain={[0, 100]} />
            <Tooltip contentStyle={TIP} formatter={(v: number, k: string) => (k === "price" ? [`$${v}/MWh`, "price"] : k === "soc_pct" ? [`${v}%`, "SOC"] : [`${Math.abs(v)} MW`, k])} />
            <ReferenceLine yAxisId="mw" y={0} stroke="#3a3935" />
            <Bar yAxisId="mw" dataKey="buy" fill={GREEN} fillOpacity={0.85} isAnimationActive={false} />
            <Bar yAxisId="mw" dataKey="sell" fill={GOLD} fillOpacity={0.9} isAnimationActive={false}>
              {data.map((_, i) => <Cell key={i} />)}
            </Bar>
            <Line yAxisId="p" dataKey="price" stroke={RED} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            <Line yAxisId="soc" dataKey="soc_pct" stroke="#e8e6e1" strokeDasharray="3 3" strokeWidth={1} dot={false} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
        {hi > 0 && <div className="ask-fine">peak ${hi.toFixed(2)}/MWh. bars: MW per hour, buy below the axis, sell above.</div>}
      </Card>
    );
  },
});

const SafetyChecks = defineComponent({
  name: "SafetyChecks",
  description: "Pass/fail safety checks from the plan's simulation. Pass CHECKS (placeholder) as items.",
  props: z.object({ items: z.array(z.object({ name: z.string(), ok: z.boolean(), detail: z.string().nullable().optional() })) }),
  component: ({ props }) => {
    const items = arr(props.items);
    const bad = items.filter((i) => !i.ok).length;
    return (
      <Card tag="safety checks" right={<span className={bad ? "red" : "ask-ok"}>{bad ? `${bad} failed` : "all passed"}</span>}>
        <ul className="ask-checks">
          {items.map((c, i) => (
            <li key={i} className={c.ok ? "" : "bad"} title={c.detail ?? undefined}><span className="mk">{c.ok ? "✓" : "×"}</span><b>{c.name}</b>{!c.ok && c.detail && <span className="dim">{c.detail}</span>}</li>
          ))}
        </ul>
      </Card>
    );
  },
});

const UNITS = ["usd", "pct", "mwh", "usd_mwh", "cycles", "hour", "text"] as const;
const fmt = (v: number | string | null | undefined, u?: string | null) => {
  if (typeof v === "string") return v;
  if (!num(v)) return "-";
  return u === "usd" ? usd(v) : u === "pct" ? `${v}%` : u === "mwh" ? `${v} MWh` : u === "usd_mwh" ? `$${v}/MWh` : u === "cycles" ? `${v} cycles` : u === "hour" ? hourLabel(v) : String(v);
};

const Compare = defineComponent({
  name: "Compare",
  description: "Labeled figures side by side, e.g. default plan vs what-if, or simple rule vs optimal. unit: usd | pct | mwh | usd_mwh | cycles | hour | text.",
  props: z.object({
    title: z.string(),
    rows: z.array(z.object({ label: z.string(), value: z.union([z.number(), z.string()]), unit: z.enum(UNITS).nullable().optional() })),
  }),
  component: ({ props }) => (
    <Card tag="compare">
      <h3>{props.title}</h3>
      <div className="ask-rows">
        {arr(props.rows).map((r, i) => (
          <div key={i} className="ask-row"><span>{r.label}</span><b className="mono">{fmt(r.value, r.unit)}</b></div>
        ))}
      </div>
    </Card>
  ),
});

const BacktestBars = defineComponent({
  name: "BacktestBars",
  description: "Per-day realized profit (USD) of the optimal plan, with the simple rule as a muted bar. Pass DAYS (placeholder) as days.",
  props: z.object({
    title: z.string(),
    days: z.array(z.object({ date: z.string(), profit: z.number(), baseline: z.number().nullable().optional() })),
  }),
  component: ({ props }) => {
    const data = arr(props.days).map((d) => ({ ...d, label: d.date.slice(5) }));
    const tot = data.reduce((s, d) => s + (d.profit || 0), 0);
    const totB = data.reduce((s, d) => s + (d.baseline || 0), 0);
    return (
      <Card className="wide" tag="backtest, settled on real-time prices" right={<span className="mono">{usd(tot)} vs {usd(totB)} simple rule</span>}>
        <h3>{props.title}</h3>
        <ResponsiveContainer width="100%" height={200}>
          <ComposedChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: -4 }}>
            <CartesianGrid stroke="#1c1b19" vertical={false} />
            <XAxis dataKey="label" stroke={MUTED} tickLine={false} tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }} />
            <YAxis stroke={MUTED} tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }} width={52} tickFormatter={(v) => `$${v}`} />
            <Tooltip contentStyle={TIP} formatter={(v: number, k: string) => [usd(v), k === "profit" ? "optimal" : "simple rule"]} />
            <ReferenceLine y={0} stroke="#3a3935" />
            <Bar dataKey="baseline" fill={MUTED} fillOpacity={0.6} isAnimationActive={false} />
            <Bar dataKey="profit" isAnimationActive={false}>
              {data.map((d, i) => <Cell key={i} fill={d.profit < 0 ? RED : GREEN} />)}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </Card>
    );
  },
});

const Narrative = defineComponent({
  name: "Narrative",
  description: "One or two sentences of plain explanation (the why). Use once.",
  props: z.object({ text: z.string() }),
  component: ({ props }) => <p className="ask-narrative">{props.text}</p>,
});

const Evidence = defineComponent({
  name: "Evidence",
  description: "Provenance footer: data source, date and where the plan was simulated. Always last, exactly once.",
  props: z.object({ text: z.string() }),
  component: ({ props }) => <div className="ask-evidence">{props.text}</div>,
});

const GridAlerts = defineComponent({
  name: "GridAlerts",
  description:
    "NYISO grid operating status and active alerts. status: normal | alert | emergency. alerts: [{type, zone, text, since, simulated}] (simulated true marks a demo alert). Read live from NYISO in a cloud browser (Anchor).",
  props: z.object({
    status: z.string(),
    alerts: z
      .array(z.object({ type: z.string().nullable().optional(), zone: z.string().nullable().optional(), text: z.string(), since: z.string().nullable().optional(), simulated: z.boolean().nullable().optional() }))
      .nullable()
      .optional(),
  }),
  component: ({ props }) => {
    const st = String(props.status || "normal").toLowerCase();
    const level = /emerg/.test(st) ? "emergency" : /alert|warn|watch/.test(st) ? "alert" : "normal";
    const color = level === "emergency" ? RED : level === "alert" ? GOLD : GREEN;
    const items = arr(props.alerts);
    return (
      <Card className="wide" tag="grid alerts">
        <div className="ga-status">
          <i style={{ background: color, boxShadow: `0 0 10px ${color}` }} />
          <span style={{ color }}>{level === "normal" ? "Normal operations" : level === "alert" ? "Alert" : "Emergency"}</span>
          {!items.length && <span className="dim">no active alerts</span>}
        </div>
        {items.length > 0 && (
          <ul className="ga-list">
            {items.map((a, i) => (
              <li key={i}>
                <span className="mono ga-type">{[a.type, a.zone].filter(Boolean).join(" · ")}</span>
                <span>
                  {a.text}
                  {a.simulated && <em className="ga-sim">simulated for demo</em>}
                </span>
                <span className="mono ga-since">{a.since ?? ""}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="ask-fine">read live from NYISO in a cloud browser (Anchor)</div>
      </Card>
    );
  },
});

export const library = createLibrary({
  components: [Stack, ProfitHeadline, BuySellChart, Compare, BacktestBars, SafetyChecks, Narrative, Evidence, GridAlerts, ...pageComponents],
  root: "Stack",
});

export const askPrompt = () =>
  library.prompt({
    preamble:
      "You answer questions about a 10 MW / 40 MWh grid battery trading NYISO N.Y.C. prices as generative UI. " +
      "Reply with an OpenUI Lang program only: no prose, no code fences.",
    additionalRules: [
      "Root is always Stack. ProfitHeadline first and exactly once; Evidence last and exactly once.",
      "Every number must come from a tool result. Never invent or extrapolate.",
      "What-if questions: ProfitHeadline (the what-if profit), Compare (default vs what-if profit, reserve, cycles), BuySellChart(title, HOURS), SafetyChecks(CHECKS).",
      "History questions: ProfitHeadline (total optimal profit), BacktestBars(title, DAYS), Compare (optimal vs simple rule totals).",
      "HOURS, DAYS and CHECKS are bare placeholders the server fills in; never type those arrays yourself and never quote them.",
      "Page components: LivePrice() and ZoneMap() read live prices themselves when called with no args. PlayTomorrow(HOURS, profit, battery), HomesPowered(HOURS), BidsTable(BIDS, date, HOURS), ScaleSlider({profit_usd, hours: HOURS, avg_daily_profit}, battery), BacktestCalendar(DAYS, totals), MemoryTimeline(), ApproveCard(plan_id, profit, message).",
      "Show the plan for tomorrow: ProfitHeadline, BuySellChart, PlayTomorrow, SafetyChecks, ApproveCard. Show the grid: LivePrice, ZoneMap. Show my bids: BidsTable, ApproveCard. Last 30 days: ProfitHeadline, BacktestCalendar, BacktestBars, Compare.",
      "No em dashes or en dashes.",
    ],
  });
