import { fmtUsd, type Fact, type Memory } from "./api";

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

export function MemoryPanel({ data }: { data: Memory | null }) {
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
