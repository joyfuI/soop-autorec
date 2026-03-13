from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.models import settings as settings_model

router = APIRouter(prefix="/api/settings", tags=["settings"])

class AuthSettingsUpdate(BaseModel):
    username: str | None = None
    password: str | None = None
    cookies_txt_path: str | None = None
    clear_password: bool = False


class ProxySettingsUpdate(BaseModel):
    proxy_url: str | None = None


@router.get("")
async def api_list_settings(request: Request) -> dict[str, str]:
    app_settings = request.app.state.settings
    values = settings_model.list_settings(app_settings)
    if settings_model.SOOP_PASSWORD_KEY in values:
        values[settings_model.SOOP_PASSWORD_KEY] = "***"
    if settings_model.CONTROL_PROXY_URL_KEY in values:
        values[settings_model.CONTROL_PROXY_URL_KEY] = "***"
    return values


@router.get("/auth")
async def api_get_auth_settings(request: Request) -> dict:
    app_settings = request.app.state.settings
    auth = settings_model.get_auth_settings(app_settings)
    return {
        "username": auth["username"],
        "has_password": auth["has_password"],
        "cookies_txt_path": auth["cookies_txt_path"],
    }


@router.put("/auth")
async def api_update_auth_settings(request: Request, payload: AuthSettingsUpdate) -> dict:
    app_settings = request.app.state.settings
    try:
        auth = settings_model.update_auth_settings(
            app_settings,
            username=payload.username,
            password=payload.password,
            cookies_txt_path=payload.cookies_txt_path,
            clear_password=payload.clear_password,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {
        "username": auth["username"],
        "has_password": auth["has_password"],
        "cookies_txt_path": auth["cookies_txt_path"],
    }


@router.get("/proxy")
async def api_get_proxy_settings(request: Request) -> dict:
    app_settings = request.app.state.settings
    proxy = settings_model.get_proxy_settings(app_settings)
    return {"proxy_url": proxy["proxy_url"]}


@router.put("/proxy")
async def api_update_proxy_settings(request: Request, payload: ProxySettingsUpdate) -> dict:
    app_settings = request.app.state.settings
    try:
        proxy = settings_model.update_proxy_settings(
            app_settings,
            proxy_url=payload.proxy_url,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {"proxy_url": proxy["proxy_url"]}
