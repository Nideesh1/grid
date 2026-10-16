"""ElevenLabs TTS of the dispatcher message (ELEVENLABS_API_KEY). Audio cached at data/voice/<plan_id>.mp3."""
from __future__ import annotations

import logging

import httpx

from grid.config import DATA, env, has

log = logging.getLogger("grid.voice")
VOICE_DIR = DATA / "voice"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs premade "George"
_state = {"error": None}


def enabled() -> bool:
    return has("ELEVENLABS_API_KEY")


def status() -> dict:
    return {"enabled": enabled(), "voice_id": env("ELEVENLABS_VOICE_ID", DEFAULT_VOICE) if enabled() else None,
            "error": _state["error"]}


def path_for(plan_id: str):
    return VOICE_DIR / f"{plan_id}.mp3"


def synthesize(plan_id: str, text: str) -> str | None:
    """Returns the mp3 path, or None when disabled / failed (logged, never raises)."""
    if not enabled():
        return None
    p = path_for(plan_id)
    if p.exists():
        return str(p)
    try:
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        vid = env("ELEVENLABS_VOICE_ID", DEFAULT_VOICE)
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
                       headers={"xi-api-key": env("ELEVENLABS_API_KEY"), "Content-Type": "application/json"},
                       json={"text": text, "model_id": env("ELEVENLABS_MODEL", "eleven_flash_v2_5")}, timeout=30)
        r.raise_for_status()
        p.write_bytes(r.content)
        _state["error"] = None
        return str(p)
    except Exception as e:  # noqa: BLE001
        _state["error"] = f"{type(e).__name__}"
        log.warning("elevenlabs tts failed: %s", _state["error"])
        return None


# ------------------------------------------------------------------ ElevenAgents voice dispatcher (in-browser)
import hashlib  # noqa: E402
import json  # noqa: E402

AGENT_FILE = DATA / "eleven_agent.json"
AGENT_NAME = "grid battery trader"
AGENT_PROMPT = (
    "You are the voice of GRID, an AI trader for a 10 megawatt, 40 megawatt-hour grid battery in New York City. It "
    "buys power when NYISO day-ahead prices are cheap, stores it, and sells when prices are high, inside hard safety "
    "limits, and you need the human dispatcher's approval. Always call get_plan first and quote only numbers returned "
    "by tools: buy and sell windows, profit in dollars, the naive-rule profit, cycles, safety violations, the Daytona "
    "sandbox. Pitch it like: 'Tomorrow I'll buy 2 to 5am and sell 5 to 8pm, profit 418 dollars. Approve?' If the "
    "dispatcher gives a constraint such as 'keep 30 percent in reserve for the storm', 'only half a cycle today', "
    "'don't sell before 6pm' or 'reset constraints', call replan with their words, wait, then read back the new profit "
    "and safety result. When they clearly approve, call approve with the latest plan_id and approved true; if they "
    "reject, approved false. Keep turns under 30 words, plain spoken English, no lists."
)
FIRST_MESSAGE = "Grid trader here. Tomorrow's battery trading plan is ready. Want the numbers, or any limits for tomorrow?"
CLIENT_TOOLS = [
    {"type": "client", "name": "get_plan", "expects_response": True, "response_timeout_secs": 20,
     "description": "Get the latest battery trading plan: plan_id, buy and sell windows, profit, naive-rule profit, cycles, violations, sandbox id.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "client", "name": "replan", "expects_response": True, "response_timeout_secs": 40,
     "description": "Re-plan with an operator constraint in plain words: reserve percent, cycle limit, no selling before an hour, no buying after an hour, or reset.",
     "parameters": {"type": "object", "required": ["constraint"], "properties": {
         "constraint": {"type": "string", "description": "e.g. 'keep 30% in reserve for the storm', 'only half a cycle today', \"don't sell before 6pm\", 'reset constraints'"}}}},
    {"type": "client", "name": "approve", "expects_response": True, "response_timeout_secs": 20,
     "description": "Record the dispatcher's decision on a plan.",
     "parameters": {"type": "object", "required": ["plan_id", "approved"], "properties": {
         "plan_id": {"type": "string", "description": "plan_id from get_plan or replan"},
         "approved": {"type": "boolean", "description": "true to approve, false to reject"}}}},
]
_agent_state: dict = {"agent_id": None, "error": None}


def _agent_config() -> dict:
    return {"name": AGENT_NAME, "conversation_config": {
        "agent": {"first_message": FIRST_MESSAGE, "language": "en",
                  "prompt": {"prompt": AGENT_PROMPT, "llm": env("ELEVENLABS_AGENT_LLM", "gemini-2.0-flash"),
                             "tools": CLIENT_TOOLS}},
        "tts": {"voice_id": env("ELEVENLABS_VOICE_ID", DEFAULT_VOICE)}}}


def ensure_agent() -> str | None:
    """Create the ElevenLabs agent once (agent_id kept in data/eleven_agent.json); PATCH it when the config changes."""
    if not enabled():
        return None
    cfg = _agent_config()
    digest = hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]
    saved = json.loads(AGENT_FILE.read_text()) if AGENT_FILE.exists() else {}
    h = {"xi-api-key": env("ELEVENLABS_API_KEY"), "Content-Type": "application/json"}
    try:
        if saved.get("agent_id"):
            if saved.get("digest") != digest:
                r = httpx.patch(f"https://api.elevenlabs.io/v1/convai/agents/{saved['agent_id']}", headers=h, json=cfg, timeout=30)
                if r.status_code == 404:
                    saved = {}
                else:
                    r.raise_for_status()
                    AGENT_FILE.write_text(json.dumps({**saved, "digest": digest}))
            if saved.get("agent_id"):
                _agent_state.update(agent_id=saved["agent_id"], error=None)
                return saved["agent_id"]
        r = httpx.post("https://api.elevenlabs.io/v1/convai/agents/create", headers=h, json=cfg, timeout=30)
        r.raise_for_status()
        aid = r.json()["agent_id"]
        AGENT_FILE.write_text(json.dumps({"agent_id": aid, "digest": digest}))
        _agent_state.update(agent_id=aid, error=None)
        log.info("elevenlabs agent created %s", aid)
        return aid
    except Exception as e:  # noqa: BLE001
        body = getattr(getattr(e, "response", None), "text", "")[:200]
        _agent_state["error"] = f"{type(e).__name__} {body}"
        log.warning("elevenlabs agent setup failed: %s", _agent_state["error"])
        return None


def signed_url(agent_id: str) -> str | None:
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url", params={"agent_id": agent_id},
                      headers={"xi-api-key": env("ELEVENLABS_API_KEY")}, timeout=15)
        r.raise_for_status()
        return r.json().get("signed_url")
    except Exception as e:  # noqa: BLE001
        log.info("signed url unavailable: %s", type(e).__name__)
        return None


def agent_status() -> dict:
    return {"agent_id": _agent_state["agent_id"], "agent_error": _agent_state["error"]}
