from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.models import channel as channel_model
from app.models import dashboard as dashboard_model
from app.models import event_log as event_log_model
from app.models import recording as recording_model
from app.models import settings as settings_model
from app.utils.time import format_datetime_for_display, format_datetime_iso_offset

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_datetime"] = format_datetime_for_display
templates.env.filters["fmt_iso_offset"] = format_datetime_iso_offset
router = APIRouter(tags=["ui"])
CHANNEL_TAB_KEYS = {"channel", "auth", "proxy"}
logger = logging.getLogger(__name__)


async def _delayed_process_exit(delay_sec: float = 0.3) -> None:
    await asyncio.sleep(delay_sec)
    os._exit(0)


async def _shutdown_supervisor_and_exit(
    supervisor,
    *,
    recorder_stop_reason: str,
    delay_sec: float = 0.3,
) -> None:
    try:
        await supervisor.stop(recorder_stop_reason=recorder_stop_reason)
    except Exception:  # pragma: no cover
        logger.exception("재시작 준비 중 supervisor 종료에 실패했습니다.")
    await asyncio.sleep(delay_sec)
    os._exit(0)


def _resolve_return_path(value: str | None) -> str:
    normalized = (value or "").strip()
    if normalized in {"/", "/channels"}:
        return normalized
    return "/"


def _resolve_channel_tab(tab: str | None) -> str | None:
    normalized = (tab or "").strip().lower()
    if normalized in CHANNEL_TAB_KEYS:
        return normalized
    return None


def _build_redirect(
    path: str,
    *,
    message: str | None = None,
    error: str | None = None,
    tab: str | None = None,
) -> RedirectResponse:
    query: dict[str, str] = {}
    if message:
        query["message"] = message
    if error:
        query["error"] = error

    url = path
    if query:
        url = f"{path}?{urlencode(query)}"

    tab_key = _resolve_channel_tab(tab)
    if path == "/channels" and tab_key is not None:
        url = f"{url}#tab-{tab_key}"

    return RedirectResponse(url=url, status_code=303)


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    settings = request.app.state.settings
    supervisor_state = request.app.state.supervisor.state

    summary = dashboard_model.fetch_dashboard_summary(settings)
    channels = channel_model.list_channels(settings)
    recent_events = event_log_model.list_recent_event_logs(settings, limit=12)
    channel_name_by_id = {
        int(channel["id"]): str(channel.get("display_name") or channel.get("user_id") or "")
        for channel in channels
    }
    for event in recent_events:
        channel_id = event.get("channel_id")
        try:
            normalized_channel_id = int(channel_id) if channel_id is not None else None
        except (TypeError, ValueError):
            normalized_channel_id = None

        event["channel_name"] = (
            channel_name_by_id.get(normalized_channel_id)
            if normalized_channel_id is not None
            else None
        )
    recent_recordings = recording_model.list_recent_recordings(settings, limit=12)
    active_recorder_count = request.app.state.supervisor.recorder.active_count

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "status": supervisor_state,
            "settings": settings,
            "summary": summary,
            "channels": channels,
            "recent_events": recent_events,
            "recent_recordings": recent_recordings,
            "message": message,
            "error": error,
            "active_recorder_count": active_recorder_count,
        },
    )


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    settings = request.app.state.settings
    channels = channel_model.list_channels(settings)
    auth_settings = settings_model.get_auth_settings(settings)
    proxy_settings = settings_model.get_proxy_settings(settings)
    active_recorder_count = request.app.state.supervisor.recorder.active_count

    return templates.TemplateResponse(
        "channels.html",
        {
            "request": request,
            "channels": channels,
            "message": message,
            "error": error,
            "auth_settings": auth_settings,
            "proxy_settings": proxy_settings,
            "active_recorder_count": active_recorder_count,
        },
    )


@router.post("/dashboard/channels/{channel_id}/stop")
async def stop_dashboard_channel_recording(
    request: Request,
    channel_id: int,
) -> RedirectResponse:
    settings = request.app.state.settings
    channel = channel_model.get_channel(settings, channel_id)
    if channel is None:
        return _build_redirect("/", error="채널을 찾을 수 없습니다.")

    stopped = await request.app.state.supervisor.recorder.stop_recording(
        channel_id,
        reason="manual_dashboard_stop",
    )
    if not stopped:
        return _build_redirect(
            "/",
            error=f"현재 중단할 녹화가 없습니다: {channel['user_id']}",
        )

    broad_no_raw = channel.get("last_broad_no")
    try:
        hold_broad_no = int(broad_no_raw) if broad_no_raw is not None else None
    except (TypeError, ValueError):
        hold_broad_no = None

    if hold_broad_no is None:
        active_recording = recording_model.get_active_recording_for_channel(settings, channel_id)
        if active_recording is not None:
            try:
                hold_broad_no = int(active_recording.get("broad_no"))
            except (TypeError, ValueError):
                hold_broad_no = None

    request.app.state.supervisor.hold_manual_stop_for_broadcast(
        channel_id,
        hold_broad_no,
    )

    return _build_redirect(
        "/",
        message=f"녹화 중지를 요청했습니다: {channel['user_id']}",
    )


@router.post("/dashboard/channels/{channel_id}/retry")
async def retry_dashboard_channel(
    request: Request,
    channel_id: int,
) -> RedirectResponse:
    settings = request.app.state.settings
    channel = channel_model.get_channel(settings, channel_id)
    if channel is None:
        return _build_redirect("/", error="채널을 찾을 수 없습니다.")

    status = str(channel.get("last_status") or "")
    if status not in {"standby_no_stream", "error", "online"}:
        return _build_redirect(
            "/",
            error=f"재시도 가능한 상태가 아닙니다: {channel['user_id']}",
        )

    request.app.state.supervisor.clear_manual_stop_hold(channel_id)
    request.app.state.supervisor.request_manual_record(channel_id)
    return _build_redirect(
        "/",
        message=f"재시도를 요청했습니다: {channel['user_id']}",
    )


@router.post("/channels")
async def create_channel(
    request: Request,
    user_id: str = Form(...),
    display_name: str = Form(default=""),
    enabled: str | None = Form(default=None),
    output_template: str = Form(default=""),
    stream_password: str = Form(default=""),
    preferred_quality: str = Form(default="best"),
    tab: str = Form(default="channel"),
) -> RedirectResponse:
    settings = request.app.state.settings
    tab_key = _resolve_channel_tab(tab) or "channel"

    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        return _build_redirect("/channels", error="user_id는 필수입니다.", tab=tab_key)

    try:
        stream_password_value = stream_password.strip() or None

        channel_model.create_channel(
            settings,
            user_id=normalized_user_id,
            display_name=display_name.strip() or None,
            enabled=enabled == "on",
            output_template=output_template.strip() or None,
            stream_password=stream_password_value,
            preferred_quality=preferred_quality.strip() or "best",
        )
        return _build_redirect(
            "/channels",
            message=f"채널을 추가했습니다: {normalized_user_id}",
            tab=tab_key,
        )
    except sqlite3.IntegrityError:
        return _build_redirect("/channels", error="이미 존재하는 user_id입니다.", tab=tab_key)


@router.post("/channels/{channel_id}/update")
async def update_channel(
    request: Request,
    channel_id: int,
    display_name: str = Form(default=""),
    enabled: str | None = Form(default=None),
    output_template: str = Form(default=""),
    stream_password: str = Form(default=""),
    preferred_quality: str = Form(default=""),
    tab: str = Form(default="channel"),
) -> RedirectResponse:
    settings = request.app.state.settings
    tab_key = _resolve_channel_tab(tab) or "channel"
    channel = channel_model.get_channel(settings, channel_id)
    if channel is None:
        return _build_redirect("/channels", error="채널을 찾을 수 없습니다.", tab=tab_key)

    stream_password_value = stream_password.strip() or None
    next_enabled = bool(channel["enabled"]) if enabled is None else enabled == "on"

    updated = channel_model.update_channel(
        settings,
        channel_id,
        display_name=display_name.strip() or None,
        enabled=next_enabled,
        output_template=output_template.strip() or None,
        stream_password=stream_password_value,
        preferred_quality=preferred_quality.strip() or str(channel["preferred_quality"] or "best"),
    )
    if updated is None:
        return _build_redirect("/channels", error="채널을 찾을 수 없습니다.", tab=tab_key)

    return _build_redirect(
        "/channels",
        message=f"채널 정보를 수정했습니다: {channel['user_id']}",
        tab=tab_key,
    )


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(
    request: Request,
    channel_id: int,
    tab: str = Form(default="channel"),
) -> RedirectResponse:
    settings = request.app.state.settings
    tab_key = _resolve_channel_tab(tab) or "channel"
    channel = channel_model.get_channel(settings, channel_id)
    if channel is None:
        return _build_redirect("/channels", error="채널을 찾을 수 없습니다.", tab=tab_key)

    next_enabled = not bool(channel["enabled"])
    channel_model.update_channel(
        settings,
        channel_id,
        display_name=channel["display_name"],
        enabled=next_enabled,
        output_template=channel["output_template"],
        stream_password=channel["stream_password"],
        preferred_quality=channel["preferred_quality"],
    )

    label = "활성화" if next_enabled else "비활성화"
    return _build_redirect(
        "/channels",
        message=f"채널 {channel['user_id']}을(를) {label}했습니다.",
        tab=tab_key,
    )


@router.post("/channels/{channel_id}/delete")
async def delete_channel(
    request: Request,
    channel_id: int,
    tab: str = Form(default="channel"),
) -> RedirectResponse:
    settings = request.app.state.settings
    tab_key = _resolve_channel_tab(tab) or "channel"
    channel = channel_model.get_channel(settings, channel_id)
    if channel is None:
        return _build_redirect("/channels", error="채널을 찾을 수 없습니다.", tab=tab_key)

    channel_model.delete_channel(settings, channel_id)
    return _build_redirect(
        "/channels",
        message=f"채널을 삭제했습니다: {channel['user_id']}",
        tab=tab_key,
    )


@router.post("/settings/auth")
async def update_auth_settings(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    clear_password: str | None = Form(default=None),
    cookies_txt_path: str = Form(default=""),
    tab: str = Form(default="auth"),
) -> RedirectResponse:
    settings = request.app.state.settings
    tab_key = _resolve_channel_tab(tab) or "auth"

    try:
        settings_model.update_auth_settings(
            settings,
            username=username,
            password=password if password.strip() else None,
            cookies_txt_path=cookies_txt_path,
            clear_password=clear_password == "on",
        )
    except RuntimeError as exc:
        return _build_redirect("/channels", error=str(exc), tab=tab_key)

    return _build_redirect("/channels", message="전역 인증 설정을 저장했습니다.", tab=tab_key)


@router.post("/settings/proxy")
async def update_proxy_settings(
    request: Request,
    proxy_url: str = Form(default=""),
    tab: str = Form(default="proxy"),
) -> RedirectResponse:
    settings = request.app.state.settings
    tab_key = _resolve_channel_tab(tab) or "proxy"
    try:
        settings_model.update_proxy_settings(settings, proxy_url=proxy_url)
    except ValueError as exc:
        return _build_redirect("/channels", error=str(exc), tab=tab_key)
    return _build_redirect("/channels", message="프록시 설정을 저장했습니다.", tab=tab_key)


@router.post("/system/restart")
async def restart_system(
    request: Request,
    background_tasks: BackgroundTasks,
    force: str | None = Form(default=None),
    return_to: str = Form(default="/"),
    tab: str = Form(default="channel"),
) -> RedirectResponse:
    target_path = _resolve_return_path(return_to)
    tab_key = _resolve_channel_tab(tab)
    active_recorder_count = request.app.state.supervisor.recorder.active_count
    force_restart = force == "1"

    if active_recorder_count > 0 and not force_restart:
        return _build_redirect(
            target_path,
            error=(
                f"현재 {active_recorder_count}개 채널이 녹화 중입니다. "
                "강제 재시작하려면 확인 후 다시 요청(force=1)해주세요."
            ),
            tab=tab_key,
        )

    if active_recorder_count > 0 and force_restart:
        background_tasks.add_task(
            _shutdown_supervisor_and_exit,
            request.app.state.supervisor,
            recorder_stop_reason="force_restart",
        )
        return _build_redirect(
            target_path,
            message=(
                f"녹화 중 {active_recorder_count}개 채널을 중단하고 재시작합니다. "
                "잠시 후 다시 접속해주세요."
            ),
            tab=tab_key,
        )

    background_tasks.add_task(_delayed_process_exit)
    return _build_redirect(
        target_path,
        message="재시작을 요청했습니다. 잠시 후 다시 접속해주세요.",
        tab=tab_key,
    )

