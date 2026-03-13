from __future__ import annotations

from fastapi import APIRouter, Request

from app.models import recording as recording_model

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


@router.get("")
async def api_list_recordings(request: Request, limit: int = 20) -> list[dict]:
    settings = request.app.state.settings
    safe_limit = max(1, min(limit, 200))
    return recording_model.list_recent_recordings(settings, limit=safe_limit)
