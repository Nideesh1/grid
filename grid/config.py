"""Settings: load .env fresh on every call (keys may be added while the API runs) and set up tracing once."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
AGENTGLOW_URL = os.environ.get("AGENTGLOW_URL", "http://localhost:8104")

log = logging.getLogger("grid")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def env(name: str, default: str | None = None) -> str | None:
    """Re-read .env each call so keys filled in mid-demo are picked up. Never log the value."""
    vals = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    v = vals.get(name) or os.environ.get(name) or default
    return v.strip() if isinstance(v, str) and v.strip() else default


def has(*names: str) -> bool:
    return all(env(n) for n in names)


_tracing = {"done": False, "agentglow": False, "arize": False}


def setup_tracing(app=None) -> dict:
    """AgentGlow (always tried, never raises) + Arize OTel exporter on the same provider when keys exist."""
    if _tracing["done"]:
        return dict(_tracing)
    _tracing["done"] = True
    try:
        from grid.sponsors import arize

        _tracing["arize"] = arize.setup()
    except Exception as e:  # noqa: BLE001
        log.warning("arize tracing disabled: %s", type(e).__name__)
    try:
        import agentglow

        agentglow.watch(AGENTGLOW_URL, service_name="grid-api", app=app, privacy="standard")
        _tracing["agentglow"] = True
        log.info("agentglow tracing -> %s", AGENTGLOW_URL)
    except Exception as e:  # noqa: BLE001
        log.warning("agentglow disabled: %s", e)
    return dict(_tracing)


def tracing_status() -> dict:
    return dict(_tracing)
