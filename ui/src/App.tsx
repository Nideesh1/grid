import { useEffect, useMemo, useRef, useState } from "react";
import Voice from "./Voice";
import AskTrader from "./ask/AskTrader";
import {
  Bar,
  BarChart,
  Cell,
  ComposedChart,
  Line,
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
  backtestDay,
  fmtUsd,
  getBacktest,
  getJSON,
  getMemory,
  normalizePlan,
  progressFrom,
  streamPlan,
  type Backtest,
  type BacktestDay,
  type Fact,
  type Memory,
  type Plan,
  type Progress,
} from "./api";

const GREEN = "#38f28a";
const GOLD = "#f2b544";
const RED = "#ff5b3d";
const MUTED = "#6f6c66";
const INK = "#e8e6e1";
const TIP = { background: "#0d0d0c", border: "1px solid #2a2926", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 12 };

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

export default function App() {
  const [status, setStatus] = useState<string[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backtest, setBacktest] = useState<Backtest | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [liveDays, setLiveDays] = useState<BacktestDay[]>([]);
  const [approval, setApproval] = useState<"idle" | "sending" | "approved" | "error">("idle");
  const [audioUrl, setAudioUrl] = useState<string | undefined>();
  const [memory, setMemory] = useState<Memory | null>(null);
  const [flash, setFlash] = useState(0);
  const audioRef = useRef<HTMLAudioElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const refreshMemory = () => getMemory().then(setMemory);

  async function onReplanned(planId: string | undefined, summary?: unknown) {
    try {
      const full = planId ? await getJSON(`/api/plan/${planId}`) : summary;
      const p = normalizePlan(full);
      setPlan(p);
      setAudioUrl(p.audio_url);
      setApproval("idle");
      setFlash((f) => f + 1);
      setStatus((s) => [...s, `Re-planned by voice: ${p.plan_id ?? ""}`]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    refreshMemory();
  }
  function onVoiceApproved(ok: boolean) {
    setApproval(ok ? "approved" : "idle");
    refreshMemory();
  }

  async function runPlan() {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setPlanning(true);
    setPlan(null);
    setStatus([]);
    setError(null);
    setApproval("idle");
    setProgress(null);
    setLiveDays([]);
    try {
      await streamPlan((e) => {
        const pr = progressFrom(e);
        if (pr) setProgress(pr);
        if (e.day && typeof e.day === "object") setLiveDays((d) => [...d, backtestDay(e.day)]);
        if (e.kind === "status") setStatus((s) => [...s, String(e.text)]);
        else if (e.kind === "plan") {
          const p = normalizePlan(e);
          setPlan(p);
          setAudioUrl(p.audio_url);
        } else if (e.kind === "error") setError(String(e.text ?? "planner error"));
        else if (pr && e.kind !== "status" && pr.text) setStatus((s) => [...s.filter((x) => !/night\s+\d+\s*(\/|of)/i.test(x)), pr.text!]);
      }, ac.signal);
    } catch (err) {
      if (!ac.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!ac.signal.aborted) {
        setPlanning(false);
        getBacktest().then((b) => b && setBacktest(b));
      }
      refreshMemory();
    }
  }

  useEffect(() => {
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
      refreshMemory();
    } catch {
      setApproval("error");
    }
  }

  function playDispatcher() {
    if (audioUrl && audioRef.current) audioRef.current.play().catch(() => speakFallback());
    else speakFallback();
  }
  function speakFallback() {
    if (!plan || !("speechSynthesis" in window)) return;
    const text = plan.voice_text ?? `Tomorrow's plan earns ${Math.round(plan.profit_usd)} dollars inside every safety limit. Approve?`;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }

  const last30 = plan?.backtest_total_usd ?? backtest?.total;

  return (
    <div className="page">
      <header className="masthead">
        <div className="brand">
          <span className="wordmark">GRID</span>
          <span className="dek">AI battery trader · NYISO N.Y.C. zone · 10 MW / 40 MWh</span>
        </div>
        <nav>
          {MOCK && <span className="tag">mock data</span>}
          <a href="/backend">Backend</a>
        </nav>
      </header>

      <StatusFeed lines={status} planning={planning} error={error} onReplan={runPlan} />

      {plan && (
        <main className="result" key={flash}>
          <Hero plan={plan} last30={last30} />
          <AskTrader />
          <Rule label={`Tomorrow, hour by hour${plan.date ? ` · ${plan.date}` : ""}`} />
          <TradeChart plan={plan} />
          <div className="split">
            <section>
              <Rule label="Safety checks" />
              <Checks plan={plan} />
            </section>
            <section>
              <Rule label="Dispatcher" />
              <div className="approve">
                <button className="primary" onClick={onApprove} disabled={approval === "sending" || approval === "approved"}>
                  {approval === "approved" ? "Approved. Orders queued for the day-ahead market" : approval === "sending" ? "Sending..." : "Approve tomorrow's trades"}
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
                {audioUrl && <audio key={audioUrl} ref={audioRef} src={audioUrl} preload="auto" />}
                <Voice onReplanned={onReplanned} onApproved={onVoiceApproved} />
              </div>
            </section>
          </div>
        </main>
      )}

      <Rule label="Dispatcher memory (SurrealDB)" />
      <MemoryPanel data={memory} />

      <Rule label="Backtest · last 30 days, planned on day-ahead, settled on real-time prices" />
      <BacktestStrip data={backtest} progress={planning ? progress : null} liveDays={liveDays} />

      <footer className="foot">
        Prices: NYISO public LBMP, zone N.Y.C. Battery: 10 MW / 40 MWh, 85% round trip, fill 10% to 90%, at most 1 cycle per day, $8/MWh degradation.
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
        <span>{planning ? "Agent planning tomorrow's trades" : error ? "Planner error" : "Plan ready"}</span>
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

function Hero({ plan, last30 }: { plan: Plan; last30?: number }) {
  return (
    <section className="hero">
      <div className="big">
        <span className="num">{fmtUsd(plan.profit_usd)}</span>
        <span className="unit">profit tomorrow</span>
      </div>
      {last30 !== undefined && (
        <div className="second">
          <span className="num">{fmtUsd(last30)}</span>
          <span className="unit">over the last 30 real days</span>
        </div>
      )}
      <p className="src">
        vs a fixed buy-at-night / sell-at-evening rule
        {plan.baseline_profit_usd !== undefined && (
          <span className="mono">
            {" "}
            · rule {fmtUsd(plan.baseline_profit_usd)} → agent {fmtUsd(plan.profit_usd)}
          </span>
        )}
      </p>
    </section>
  );
}

function TradeChart({ plan }: { plan: Plan }) {
  const data = useMemo(
    () =>
      plan.rows.map((r) => ({
        label: hourLabel(r.hour),
        price: r.price,
        buy: r.charge_mw > 1e-6 ? -r.charge_mw : 0,
        sell: r.discharge_mw > 1e-6 ? r.discharge_mw : 0,
        soc: r.soc_pct,
      })),
    [plan],
  );
  if (!data.length) return <p className="fine pad">The plan has no hourly schedule yet.</p>;
  const ps = data.map((d) => d.price).filter((x): x is number => x !== null);
  const lo = ps.length ? Math.min(...ps) : 0;
  const hi = ps.length ? Math.max(...ps) : 1;
  const mw = Math.max(10, ...data.map((d) => Math.max(-d.buy, d.sell))) * 1.1;
  const xTick = { fontSize: 11, fontFamily: "JetBrains Mono" };
  const hasSoc = data.some((d) => d.soc !== null);

  return (
    <section className="chart">
      <div className="legend">
        <span>
          <i style={{ background: `linear-gradient(90deg, #24463a, ${RED})` }} /> NYISO day-ahead $/MWh
        </span>
        <span>
          <i style={{ background: GREEN }} /> Buy (charge MW)
        </span>
        <span>
          <i style={{ background: GOLD }} /> Sell (discharge MW)
        </span>
        {hasSoc && (
          <span>
            <i className="line" /> Battery fill %
          </span>
        )}
      </div>
      <div className="panel-label mono">$/MWh</div>
      <ResponsiveContainer width="100%" height={130}>
        <BarChart data={data} margin={{ top: 4, right: 44, left: 0, bottom: 0 }} barCategoryGap="10%" syncId="t">
          <XAxis dataKey="label" hide />
          <YAxis stroke={MUTED} tickLine={false} axisLine={false} width={52} tick={xTick} tickFormatter={(v: number) => `$${v}`} />
          <Tooltip cursor={{ fill: "rgba(255,255,255,0.04)" }} contentStyle={TIP} formatter={(v: number) => [`$${v.toFixed(2)}/MWh`, "price"]} />
          <Bar dataKey="price" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.price === null ? "transparent" : mix("#24463a", RED, hi > lo ? (d.price - lo) / (hi - lo) : 0)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <div className="panel-label mono">MW · sell above, buy below</div>
      <ResponsiveContainer width="100%" height={270}>
        <ComposedChart data={data} margin={{ top: 8, right: 0, left: 0, bottom: 0 }} barCategoryGap="10%" stackOffset="sign" syncId="t">
          <XAxis dataKey="label" stroke={MUTED} tickLine={false} axisLine={false} tick={xTick} interval={1} />
          <YAxis yAxisId="mw" stroke={MUTED} tickLine={false} axisLine={false} width={52} domain={[-mw, mw]} ticks={[-10, -5, 0, 5, 10]} tick={xTick} tickFormatter={(v: number) => `${v > 0 ? "+" : ""}${v.toFixed(0)}`} />
          <YAxis yAxisId="soc" orientation="right" stroke={MUTED} tickLine={false} axisLine={false} width={44} domain={[0, 100]} ticks={[0, 10, 50, 90, 100]} tick={xTick} tickFormatter={(v: number) => `${v}%`} />
          <Tooltip
            cursor={{ fill: "rgba(255,255,255,0.04)" }}
            contentStyle={TIP}
            formatter={(v: number, name: string) =>
              name === "soc" ? [`${v.toFixed(0)}%`, "fill"] : name === "buy" ? [`${(-v).toFixed(1)} MW`, "buy"] : [`${v.toFixed(1)} MW`, "sell"]
            }
          />
          <ReferenceLine yAxisId="mw" y={0} stroke={INK} strokeOpacity={0.5} />
          <ReferenceLine yAxisId="soc" y={90} stroke={MUTED} strokeDasharray="3 4" />
          <ReferenceLine yAxisId="soc" y={plan.reserve_pct ?? 10} stroke={MUTED} strokeDasharray="3 4" />
          <Bar yAxisId="mw" dataKey="sell" stackId="p" fill={GOLD} isAnimationActive={false} />
          <Bar yAxisId="mw" dataKey="buy" stackId="p" fill={GREEN} isAnimationActive={false} />
          {hasSoc && <Line yAxisId="soc" type="monotone" dataKey="soc" stroke={INK} strokeWidth={1.75} dot={false} isAnimationActive={false} connectNulls />}
        </ComposedChart>
      </ResponsiveContainer>
      <p className="fine">
        Dashed lines: fill limits {Math.round(plan.reserve_pct ?? 10)}% and 90%.
        {plan.revenue_usd !== undefined && plan.cost_usd !== undefined && (
          <span className="mono">
            {" "}
            Sells {fmtUsd(plan.revenue_usd)}, buys {fmtUsd(plan.cost_usd)}.
          </span>
        )}
      </p>
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

function BacktestStrip({ data, progress, liveDays }: { data: Backtest | null; progress: Progress | null; liveDays: BacktestDay[] }) {
  const days = progress && liveDays.length ? liveDays : data?.days ?? [];
  const prog = progress && (
    <div className="bt-progress mono">
      <span>
        simulating night {progress.n}/{progress.of}
        {progress.sandbox ? ` in sandbox ${progress.sandbox}` : ""}
      </span>
      <span className="track">
        <i style={{ width: `${Math.min(100, (progress.n / Math.max(1, progress.of)) * 100)}%` }} />
      </span>
    </div>
  );
  if (!days.length) return <>{prog ?? <p className="fine pad">{data ? "No backtest days yet." : "Backtest unavailable (API offline)."}</p>}</>;
  const total = progress && liveDays.length ? liveDays.reduce((a, d) => a + d.optimal, 0) : data?.total ?? 0;
  const base = progress && liveDays.length ? liveDays.reduce((a, d) => a + d.baseline, 0) : data?.total_baseline ?? 0;
  return (
    <section className="backtest">
      <div className="bt-total">
        <span className="num">{fmtUsd(total)}</span>
        <span className="unit">profit over {days.length} real days</span>
        <span className="mono fine">
          rule {fmtUsd(base)} · avg {fmtUsd(total / days.length)}/day
        </span>
      </div>
      <div className="bt-chart">
        {prog}
        <ResponsiveContainer width="100%" height={96}>
          <BarChart data={days} margin={{ top: 4, right: 0, left: 0, bottom: 0 }} barCategoryGap="18%" barGap={1}>
            <XAxis dataKey="date" hide />
            <Tooltip cursor={{ fill: "rgba(255,255,255,0.05)" }} contentStyle={TIP} formatter={(v: number, n: string) => [fmtUsd(v, 2), n === "optimal" ? "agent" : "rule"]} />
            <Bar dataKey="baseline" fill={MUTED} fillOpacity={0.6} isAnimationActive={false} />
            <Bar dataKey="optimal" fill={GOLD} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
        <div className="bt-axis mono">
          <span>{days[0].date}</span>
          <span>
            <i className="sw" style={{ background: GOLD }} /> agent <i className="sw" style={{ background: MUTED }} /> fixed rule
          </span>
          <span>{days[days.length - 1].date}</span>
        </div>
      </div>
    </section>
  );
}

function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "none";
  if (typeof v !== "object") return String(v);
  const o = v as Record<string, unknown>;
  return Object.entries(o)
    .filter(([, x]) => x !== null && x !== undefined)
    .slice(0, 4)
    .map(([k, x]) => `${k.replace(/_/g, " ")} ${typeof x === "number" && /usd/.test(k) ? fmtUsd(x) : typeof x === "object" ? JSON.stringify(x) : String(x)}`)
    .join(" · ");
}
function fmtTime(t?: string): string {
  if (!t) return "";
  const d = new Date(t);
  return isNaN(+d) ? t : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function MemoryPanel({ data }: { data: Memory | null }) {
  if (!data) return <p className="fine pad">Memory unavailable (API offline).</p>;
  const byId = new Map(data.facts.map((f) => [f.id, f]));
  const prevOf = new Map<string, Fact>();
  for (const f of data.facts) if (f.superseded_by && byId.has(f.superseded_by)) prevOf.set(f.superseded_by, f);
  const current = data.facts.filter((f) => !f.superseded_by || !byId.has(f.superseded_by)).slice(0, 8);
  if (!current.length) return <p className="fine pad">No facts remembered yet.</p>;
  return (
    <section className="memory">
      {data.backend && !/surreal/i.test(data.backend) && <p className="fine">Backend: {data.backend} fallback with the same valid-time shape.</p>}
      <ul>
        {current.map((f) => {
          const chain: Fact[] = [];
          let p = prevOf.get(f.id);
          while (p && chain.length < 1) {
            chain.push(p);
            p = prevOf.get(p.id);
          }
          return (
            <li key={f.id}>
              <span className="mkind mono">{f.kind}</span>
              <span className="mkey">{f.key}</span>
              <span className="mval">
                {chain
                  .slice()
                  .reverse()
                  .map((c) => (
                    <span key={c.id} className="old" title={`${c.source ?? ""} ${fmtTime(c.valid_from)}`}>
                      <s>{fmtVal(c.value)}</s> <span className="arrow">→</span>{" "}
                    </span>
                  ))}
                <span className="new">{fmtVal(f.value)}</span>
              </span>
              <span className="msrc mono">
                {f.source} · {fmtTime(f.valid_from)}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
