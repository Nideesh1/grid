import { useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  Bar,
  BarChart,
  Cell,
  ComposedChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  MOCK,
  approve,
  audioFrom,
  fmtInt,
  fmtUsd,
  getBacktest,
  getPrices,
  normalizePlan,
  streamPlan,
  type Backtest,
  type Plan,
  type PricePoint,
} from "./api";

const GREEN = "#38f28a";
const RED = "#ff5b3d";
const MUTED = "#6f6c66";
const NIGHT = [17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7];

function hourLabel(h: number): string {
  if (h === 0) return "12a";
  if (h === 12) return "12p";
  return h < 12 ? `${h}a` : `${h - 12}p`;
}

function mix(a: string, b: string, t: number): string {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return "#" + pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, "0")).join("");
}

function priceByHour(prices: PricePoint[]): Map<number, number> {
  const m = new Map<number, number[]>();
  for (const p of prices) {
    const s = String(p.ts);
    const mm = s.match(/[T ](\d{2}):/);
    const h = mm ? +mm[1] : new Date(s).getHours();
    if (!m.has(h)) m.set(h, []);
    m.get(h)!.push(p.usd_mwh);
  }
  const out = new Map<number, number>();
  m.forEach((v, h) => out.set(h, v.reduce((a, b) => a + b, 0) / v.length));
  return out;
}

export default function App() {
  const [status, setStatus] = useState<string[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backtest, setBacktest] = useState<Backtest | null>(null);
  const [prices, setPrices] = useState<PricePoint[]>([]);
  const [approval, setApproval] = useState<"idle" | "sending" | "approved" | "error">("idle");
  const [audioUrl, setAudioUrl] = useState<string | undefined>();
  const audioRef = useRef<HTMLAudioElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function runPlan() {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setPlanning(true);
    setPlan(null);
    setStatus([]);
    setError(null);
    setApproval("idle");
    const px = prices.length ? prices : await getPrices();
    try {
      await streamPlan((e) => {
        if (e.kind === "status") setStatus((s) => [...s, e.text]);
        else if (e.kind === "plan") {
          const p = normalizePlan(e, px);
          setPlan(p);
          setAudioUrl(p.audio_url);
        } else if (e.kind === "error") setError(e.text ?? "planner error");
      }, ac.signal);
    } catch (err) {
      if (!ac.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!ac.signal.aborted) setPlanning(false);
    }
  }

  useEffect(() => {
    getPrices().then(setPrices);
    getBacktest().then(setBacktest);
    runPlan();
    return () => abortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onApprove() {
    setApproval("sending");
    try {
      const res = await approve(plan?.plan_id, true);
      const a = audioFrom(res);
      if (a) setAudioUrl(a);
      setApproval("approved");
    } catch {
      setApproval("error");
    }
  }

  function playDispatcher() {
    if (audioUrl && audioRef.current) {
      audioRef.current.play().catch(() => speakFallback());
    } else speakFallback();
  }

  function speakFallback() {
    if (!plan || !("speechSynthesis" in window)) return;
    const text =
      plan.voice_text ??
      `Tonight's plan saves ${Math.round(plan.tonight_usd)} dollars versus plugging in at 7. Every bus leaves above 90 percent. Approve?`;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }

  return (
    <div className="page">
      <header className="masthead">
        <div className="brand">
          <span className="wordmark">GRID</span>
          <span className="dek">NYC bus depot · 40 buses · NYISO N.Y.C. zone</span>
        </div>
        <nav>
          {MOCK && <span className="tag">mock data</span>}
          <a href="/backend">Backend</a>
        </nav>
      </header>

      <StatusFeed lines={status} planning={planning} error={error} onReplan={runPlan} />

      {plan && (
        <main className="result">
          <Hero plan={plan} />
          <Rule label="Tonight, hour by hour" />
          <LoadChart plan={plan} />
          <div className="split">
            <section>
              <Rule label="Safety checks" />
              <Checks plan={plan} />
            </section>
            <section>
              <Rule label="Dispatcher" />
              <div className="approve">
                <button className="primary" onClick={onApprove} disabled={approval === "sending" || approval === "approved"}>
                  {approval === "approved" ? "Approved. Plan sent to depot" : approval === "sending" ? "Sending..." : "Approve tonight's plan"}
                </button>
                <button className="secondary" onClick={playDispatcher}>
                  ▶ Play dispatcher
                </button>
                <p className="fine">
                  {approval === "error"
                    ? "Approval failed to reach the API. Try again."
                    : audioUrl
                      ? "ElevenLabs voice ready."
                      : "Voice not generated yet; playback falls back to the browser voice."}
                  {plan.plan_id && <span className="mono"> · {plan.plan_id}</span>}
                </p>
                {audioUrl && <audio ref={audioRef} src={audioUrl} preload="auto" />}
              </div>
            </section>
          </div>
        </main>
      )}

      <Rule label="Backtest · last 30 days on real NYISO real-time prices" />
      <BacktestStrip data={backtest} />

      <footer className="foot">
        Prices: NYISO public LBMP, zone N.Y.C. Bill model: LBMP + $0.05/kWh delivery, $30/kW monthly demand charge (approximate ConEd SC9).
      </footer>
    </div>
  );
}

function Rule({ label }: { label: string }) {
  return (
    <div className="rule">
      <span>{label}</span>
    </div>
  );
}

function StatusFeed({ lines, planning, error, onReplan }: { lines: string[]; planning: boolean; error: string | null; onReplan: () => void }) {
  return (
    <section className={`status ${planning ? "live" : "settled"}`}>
      <div className="status-head">
        <span className="dot" data-on={planning} />
        <span>{planning ? "Agent planning tonight" : error ? "Planner error" : "Plan ready"}</span>
        {!planning && (
          <button className="link" onClick={onReplan}>
            Re-plan
          </button>
        )}
      </div>
      <ol>
        {lines.map((l, i) => (
          <li key={i} className={i === lines.length - 1 && planning ? "cur" : ""}>
            <span className="mono n">{String(i + 1).padStart(2, "0")}</span>
            {l}
          </li>
        ))}
        {error && <li className="err">{error}</li>}
      </ol>
    </section>
  );
}

function Hero({ plan }: { plan: Plan }) {
  return (
    <section className="hero">
      <div className="big">
        <span className="num">{fmtUsd(plan.tonight_usd)}</span>
        <span className="unit">saved tonight</span>
      </div>
      <div className="second">
        <span className="num">{fmtUsd(plan.monthly_usd)}</span>
        <span className="unit">per month</span>
        {plan.pct !== undefined && <span className="pct">−{plan.pct.toFixed(1)}% on the bill</span>}
      </div>
      <p className="src">
        vs plugging in at 7pm, on real NYISO prices
        {plan.baseline_cost !== undefined && plan.optimal_cost !== undefined && (
          <span className="mono">
            {" "}
            · {fmtUsd(plan.baseline_cost)} → {fmtUsd(plan.optimal_cost)}
          </span>
        )}
      </p>
    </section>
  );
}

function LoadChart({ plan }: { plan: Plan }) {
  const data = useMemo(() => {
    const ph = priceByHour(plan.prices);
    const b = new Map(plan.baseline.map((x) => [x.hour, x.kw]));
    const o = new Map(plan.optimal.map((x) => [x.hour, x.kw]));
    return NIGHT.map((h) => ({ h, label: hourLabel(h), price: ph.get(h) ?? null, baseline: b.get(h) ?? 0, optimal: o.get(h) ?? 0 }));
  }, [plan]);
  const ps = data.map((d) => d.price).filter((x): x is number => x !== null);
  const lo = Math.min(...ps);
  const hi = Math.max(...ps);
  const maxKw = Math.max(plan.cap_kw * 1.15, ...data.map((d) => Math.max(d.baseline, d.optimal))) * 1.05;

  return (
    <section className="chart">
      <div className="legend">
        <span>
          <i style={{ background: `linear-gradient(90deg, ${mix("#24463a", RED, 0)}, ${RED})` }} /> NYISO day-ahead $/MWh
        </span>
        <span>
          <i style={{ background: RED }} /> Plug in at 7pm
        </span>
        <span>
          <i style={{ background: GREEN }} /> Agent plan
        </span>
        <span>
          <i className="dash" /> Site cap {fmtInt(plan.cap_kw)} kW
        </span>
      </div>
      <ResponsiveContainer width="100%" height={380}>
        <ComposedChart data={data} margin={{ top: 16, right: 8, left: 0, bottom: 0 }} barCategoryGap="12%">
          <XAxis dataKey="label" stroke={MUTED} tickLine={false} axisLine={{ stroke: "#2a2926" }} tick={{ fontSize: 12, fontFamily: "JetBrains Mono" }} />
          <YAxis
            yAxisId="kw"
            stroke={MUTED}
            tickLine={false}
            axisLine={false}
            domain={[0, maxKw]}
            tickFormatter={(v: number) => `${(v / 1000).toFixed(1)}MW`}
            tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }}
            width={52}
          />
          <YAxis
            yAxisId="px"
            orientation="right"
            stroke={MUTED}
            tickLine={false}
            axisLine={false}
            domain={[0, Math.ceil((hi * 1.6) / 10) * 10]}
            tickFormatter={(v: number) => `$${v}`}
            tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }}
            width={44}
          />
          <Tooltip
            cursor={{ fill: "rgba(255,255,255,0.04)" }}
            contentStyle={{ background: "#0d0d0c", border: "1px solid #2a2926", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 12 }}
            formatter={(v: number, name: string) =>
              name === "price" ? [`$${v.toFixed(2)}/MWh`, "price"] : [`${fmtInt(v)} kW`, name === "baseline" ? "7pm plug-in" : "agent plan"]
            }
          />
          <Bar yAxisId="px" dataKey="price" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.price === null ? "transparent" : mix("#24463a", RED, hi > lo ? (d.price - lo) / (hi - lo) : 0)} fillOpacity={0.55} />
            ))}
          </Bar>
          <Area yAxisId="kw" type="stepAfter" dataKey="baseline" stroke={RED} strokeWidth={1.5} fill={RED} fillOpacity={0.22} isAnimationActive={false} />
          <Area yAxisId="kw" type="stepAfter" dataKey="optimal" stroke={GREEN} strokeWidth={2} fill={GREEN} fillOpacity={0.28} isAnimationActive={false} />
          <ReferenceLine
            yAxisId="kw"
            y={plan.cap_kw}
            stroke="#e8e6e1"
            strokeDasharray="6 5"
            strokeOpacity={0.7}
            label={{ value: `cap ${fmtInt(plan.cap_kw)} kW`, position: "insideTopLeft", fill: "#e8e6e1", fontSize: 11, fontFamily: "JetBrains Mono" }}
          />
        </ComposedChart>
      </ResponsiveContainer>
      {plan.baseline_peak_kw !== undefined && plan.baseline_peak_kw > plan.cap_kw && (
        <p className="fine">
          Plugging in at 7pm peaks at <b className="red mono">{fmtInt(plan.baseline_peak_kw)} kW</b>, over the site cap, in the most expensive hour of the day.
        </p>
      )}
    </section>
  );
}

function Checks({ plan }: { plan: Plan }) {
  return (
    <ul className="checks">
      {plan.checks.map((c, i) => (
        <li key={i}>
          <span className={`mark ${c.ok ? "ok" : "bad"}`}>{c.ok ? "PASS" : "FAIL"}</span>
          <span className="name">{c.name}</span>
          <span className="mono detail">{c.detail}</span>
        </li>
      ))}
    </ul>
  );
}

function BacktestStrip({ data }: { data: Backtest | null }) {
  if (!data) return <p className="fine pad">Backtest unavailable (API offline).</p>;
  if (!data.days.length) return <p className="fine pad">No backtest days yet.</p>;
  const avg = data.total_saved / data.days.length;
  return (
    <section className="backtest">
      <div className="bt-total">
        <span className="num">{fmtUsd(data.total_saved)}</span>
        <span className="unit">saved over {data.days.length} nights</span>
        <span className="mono fine">avg {fmtUsd(avg)}/night</span>
      </div>
      <div className="bt-chart">
        <ResponsiveContainer width="100%" height={96}>
          <BarChart data={data.days} margin={{ top: 4, right: 0, left: 0, bottom: 0 }} barCategoryGap="18%">
            <XAxis dataKey="date" hide />
            <Tooltip
              cursor={{ fill: "rgba(255,255,255,0.05)" }}
              contentStyle={{ background: "#0d0d0c", border: "1px solid #2a2926", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 12 }}
              formatter={(v: number) => [fmtUsd(v, 2), "saved"]}
            />
            <Bar dataKey="saved_usd" fill={GREEN} fillOpacity={0.85} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
        <div className="bt-axis mono">
          <span>{data.days[0].date}</span>
          <span>{data.days[data.days.length - 1].date}</span>
        </div>
      </div>
    </section>
  );
}
