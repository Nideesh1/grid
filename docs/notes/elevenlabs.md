# ElevenLabs Agents: verified notes (2026-10-16)

The flow below was run against the live API with our key. It created 3 client tools, created an agent, fetched a signed URL and a conversation token, ran a simulated conversation and then deleted everything.
Result: for the user line "what if bus 12 leaves at 4am?", the agent called `replan({"constraint": "bus 12 leaves at 4am"})`. It then read back the mocked result. `analysis.call_successful` came back as `success`.

## Key facts
- Auth header: `xi-api-key: $ELEVENLABS_API_KEY`. Base URL: `https://api.elevenlabs.io`.
- **Tools are standalone objects.** Create each one with `POST /v1/convai/tools`, then attach them to the agent through `conversation_config.agent.prompt.tool_ids`.
- **Client tools vs server tools:**
  - `type: "client"` runs in the browser. Register a handler with the same name in `clientTools`. Set `expects_response: true` so the agent waits for the return value (the dashboard calls this "blocking").
  - `type: "webhook"` (server tool) makes ElevenLabs call your HTTPS URL directly. You do not need a browser, but the URL must be publicly reachable.
- **LLM** (`prompt.llm`):
  - Built-in options include `gemini-2.5-flash` (fast; we tested it), `gpt-4.1-mini`, `gpt-5*`, `claude-sonnet-4-5`, `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-*`, `qwen3-30b-a3b`, `gpt-oss-120b` and others.
  - **Claude is built in**, so you do not need your own key for it.
  - `llm: "custom-llm"` plus `custom_llm: {url, model_id, api_key: {secret_id}}` points the agent at any OpenAI-compatible `/v1/chat/completions` or `/v1/responses` server. This is how to route it through our own backend.
- **Voice and latency** (`conversation_config.tts`):
  - `voice_id` (Rachel is `21m00Tcm4TlvDq8ikWAM`).
  - `model_id: "eleven_flash_v2"` for English. Use `eleven_flash_v2_5` for other languages; it is the lowest-latency option.
  - `optimize_streaming_latency: 0-4`.
  - A fast LLM (gemini-2.5-flash or claude-haiku-4-5) matters more than any of the TTS settings.
- **Browser auth:**
  - Public agent: `startSession({agentId})` is enough.
  - Private agent: the server mints a credential with the API key.
    - `GET /v1/convai/conversation/token?agent_id=…` returns `{token, conversation_id}`. Use it for **WebRTC**, which is the default for voice.
    - `GET /v1/convai/conversation/get-signed-url?agent_id=…` returns `{signed_url: "wss://…"}`. Use it for **WebSocket**.
- **Headless test:** `POST /v1/convai/agents/{id}/simulate-conversation`. You can set `tool_mock_config` to fake client tool results. The endpoint is deprecated and will be removed on 2026-10-31; the replacement is `/v1/convai/agent-testing/create` plus `/run-tests`. It still works today. A run took about 10s.
- **Deleting tools:** `DELETE /v1/convai/tools/{id}` returns 409 "still in use" even after the agent is deleted. Add `?force=true` to delete it.

## Python (REST, verified) — `uv run --with httpx --with python-dotenv python setup.py`
```python
import os, json, httpx
from dotenv import dotenv_values
K = dotenv_values(os.path.expanduser("~/Documents/CODING/grid/.env"))["ELEVENLABS_API_KEY"]
c = httpx.Client(base_url="https://api.elevenlabs.io", headers={"xi-api-key": K}, timeout=180)

def tool(name, desc, props, req):
    r = c.post("/v1/convai/tools", json={"tool_config": {
        "type": "client", "name": name, "description": desc,
        "expects_response": True, "response_timeout_secs": 20,
        "parameters": {"type": "object", "properties": props, "required": req}}})
    r.raise_for_status(); return r.json()["id"]

tids = [
  tool("get_plan", "Fetch the current bus dispatch plan.", {}, []),
  tool("replan", "Recompute the dispatch plan under a new constraint stated by the user, e.g. 'bus 12 departs 04:00'.",
       {"constraint": {"type": "string", "description": "The new constraint in plain words"}}, ["constraint"]),
  tool("approve", "Approve a plan by id once the user confirms.",
       {"plan_id": {"type": "string", "description": "Plan id"}}, ["plan_id"]),
]
agent_id = c.post("/v1/convai/agents/create", json={"name": "grid-dispatcher", "conversation_config": {
  "agent": {"first_message": "Dispatch here. Want the current plan or a what-if?", "language": "en",
    "prompt": {"prompt": "You are a bus fleet dispatcher voice assistant. For any what-if question or schedule change, "
                         "call replan with the constraint. Use get_plan to read the plan. Only call approve after the "
                         "user explicitly confirms. Keep replies to one or two sentences.",
               "llm": "gemini-2.5-flash", "temperature": 0.2, "tool_ids": tids}},
  "tts": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_flash_v2", "optimize_streaming_latency": 3},
}}).json()["agent_id"]

# browser credentials (server side only; never ship the API key)
token = c.get("/v1/convai/conversation/token", params={"agent_id": agent_id}).json()["token"]          # WebRTC
signed = c.get("/v1/convai/conversation/get-signed-url", params={"agent_id": agent_id}).json()["signed_url"]  # WebSocket

# headless simulated conversation with mocked client tools
sim = c.post(f"/v1/convai/agents/{agent_id}/simulate-conversation", json={"simulation_specification": {
  "simulated_user_config": {"first_message": "what if bus 12 leaves at 4am?", "language": "en",
     "prompt": {"prompt": "You are a dispatcher asking: what if bus 12 leaves at 4am? After the agent answers, say thanks and end."}},
  "tool_mock_config": {"replan": {"default_return_value": json.dumps({"plan_id": "p-42", "summary": "Bus 12 at 04:00; route 7 shifts to bus 9"})}}},
  "new_turns_limit": 6}).json()
for m in sim["simulated_conversation"]:
    print(m["role"], m.get("message"), [(t["tool_name"], t["params_as_json"]) for t in m.get("tool_calls") or []])
# -> agent | [('replan', '{"constraint": "bus 12 leaves at 4am"}')]

# cleanup
c.delete(f"/v1/convai/agents/{agent_id}")
for t in tids: c.delete(f"/v1/convai/tools/{t}", params={"force": "true"})
```

## FastAPI token endpoint
```python
@app.get("/api/el-token")
async def el_token():
    async with httpx.AsyncClient() as h:
        r = await h.get("https://api.elevenlabs.io/v1/convai/conversation/token",
                        params={"agent_id": AGENT_ID}, headers={"xi-api-key": ELEVENLABS_API_KEY})
    r.raise_for_status(); return {"token": r.json()["token"]}
```

## React (`npm i @elevenlabs/react`, from the current docs; not run in a browser)
The current SDK requires a `<ConversationProvider>` ancestor. `useConversation(opts)` and the provider accept the same options.
Client tool names must exactly match the tool names configured on the agent. The handler's return value is sent back to the LLM.
```tsx
"use client";
import { ConversationProvider, useConversation } from "@elevenlabs/react";

export default function Page() {
  return <ConversationProvider><Dispatcher /></ConversationProvider>;
}

function Dispatcher() {
  const conversation = useConversation({
    clientTools: {
      get_plan: async () => JSON.stringify(await (await fetch("/api/plan")).json()),
      replan: async ({ constraint }: { constraint: string }) =>
        JSON.stringify(await (await fetch("/api/replan", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ constraint }) })).json()),
      approve: async ({ plan_id }: { plan_id: string }) =>
        JSON.stringify(await (await fetch(`/api/approve/${plan_id}`, { method: "POST" })).json()),
    },
    onMessage: (m) => console.log(m),
    onError: (e) => console.error(e),
  });

  const start = async () => {
    await navigator.mediaDevices.getUserMedia({ audio: true });
    const { token } = await (await fetch("/api/el-token")).json();
    await conversation.startSession({ conversationToken: token });       // WebRTC (default for voice)
    // alt: startSession({ signedUrl })  -> WebSocket; or startSession({ agentId }) for a public agent
    // connectionType: "webrtc" | "websocket" can be passed explicitly
  };
  return conversation.status === "connected"
    ? <button onClick={() => conversation.endSession()}>Stop ({conversation.isSpeaking ? "speaking" : "listening"})</button>
    : <button onClick={start}>Talk to dispatch</button>;
}
```
Other helpers:
- `conversation.sendUserMessage(text)` sends a typed user turn.
- `conversation.sendContextualUpdate(text)` pushes silent context, such as "plan p-42 approved".
- `useConversationClientTool("name", fn)` registers a tool from inside a component and unregisters it on unmount.
