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
AGENT_NAME = "grid night dispatcher"
AGENT_PROMPT = (
    "You are the voice of GRID, the overnight energy dispatcher for a 40-bus electric bus depot in New York City. "
    "You talk with the human night dispatcher. Always call get_plan first to read tonight's plan; quote only numbers "
    "returned by tools (dollar savings tonight and per month, charging window, peak kW, safety violations, Daytona "
    "sandbox). If the dispatcher gives a constraint such as 'bus 12 leaves at 04:00' or a new site cap, call replan "
    "with it, wait for the result and read back the new savings and safety result. When the dispatcher clearly says "
    "yes or approve, call approve with the plan_id from the latest plan; if they say no, call approve with approved "
    "false. Keep every turn under 30 words, plain spoken English, no lists."
)
FIRST_MESSAGE = "Grid dispatcher here. Tonight's charging plan is ready. Want the numbers, or any buses with special departure times?"
CLIENT_TOOLS = [
    {"type": "client", "name": "get_plan", "expects_response": True, "response_timeout_secs": 20,
     "description": "Get tonight's latest charging plan: plan_id, savings tonight and per month, charging window, peak kW, violations, sandbox id.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "client", "name": "replan", "expects_response": True, "response_timeout_secs": 40,
     "description": "Re-plan tonight with a new constraint, e.g. 'bus 12 leaves at 04:00', and/or a new site power cap in kW.",
     "parameters": {"type": "object", "required": [], "properties": {
         "constraint": {"type": "string", "description": "Constraint in plain words, e.g. 'bus 12 leaves at 04:00'"},
         "cap_kw": {"type": "number", "description": "Optional site power cap in kW"}}}},
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
