// "Talk to dispatcher": ElevenLabs conversational agent with client tools that drive this page.
import { useEffect, useRef, useState } from "react";
import { useConversation } from "@elevenlabs/react";
import { MOCK, getJSON, postJSON } from "./api";

type Line = { role: "user" | "agent" | "tool"; text: string };

export default function Voice({
  onReplanned,
  onApproved,
}: {
  onReplanned: (planId: string | undefined, summary: unknown) => Promise<void> | void;
  onApproved: (approved: boolean) => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const add = (l: Line) => setLines((s) => [...s.slice(-40), l]);

  const conv = useConversation({
    onMessage: (m) => add({ role: m.role === "user" || m.source === "user" ? "user" : "agent", text: m.message }),
    onError: (e: unknown) => setErr(typeof e === "string" ? e : (e as Error)?.message ?? "voice error"),
    onDisconnect: () => setLevel(0),
  });

  const live = conv.status === "connected";
  const busy = conv.status === "connecting";

  // mic / speaking meter
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
      add({ role: "tool", text: "get_plan" });
      return JSON.stringify(await getJSON("/api/plan/latest"));
    },
    replan: async (p: Record<string, unknown>) => {
      add({ role: "tool", text: `replan ${JSON.stringify(p)}` });
      const res = await postJSON("/api/replan", { constraint: p.constraint ?? null, cap_kw: p.cap_kw ?? null });
      await onReplanned(res?.plan_id, res);
      return JSON.stringify(res);
    },
    approve: async (p: Record<string, unknown>) => {
      const approved = p.approved !== false && p.approved !== "false";
      add({ role: "tool", text: `approve ${approved ? "yes" : "no"}` });
      let planId = p.plan_id as string | undefined;
      if (!planId) planId = (await getJSON("/api/plan/latest"))?.plan_id;
      const res = await postJSON("/api/approve", { plan_id: planId, approved, who: "voice" });
      onApproved(approved);
      return JSON.stringify(res);
    },
  };

  async function start() {
    setErr(null);
    try {
      await navigator.mediaDevices.getUserMedia({ audio: true });
      // Prefer a signed URL (WebSocket, works for private agents); fall back to the public agent id (WebRTC).
      let j = await getJSON("/api/voice/agent?signed=true").catch(() => null);
      if (!j?.signed_url && !j?.agent_id) j = await getJSON("/api/voice/agent");
      if (j?.signed_url) conv.startSession({ signedUrl: j.signed_url, connectionType: "websocket", clientTools });
      else if (j?.agent_id) conv.startSession({ agentId: j.agent_id, connectionType: "webrtc", clientTools });
      else throw new Error("no agent from /api/voice/agent");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  const state = !live ? (busy ? "connecting" : "off") : conv.isSpeaking ? "speaking" : "listening";

  return (
    <div className="voice">
      <div className="voice-row">
        <button className="secondary" onClick={live ? () => conv.endSession() : start} disabled={busy || MOCK}>
          {live ? "■ End call" : busy ? "Connecting..." : "● Talk to dispatcher"}
        </button>
        <span className={`vstate ${state}`}>
          <span className="bars">
            {[0, 1, 2, 3, 4].map((i) => (
              <i key={i} style={{ height: live ? `${4 + Math.min(1, level * (1.6 + i * 0.3)) * 18}px` : "4px" }} />
            ))}
          </span>
          {state === "speaking" ? "dispatcher speaking" : state === "listening" ? "mic live, listening" : state === "connecting" ? "connecting" : MOCK ? "voice off in mock mode" : "mic off"}
        </span>
      </div>
      {err && <p className="fine red">{err}</p>}
      {lines.length > 0 && (
        <ol className="transcript">
          {lines.map((l, i) => (
            <li key={i} className={l.role}>
              <span className="who mono">{l.role === "user" ? "YOU" : l.role === "agent" ? "DISPATCH" : "TOOL"}</span>
              <span>{l.text}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
