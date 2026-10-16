// Voice dispatcher: ElevenLabs conversational agent whose client tools drive this page.
// The session lives in the app-level <ConversationProvider>; the mic in the search bar owns it.
// Anything else (e.g. ApproveCard's "talk" button) starts it via the "grid:voice" window event.
import { useEffect, useRef, useState } from "react";
import { useConversation } from "@elevenlabs/react";
import { MOCK, getJSON, postJSON } from "./api";

export type VoiceLine = { role: "user" | "agent" | "tool"; text: string };
export type VoiceHooks = {
  /** a tool changed the plan (replan / approve): render it */
  onPlanChanged: (planId: string | undefined, why: "replan" | "approve") => void;
};

export function startVoice() {
  window.dispatchEvent(new CustomEvent("grid:voice"));
}

export function useDispatcherVoice({ onPlanChanged }: VoiceHooks) {
  const [lines, setLines] = useState<VoiceLine[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const add = (l: VoiceLine) => setLines((s) => [...s.slice(-12), l]);
  const cb = useRef(onPlanChanged);
  cb.current = onPlanChanged;

  const conv = useConversation({
    onMessage: (m) => add({ role: m.role === "user" || m.source === "user" ? "user" : "agent", text: m.message }),
    onError: (e: unknown) => setErr(typeof e === "string" ? e : (e as Error)?.message ?? "voice error"),
    onDisconnect: () => setLevel(0),
  });
  const live = conv.status === "connected";
  const busy = conv.status === "connecting";

  const raf = useRef<number>(0);
  useEffect(() => {
    if (!live) return;
    const tick = () => {
      try {
        setLevel(conv.isSpeaking ? conv.getOutputVolume() : conv.getInputVolume());
      } catch {
        /* not ready */
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [live, conv.isSpeaking, conv]);

  const clientTools = {
    get_plan: async () => {
      add({ role: "tool", text: "reading the plan" });
      return JSON.stringify(await getJSON("/api/plan/latest"));
    },
    replan: async (p: Record<string, unknown>) => {
      add({ role: "tool", text: `re-planning: ${p.constraint ?? "same rules"}` });
      const res = await postJSON("/api/replan", { constraint: p.constraint ?? null });
      cb.current(res?.plan_id, "replan");
      return JSON.stringify(res);
    },
    approve: async (p: Record<string, unknown>) => {
      const approved = p.approved !== false && p.approved !== "false";
      add({ role: "tool", text: approved ? "approving the plan" : "rejecting the plan" });
      let planId = p.plan_id as string | undefined;
      if (!planId) planId = (await getJSON("/api/plan/latest"))?.plan_id;
      const res = await postJSON("/api/approve", { plan_id: planId, approved, who: "voice" });
      cb.current(planId, "approve");
      return JSON.stringify(res);
    },
  };

  async function start() {
    if (MOCK) {
      setErr("Voice is off in mock mode.");
      return;
    }
    setErr(null);
    setLines([]);
    try {
      await navigator.mediaDevices.getUserMedia({ audio: true });
      let j = await getJSON("/api/voice/agent?signed=true").catch(() => null);
      if (!j?.signed_url && !j?.agent_id) j = await getJSON("/api/voice/agent");
      if (j?.signed_url) conv.startSession({ signedUrl: j.signed_url, connectionType: "websocket", clientTools });
      else if (j?.agent_id) conv.startSession({ agentId: j.agent_id, connectionType: "webrtc", clientTools });
      else throw new Error("no agent from /api/voice/agent");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  const stop = () => conv.endSession();
  const toggle = () => (live || busy ? stop() : start());

  useEffect(() => {
    const h = () => {
      if (!live && !busy) start();
    };
    window.addEventListener("grid:voice", h);
    return () => window.removeEventListener("grid:voice", h);
  });

  const state: "off" | "connecting" | "speaking" | "listening" = !live ? (busy ? "connecting" : "off") : conv.isSpeaking ? "speaking" : "listening";
  return { state, level, lines, err, toggle, live };
}

export function MicIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <rect x="9" y="3" width="6" height="12" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}

/** Voice state + transcript, shown under the search bar while a session is open. */
export function VoiceTranscript({ v }: { v: ReturnType<typeof useDispatcherVoice> }) {
  if (v.state === "off" && !v.err && !v.lines.length) return null;
  const label = v.state === "speaking" ? "Dispatcher speaking" : v.state === "listening" ? "Listening" : v.state === "connecting" ? "Connecting" : "Call ended";
  return (
    <div className="vt">
      <div className={`vt-state ${v.state}`}>
        <span className="bars">
          {[0, 1, 2, 3, 4].map((i) => (
            <i key={i} style={{ height: v.state === "speaking" || v.state === "listening" ? `${3 + Math.min(1, v.level * (1.6 + i * 0.3)) * 13}px` : "3px" }} />
          ))}
        </span>
        <span>{label}</span>
      </div>
      {v.err && <p className="vt-err">{v.err}</p>}
      {v.lines.length > 0 && (
        <ol>
          {v.lines.slice(-4).map((l, i) => (
            <li key={i} className={l.role}>
              <span className="who">{l.role === "user" ? "You" : l.role === "agent" ? "Trader" : "·"}</span>
              <span>{l.text}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
