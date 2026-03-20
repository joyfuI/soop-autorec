from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import Settings
from app.db import connect, database_ping
from app.models import event_log as event_log_model
from app.services.health import build_health_report
from app.services.poller import SupervisorState
from app.utils.time import now_utc

router = APIRouter(prefix="/api/system", tags=["system"])
logger = logging.getLogger(__name__)
STREAM_POLL_INTERVAL_SEC = 1.0
STREAM_HEARTBEAT_INTERVAL_SEC = 15.0


@router.get("/health")
async def api_health(request: Request) -> dict:
    settings = request.app.state.settings
    supervisor = request.app.state.supervisor

    report = build_health_report(
        state=supervisor.state,
        db_ok=database_ping(settings),
    )
    return report.to_dict()


@router.get("/status")
async def api_status(request: Request) -> dict:
    settings = request.app.state.settings
    state = request.app.state.supervisor.state
    (
        event_log_size,
        event_log_mtime_ns,
        recording_max_id,
        channel_max_updated_at,
        channel_count,
    ) = _fetch_stream_db_cursor(settings)
    return {
        "running": state.running,
        "iteration_count": state.iteration_count,
        "last_probe_at": state.last_probe_at,
        "last_iteration_finished_at": state.last_iteration_finished_at,
        "last_channel_count": state.last_channel_count,
        "last_live_count": state.last_live_count,
        "last_probe_error_count": state.last_probe_error_count,
        "active_recorder_count": state.active_recorder_count,
        "last_error": state.last_error,
        "event_log_size": event_log_size,
        "event_log_mtime_ns": event_log_mtime_ns,
        "recording_max_id": recording_max_id,
        "channel_max_updated_at": channel_max_updated_at,
        "channel_count": channel_count,
    }


def _fetch_stream_db_cursor(settings: Settings) -> tuple[int, int, int, str, int]:
    event_log_size, event_log_mtime_ns = event_log_model.get_event_log_cursor(settings)

    with connect(settings) as conn:
        recording_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM recordings"
        ).fetchone()
        channel_row = conn.execute(
            "SELECT COALESCE(MAX(updated_at), '') AS max_updated_at, COUNT(*) AS channel_count "
            "FROM channels"
        ).fetchone()

    recording_max_id = int(recording_row["max_id"]) if recording_row is not None else 0
    channel_max_updated_at = (
        str(channel_row["max_updated_at"]) if channel_row is not None else ""
    )
    channel_count = int(channel_row["channel_count"]) if channel_row is not None else 0
    return (
        event_log_size,
        event_log_mtime_ns,
        recording_max_id,
        channel_max_updated_at,
        channel_count,
    )


def _build_stream_state_key(settings: Settings, state: SupervisorState) -> tuple:
    (
        event_log_size,
        event_log_mtime_ns,
        recording_max_id,
        channel_max_updated_at,
        channel_count,
    ) = _fetch_stream_db_cursor(settings)
    return (
        state.last_probe_error_count,
        state.active_recorder_count,
        event_log_size,
        event_log_mtime_ns,
        recording_max_id,
        channel_max_updated_at,
        channel_count,
    )


@router.get("/stream")
async def api_stream(request: Request) -> StreamingResponse:
    settings = request.app.state.settings
    state = request.app.state.supervisor.state

    async def event_generator():
        try:
            last_state_key = _build_stream_state_key(settings, state)
        except Exception:  # pragma: no cover
            logger.exception("Failed to build initial stream state key.")
            last_state_key = None

        last_heartbeat_at = time.monotonic()

        while True:
            if await request.is_disconnected():
                break

            await asyncio.sleep(STREAM_POLL_INTERVAL_SEC)

            try:
                state_key = _build_stream_state_key(settings, state)
            except Exception:  # pragma: no cover
                logger.exception("Failed to build stream state key.")
                continue

            if state_key != last_state_key:
                last_state_key = state_key
                payload = {
                    "type": "dashboard_changed",
                    "at": now_utc().isoformat(),
                }
                yield f"event: dashboard\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_heartbeat_at = time.monotonic()
                continue

            if time.monotonic() - last_heartbeat_at >= STREAM_HEARTBEAT_INTERVAL_SEC:
                yield ": keep-alive\n\n"
                last_heartbeat_at = time.monotonic()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=headers,
    )
