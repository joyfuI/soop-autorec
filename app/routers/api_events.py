from __future__ import annotations

from fastapi import APIRouter, Request

from app.models import event_log as event_log_model
from app.utils.asyncio import run_blocking

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("")
async def api_list_events(request: Request, limit: int = 50) -> list[dict]:
    settings = request.app.state.settings
    safe_limit = max(1, min(limit, 500))
    return await run_blocking(event_log_model.list_recent_event_logs, settings, limit=safe_limit)
