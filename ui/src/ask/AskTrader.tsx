import "./nodevtools";
import { Renderer } from "@openuidev/react-lang";
import { useEffect, useMemo, useState } from "react";
import { API } from "../api";
import { askPrompt, library } from "./library";
import "./ask.css";

const EXAMPLES = [
  "What if we keep 30% for the storm tomorrow?",
  "Why not sell at 3pm?",
  "How much did we make last week vs the simple rule?",
  "What if the battery could only do half a cycle?",
];

type Ev =
  | { kind: "status"; text: string }
  | { kind: "ui"; id: string; line: string }
  | { kind: "error"; text: string }
  | { kind: "done"; meta?: { ms?: number; tools?: string[] } };

type Turn = { q: string; status: { text: string; err?: boolean }[]; stmts: Record<string, string>; live: boolean; start: number; end?: number; tools?: string[] };

const program = (s: Record<string, string>) =>
  [s.root, ...Object.entries(s).filter(([k]) => k !== "root").map(([, v]) => v)].filter(Boolean).join("\n");

async function* sse(q: string, system_prompt: string): AsyncGenerator<Ev> {
  const res = await fetch(`${API}/api/ask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q, system_prompt }) });
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
      if (data) yield JSON.parse(data) as Ev;
    }
  }
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
  const waiting = !t.stmts.root;
  if (waiting)
    return (
      <div className="ask-loader">
        <ol>
          {t.status.map((s, i) => {
            const cur = t.live && i === t.status.length - 1 && !s.err;
            return <li key={i} className={s.err ? "err" : cur ? "cur" : ""}><span className="ic">{s.err ? "!" : cur ? <i className="ask-spin" /> : "✓"}</span>{s.text}</li>;
          })}
        </ol>
        <div className="ask-time mono">{secs.toFixed(1)}s</div>
      </div>
    );
  const errs = t.status.filter((s) => s.err);
  return (
    <div className="ask-steps">
      <button onClick={() => setOpen(!open)}>
        {t.status.length} steps {"·"} {secs.toFixed(1)}s{t.live ? " · drawing" : ""} {open ? "▴" : "▾"}
      </button>
      {open && <div className="ask-steplist">{t.status.map((s, i) => <div key={i} className={s.err ? "red" : ""}>{s.text}</div>)}{t.tools?.length ? <div>tools: {t.tools.join(", ")}</div> : null}</div>}
      {!open && errs.map((s, i) => <div key={i} className="red">{s.text}</div>)}
    </div>
  );
}

export default function AskTrader() {
  const [q, setQ] = useState("");
  const [turn, setTurn] = useState<Turn | null>(null);
  const busy = !!turn?.live;
  const prompt = useMemo(() => askPrompt(), []);

  async function run(text: string) {
    const query = text.trim();
    if (!query || busy) return;
    setQ("");
    const t0: Turn = { q: query, status: [], stmts: {}, live: true, start: performance.now() };
    setTurn(t0); // a new question replaces the old answer
    const patch = (f: (t: Turn) => Turn) => setTurn((t) => (t && t.start === t0.start ? f(t) : t));
    try {
      for await (const ev of sse(query, prompt)) {
        if (ev.kind === "status") patch((t) => ({ ...t, status: [...t.status, { text: ev.text }] }));
        else if (ev.kind === "ui") patch((t) => ({ ...t, stmts: { ...t.stmts, [ev.id]: ev.line } }));
        else if (ev.kind === "error") patch((t) => ({ ...t, status: [...t.status, { text: ev.text, err: true }] }));
        else if (ev.kind === "done") patch((t) => ({ ...t, live: false, tools: ev.meta?.tools, end: performance.now() }));
      }
    } catch (e) {
      patch((t) => ({ ...t, status: [...t.status, { text: `API unreachable at ${API}: ${String(e)}`, err: true }] }));
    }
    patch((t) => ({ ...t, live: false, end: t.end ?? performance.now() }));
  }

  return (
    <section className="ask">
      <div className="rule"><span>Ask the trader</span></div>
      <form className="ask-search" onSubmit={(e) => { e.preventDefault(); run(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask the trader..." />
        <button className="go" disabled={busy || !q.trim()}>{busy ? "thinking" : "Ask"}</button>
      </form>
      <div className="ask-chips">
        {EXAMPLES.map((x) => <button key={x} className="ask-chip" disabled={busy} onClick={() => run(x)}>{x}</button>)}
      </div>
      {turn && (
        <article className="ask-turn">
          <h2 className="ask-q">{turn.q}</h2>
          <Steps t={turn} />
          {turn.stmts.root && <Renderer response={program(turn.stmts)} library={library} isStreaming={turn.live} />}
        </article>
      )}
    </section>
  );
}
