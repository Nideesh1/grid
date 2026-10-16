import { AgentScene } from "agentglow";
import "agentglow/style.css";

const SOURCE = (import.meta.env.VITE_AGENTGLOW as string | undefined) ?? "http://localhost:8104";

export default function Backend() {
  return (
    <div className="scene">
      <AgentScene theme="neural" source={SOURCE} hud style={{ width: "100%", height: "100%" }} />
      <a className="scene-back" href="/">
        ← GRID
      </a>
    </div>
  );
}
