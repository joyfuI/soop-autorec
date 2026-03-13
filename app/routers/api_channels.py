from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request, status

from app.models import channel as channel_model
from app.schemas.channel import ChannelCreate, ChannelRead, ChannelUpdate

router = APIRouter(prefix="/api/channels", tags=["channels"])


@router.get("", response_model=list[ChannelRead])
async def api_list_channels(request: Request) -> list[dict]:
    settings = request.app.state.settings
    return channel_model.list_channels(settings)


@router.get("/{channel_id}", response_model=ChannelRead)
async def api_get_channel(request: Request, channel_id: int) -> dict:
    settings = request.app.state.settings
    channel = channel_model.get_channel(settings, channel_id)
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="채널을 찾을 수 없습니다.",
        )
    return channel


@router.post("", response_model=ChannelRead, status_code=status.HTTP_201_CREATED)
async def api_create_channel(request: Request, payload: ChannelCreate) -> dict:
    settings = request.app.state.settings
    stream_password = (
        payload.stream_password_enc.strip() if payload.stream_password_enc else None
    ) or None

    try:
        return channel_model.create_channel(
            settings,
            user_id=payload.user_id.strip(),
            display_name=payload.display_name.strip() if payload.display_name else None,
            enabled=payload.enabled,
            output_template=payload.output_template.strip() if payload.output_template else None,
            stream_password_enc=stream_password,
            preferred_quality=payload.preferred_quality.strip() or "best",
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="동일한 user_id 채널이 이미 존재합니다.",
        ) from exc


@router.put("/{channel_id}", response_model=ChannelRead)
async def api_update_channel(request: Request, channel_id: int, payload: ChannelUpdate) -> dict:
    settings = request.app.state.settings
    stream_password = (
        payload.stream_password_enc.strip() if payload.stream_password_enc else None
    ) or None

    updated = channel_model.update_channel(
        settings,
        channel_id,
        display_name=payload.display_name.strip() if payload.display_name else None,
        enabled=payload.enabled,
        output_template=payload.output_template.strip() if payload.output_template else None,
        stream_password_enc=stream_password,
        preferred_quality=payload.preferred_quality.strip() or "best",
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="채널을 찾을 수 없습니다.",
        )
    return updated


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_channel(request: Request, channel_id: int) -> None:
    settings = request.app.state.settings
    deleted = channel_model.delete_channel(settings, channel_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="채널을 찾을 수 없습니다.",
        )
