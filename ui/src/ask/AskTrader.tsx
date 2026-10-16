import "./nodevtools";
import { Renderer } from "@openuidev/react-lang";
import { useEffect, useMemo, useRef, useState } from "react";
import { API, MOCK, getBacktest, getJSON, streamPlan } from "../api";
import { MicIcon, VoiceTranscript, useDispatcherVoice } from "../Voice";
import { askPrompt, library } from "./library";
import { backtestProgram, bidsProgram, gridProgram, memoryProgram, planProgram, playProgram, route, type Stmts } from "./programs";
import "./ask.css";

const EXAMPLES = ["How much will my battery make tomorrow?", "Any grid alerts right now?", "Show the NY grid right now", "Keep 30% for the storm"];

/* eslint-disable @typescript-eslint/no-explicit-any */
type Ev = { kind: string; [k: string]: any };
type Turn = { q: string; status: { text: string; err?: boolean }[]; stmts: Stmts; live: boolean; start: number; end?: number; tools?: string[] };

const program = (s: Stmts) => [s.root, ...Object.entries(s).filter(([k]) => k !== "root").map(([, v]) => v)].filter(Boolean).join("\n");

async function* sse(res: Response): AsyncGenerator<Ev> {
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const data = buf.slice(0, i).split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
      buf = buf.slice(i + 2);
      if (data) {
        try {
          yield JSON.parse(data) as Ev;
        } catch {
          /* skip */
        }
      }
    }
  }
}

async function ask(q: string, system_prompt: string): Promise<Response> {
  const opts = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q, system_prompt }) };
  // /api/command handles both commands and questions; fall back to /api/ask if it is not deployed.
  const res = await fetch(`${API}/api/command`, opts);
  if (res.status === 404 || res.status === 405) return fetch(`${API}/api/ask`, opts);
  return res;
}

function Steps({ t }: { t: Turn }) {
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(performance.now());
  useEffect(() => {
    if (!t.live) return;
    const id = setInterval(() => setNow(performance.now()), 100);
    return () => clearInterval(id);
  }, [t.live]);
  const secs = ((t.end ?? now) - t.start) / 1000;
  if (!t.stmts.root)
    return (
      <div className="ask-loader">
        <ol>
          {t.status.slice(-6).map((s, i, a) => {
            const cur = t.live && i === a.length - 1 && !s.err;
            return (
              <li key={i} className={s.err ? "err" : cur ? "cur" : ""}>
                <span className="ic">{s.err ? "!" : cur ? <i className="ask-spin" /> : "✓"}</span>
                {s.text}
              </li>
            );
          })}
          {!t.status.length && t.live && <li className="cur"><span className="ic"><i className="ask-spin" /></span>thinking</li>}
        </ol>
        <div className="ask-time mono">{secs.toFixed(1)}s</div>
      </div>
    );
  const errs = t.status.filter((s) => s.err);
  return (
    <div className="ask-steps">
      <button onClick={() => setOpen(!open)}>
        {t.status.length} steps · {secs.toFixed(1)}s{t.live ? " · drawing" : ""} {open ? "▴" : "▾"}
      </button>
      {open && (
        <div className="ask-steplist">
          {t.status.map((s, i) => <div key={i} className={s.err ? "red" : ""}>{s.text}</div>)}
          {t.tools?.length ? <div>tools: {t.tools.join(", ")}</div> : null}
        </div>
      )}
      {!open && errs.map((s, i) => <div key={i} className="red">{s.text}</div>)}
    </div>
  );
}

export default function AskTrader({ battery, autoStart = false }: { battery: Record<string, unknown>; autoStart?: boolean }) {
  const [q, setQ] = useState("");
  const [turn, setTurn] = useState<Turn | null>(null);
  const busy = !!turn?.live;
  const prompt = useMemo(() => askPrompt(), []);
  const lastPlan = useRef<any>(null);
  const batRef = useRef(battery);
  batRef.current = battery;

  function begin(query: string) {
    const t0: Turn = { q: query, status: [], stmts: {}, live: true, start: performance.now() };
    setTurn(t0); // a new query replaces the old answer
    const patch = (f: (t: Turn) => Turn) => setTurn((t) => (t && t.start === t0.start ? f(t) : t));
    const say = (text: string, err = false) => patch((t) => ({ ...t, status: [...t.status, { text, err }] }));
    const draw = (stmts: Stmts) => patch((t) => ({ ...t, stmts }));
    const end = () => patch((t) => ({ ...t, live: false, end: t.end ?? performance.now() }));
    return { patch, say, draw, end };
  }

  /** Instant answer from the optimizer fast path (no LLM). */
  async function runFast(query = EXAMPLES[0]) {
    const { say, draw, end } = begin(query);
    try {
      await streamPlan(
        (e) => {
          if (e.kind === "status" && e.text) say(String(e.text));
          if (e.kind === "plan") {
            lastPlan.current = e;
            draw(planProgram(e));
          }
          if (e.kind === "error") say(String(e.text ?? "planner error"), true);
        },
        undefined,
        true,
      );
    } catch (err) {
      say(`API unreachable at ${API}: ${String(err)}`, true);
    }
    end();
  }

  /** Client-side answer (mock mode, or when the API cannot answer). */
  async function runLocal(query: string, h: ReturnType<typeof begin>) {
    const kind = route(query);
    if (kind === "grid") return h.draw(gridProgram());
    if (kind === "memory") return h.draw(memoryProgram());
    if (kind === "backtest") {
      h.say("reading 30 days of backtest results");
      const bt = await getBacktest();
      return bt ? h.draw(backtestProgram(bt)) : h.say("backtest unavailable", true);
    }
    if (!lastPlan.current) {
      h.say("optimizing tomorrow's plan");
      await streamPlan((e) => e.kind === "plan" && (lastPlan.current = e), undefined, true);
    }
    const p = lastPlan.current;
    if (!p) return h.say("no plan available", true);
    if (kind === "play") return h.draw(playProgram(p, batRef.current));
    if (kind === "bids") return h.draw(bidsProgram(p));
    return h.draw(planProgram(p));
  }

  async function run(text: string) {
    const query = text.trim();
    if (!query || busy) return;
    setQ("");
    const h = begin(query);
    if (MOCK) {
      for (const s of ["reading the question", "reading NYISO prices", "simulating in a Daytona sandbox"]) {
        h.say(s);
        await new Promise((r) => setTimeout(r, 250));
      }
      await runLocal(query, h);
      return h.end();
    }
    let drew = false;
    let gotPlan: any = null;
    try {
      for await (const ev of sse(await ask(query, prompt))) {
        if (ev.kind === "status") h.say(String(ev.text));
        else if (ev.kind === "ui") {
          drew = true;
          h.patch((t) => ({ ...t, stmts: { ...t.stmts, [ev.id]: ev.line } }));
        } else if (ev.kind === "plan") {
          gotPlan = ev;
          lastPlan.current = ev;
        } else if (ev.kind === "command") h.say(`${ev.applied ? "Applied" : "Not applied"}: ${ev.text ?? ""}`);
        else if (ev.kind === "error") h.say(String(ev.text), true);
        else if (ev.kind === "done") h.patch((t) => ({ ...t, tools: ev.meta?.tools }));
      }
      if (!drew && gotPlan) h.draw(planProgram(gotPlan));
    } catch (e) {
      h.say(`The trader could not answer (${String(e)}); showing the optimizer's answer.`, true);
      await runLocal(query, h).catch(() => undefined);
    }
    h.end();
  }

  async function showPlan(planId: string | undefined, why: "replan" | "approve") {
    const h = begin(why === "approve" ? "Approved by voice" : "Re-planned by voice");
    try {
      const p = planId ? await getJSON(`/api/plan/${planId}`) : await getJSON("/api/plan/latest");
      lastPlan.current = p;
      h.draw(planProgram(p, why === "approve" ? "approved profit tomorrow" : "new profit tomorrow"));
    } catch (e) {
      h.say(String(e), true);
    }
    h.end();
  }

  const voice = useDispatcherVoice({ onPlanChanged: showPlan });

  // first load: instant answer; other parts of the page can trigger queries / re-plans
  const started = useRef(false);
  useEffect(() => {
    if (autoStart && !started.current) {
      started.current = true;
      runFast();
    }
    const onAsk = (e: Event) => run(String((e as CustomEvent).detail ?? ""));
    const onReplan = () => runFast("How much will my battery make tomorrow?");
    window.addEventListener("grid:ask", onAsk);
    window.addEventListener("grid:replan", onReplan);
    return () => {
      window.removeEventListener("grid:ask", onAsk);
      window.removeEventListener("grid:replan", onReplan);
    };
  });

  return (
    <section className="ask ask-top">
      <form className="ask-search" onSubmit={(e) => { e.preventDefault(); run(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={window.innerWidth < 640 ? "Ask, or give a command" : "Ask or command: keep 30% for the storm, only sell above $60, approve"} aria-label="Ask the trader" />
        <button type="button" className={`mic ${voice.state}`} onClick={voice.toggle} aria-label={voice.live ? "End voice" : "Talk to the trader"} title={voice.live ? "End voice" : "Talk to the trader"}>
          <MicIcon />
        </button>
        <button className="go" disabled={busy || !q.trim()}>{busy ? "···" : "Ask"}</button>
      </form>
      <VoiceTranscript v={voice} />
      <div className="ask-chips">
        {EXAMPLES.map((x) => (
          <button key={x} className="ask-chip" disabled={busy} onClick={() => run(x)}>{x}</button>
        ))}
      </div>
      {turn && (
        <article className="ask-turn" key={turn.start}>
          <h2 className="ask-q">{turn.q}</h2>
          <Steps t={turn} />
          {turn.stmts.root && <Renderer response={program(turn.stmts)} library={library} isStreaming={turn.live} />}
        </article>
      )}
    </section>
  );
}
