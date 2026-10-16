"""GET /api/alerts (NYISO grid alerts via Anchor) and POST/DELETE /api/alerts/demo (stage-only simulated alert)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from grid import alerts

router = APIRouter()


class DemoReq(BaseModel):
    type: str = "thunderstorm"


@router.get("/api/alerts")
async def get_alerts():
    return await asyncio.to_thread(alerts.get_grid_alerts)


@router.post("/api/alerts/demo")
async def set_demo(req: DemoReq | None = None):
    try:
        out = alerts.set_demo((req or DemoReq()).type)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {**out, "alerts": await asyncio.to_thread(alerts.get_grid_alerts)}


@router.delete("/api/alerts/demo")
async def clear_demo():
    return alerts.clear_demo()
