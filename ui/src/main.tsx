import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ConversationProvider } from "@elevenlabs/react";
import "./styles.css";

const Backend = lazy(() => import("./Backend"));
const isBackend = window.location.pathname.replace(/\/+$/, "") === "/backend";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {isBackend ? (
      <Suspense fallback={<div className="scene-loading">Loading agent scene...</div>}>
        <Backend />
      </Suspense>
    ) : (
      <ConversationProvider>
        <App />
      </ConversationProvider>
    )}
  </StrictMode>,
);
