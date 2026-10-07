from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import Settings
from app.db import connect, database_ping
from app.models import event_log as event_log_model
from app.services.health import build_health_report
from app.utils.asyncio import run_blocking
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
        db_ok=await run_blocking(database_ping, settings),
    )
    return report.to_dict()


@router.get("/status")
async def api_status(request: Request) -> dict:
    state = request.app.state.supervisor.state
    (
        event_log_size,
        event_log_mtime_ns,
        recording_max_id,
        channel_dashboard_cursor,
    ) = await _get_stream_db_cursor(request)
    return {
        "running": state.running,
        "iteration_count": state.iteration_count,
        "last_probe_at": state.last_probe_at,
        "last_iteration_finished_at": state.last_iteration_finished_at,
        "last_channel_count": state.last_channel_count,
        "active_recorder_count": state.active_recorder_count,
        "last_error": state.last_error,
        "event_log_size": event_log_size,
        "event_log_mtime_ns": event_log_mtime_ns,
        "recording_max_id": recording_max_id,
        "channel_dashboard_cursor": channel_dashboard_cursor,
    }


def _fetch_stream_db_cursor(settings: Settings) -> tuple[int, int, int, str]:
    event_log_size, event_log_mtime_ns = event_log_model.get_event_log_cursor(settings)

    with connect(settings) as conn:
        recording_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM recordings"
        ).fetchone()
        channel_rows = conn.execute(
            """
            SELECT
              id,
              user_id,
              display_name,
              enabled,
              last_status,
              last_broad_no,
              last_probe_at
            FROM channels
            ORDER BY id ASC
            """
        ).fetchall()

    recording_max_id = int(recording_row["max_id"]) if recording_row is not None else 0
    channel_dashboard_cursor = _build_channel_dashboard_cursor(channel_rows)
    return (
        event_log_size,
        event_log_mtime_ns,
        recording_max_id,
        channel_dashboard_cursor,
    )


def _build_channel_dashboard_cursor(channel_rows) -> str:
    hasher = hashlib.sha256()
    for row in channel_rows:
        hasher.update(str(row["id"]).encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(row["user_id"] or "").encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(row["display_name"] or "").encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(int(row["enabled"] or 0)).encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(row["last_status"] or "").encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(row["last_broad_no"] or "").encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(row["last_probe_at"] or "").encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


async def _get_stream_db_cursor(request: Request) -> tuple[int, int, int, str]:
    app_state = request.app.state
    cached = app_state.stream_db_cursor
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    async with app_state.stream_db_cursor_lock:
        cached = app_state.stream_db_cursor
        if cached is None or time.monotonic() >= cached[0]:
            cursor = await run_blocking(_fetch_stream_db_cursor, app_state.settings)
            cached = (time.monotonic() + STREAM_POLL_INTERVAL_SEC, cursor)
            app_state.stream_db_cursor = cached
        return cached[1]


async def _build_stream_state_key(request: Request) -> tuple:
    cursor = await _get_stream_db_cursor(request)
    return (request.app.state.supervisor.state.active_recorder_count, *cursor)


@router.get("/stream")
async def api_stream(request: Request) -> StreamingResponse:
    async def event_generator():
        try:
            last_state_key = await _build_stream_state_key(request)
        except Exception:  # pragma: no cover
            logger.exception("Failed to build initial stream state key.")
            last_state_key = None

        last_heartbeat_at = time.monotonic()

        while True:
            if await request.is_disconnected():
                break

            await asyncio.sleep(STREAM_POLL_INTERVAL_SEC)

            try:
                state_key = await _build_stream_state_key(request)
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
