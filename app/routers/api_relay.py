from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response

router = APIRouter(prefix="/api/relay", tags=["relay"])


@router.get("/hls/{token}/playlist.m3u8")
async def api_relay_playlist(
    request: Request,
    token: str,
    u: str = Query(..., description="인코딩된 upstream playlist URL"),
) -> Response:
    relay = request.app.state.relay_manager
    try:
        upstream_url = relay.decode_upstream_url(u)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        body, content_type = await relay.fetch_playlist(
            token=token,
            upstream_url=upstream_url,
            relay_base_url=str(request.base_url).rstrip("/"),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return Response(content=body, media_type=content_type)


@router.get("/hls/{token}/key")
async def api_relay_key(
    request: Request,
    token: str,
    u: str = Query(..., description="인코딩된 upstream key URL"),
) -> Response:
    relay = request.app.state.relay_manager
    try:
        upstream_url = relay.decode_upstream_url(u)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        content, content_type = await relay.fetch_key(
            token=token,
            upstream_url=upstream_url,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return Response(content=content, media_type=content_type)
