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
