from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CHANNEL_API_URL = "https://live.sooplive.com/afreeca/player_live_api.php"
PRIVATE_AUTH_URL = "https://live.sooplive.com/api/private_auth.php"
LOGIN_URL = "https://login.sooplive.com/app/LoginAction.php"
LOGIN_REQUIRED_RESULT = -6
LOGIN_RESULT_OK = 1
CLOUDFRONT_COOKIE_NAMES = {
    "CloudFront-Signature",
    "CloudFront-Key-Pair-Id",
    "CloudFront-Policy",
}

PLAYBACK_ORIGIN = "https://play.sooplive.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


class SubscriptionPlusResolveError(ValueError):
    """Raised when a subscription plus stream is detected but cannot be authorized."""


@dataclass
class SubscriptionPlusStream:
    url: str
    headers: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


def has_subscription_plus_hint(payload: dict[str, Any]) -> bool:
    try:
        return int(payload.get("subscriptionOnly") or 0) > 0
    except TypeError, ValueError:
        return False


async def resolve_subscription_plus_stream(
    *,
    user_id: str,
    broad_no: int,
    stream_password: str | None,
    preferred_quality: str,
    cookies_txt_path: str | None,
    username: str | None,
    password: str | None,
    proxy_url: str | None,
    timeout_sec: float = 20.0,
) -> SubscriptionPlusStream | None:
    cookies = _load_cookie_file(cookies_txt_path)
    headers = _build_browser_headers(user_id=user_id, broad_no=broad_no)
    auth_source = "cookies_txt" if cookies else "none"
    login_attempted = False

    if not cookies and username and password:
        login_attempted = True
        login_cookies = await _create_direct_login_cookies(
            username=username,
            password=password,
            headers=headers,
            timeout_sec=timeout_sec,
        )
        if login_cookies:
            cookies.update(login_cookies)
            auth_source = "username_password"

    for _ in range(2):
        client_kwargs = _build_client_kwargs(
            cookies=cookies,
            headers=headers,
            proxy_url=proxy_url,
            timeout_sec=timeout_sec,
        )
        async with httpx.AsyncClient(**client_kwargs) as client:
            channel_info = await _fetch_channel_info(
                client,
                user_id=user_id,
                broad_no=broad_no,
                stream_password=stream_password,
                preferred_quality=preferred_quality,
            )

            if _channel_result(channel_info) != 1:
                if username and password and not login_attempted:
                    login_attempted = True
                    login_cookies = await _create_direct_login_cookies(
                        username=username,
                        password=password,
                        headers=headers,
                        timeout_sec=timeout_sec,
                    )
                    if login_cookies:
                        cookies.update(login_cookies)
                        auth_source = "username_password"
                        continue
                raise _build_channel_info_error(
                    channel_info,
                    username_password_configured=bool(username and password),
                    login_attempted=login_attempted,
                )

            if not _is_subscription_plus_channel(channel_info):
                raise SubscriptionPlusResolveError(
                    "구독플러스 방송으로 감지됐지만 전용 TS 재생 정보가 없습니다. "
                    "시청 권한이 있는 cookies.txt 또는 username/password를 확인해주세요."
                )

            ts_url = str(channel_info.get("TS") or "").strip()
            if not ts_url:
                raise SubscriptionPlusResolveError(
                    "구독플러스 방송으로 감지됐지만 TS 재생 URL이 없습니다."
                )

            private_auth = await _authorize_private_stream(
                client,
                user_id=user_id,
                broad_no=broad_no,
                ts_url=ts_url,
            )
            private_result = _parse_int(private_auth.get("result"))
            if private_result != 1:
                if username and password and not login_attempted:
                    login_attempted = True
                    login_cookies = await _create_direct_login_cookies(
                        username=username,
                        password=password,
                        headers=headers,
                        timeout_sec=timeout_sec,
                    )
                    if login_cookies:
                        cookies.update(login_cookies)
                        auth_source = "username_password"
                        continue
                raise SubscriptionPlusResolveError("구독플러스 방송 CDN 인증에 실패했습니다.")

            client_cookie_names = _cookie_names(client.cookies)
            missing = sorted(CLOUDFRONT_COOKIE_NAMES - client_cookie_names)
            if missing:
                raise SubscriptionPlusResolveError(
                    "구독플러스 방송 CDN 인증 쿠키가 없습니다: " + ", ".join(missing)
                )

            record_headers = _build_record_headers(
                client.cookies,
                user_id=user_id,
                broad_no=broad_no,
            )
            return SubscriptionPlusStream(
                url=ts_url,
                headers=record_headers,
                metadata={
                    "resolver": "soop_subscription_plus",
                    "auth_source": auth_source,
                    "ts_type": str(channel_info.get("TS_TYPE") or ""),
                    "p_min_tier": channel_info.get("P_MIN_TIER"),
                    "tier_type": channel_info.get("TIER_TYPE"),
                    "sub_pay_count": channel_info.get("SUB_PAY_CNT"),
                    "private_auth_result": private_result,
                },
            )

    raise SubscriptionPlusResolveError("구독플러스 방송 인증 재시도에 실패했습니다.")


def _build_client_kwargs(
    *,
    cookies: dict[str, str],
    headers: dict[str, str],
    proxy_url: str | None,
    timeout_sec: float,
) -> dict[str, Any]:
    client_kwargs: dict[str, Any] = {
        "timeout": timeout_sec,
        "follow_redirects": False,
        "cookies": cookies,
        "headers": headers,
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    return client_kwargs


async def _create_direct_login_cookies(
    *,
    username: str,
    password: str,
    headers: dict[str, str],
    timeout_sec: float,
) -> dict[str, str]:
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        follow_redirects=False,
        headers=headers,
    ) as client:
        if not await _login(client, username=username, password=password):
            return {}
        return _cookies_to_dict(client.cookies)


def _build_channel_info_error(
    channel_info: dict[str, Any],
    *,
    username_password_configured: bool,
    login_attempted: bool,
) -> SubscriptionPlusResolveError:
    channel_result = _channel_result(channel_info)
    if channel_result == LOGIN_REQUIRED_RESULT:
        if username_password_configured and login_attempted:
            return SubscriptionPlusResolveError(
                "구독플러스 방송 재생 정보 확인에 로그인이 필요하지만 "
                "username/password로 로그인 쿠키를 만들지 못했습니다."
            )
        return SubscriptionPlusResolveError(
            "구독플러스 방송 재생 정보 확인에 로그인이 필요합니다. "
            "cookies.txt를 설정하거나 username/password를 저장해주세요."
        )

    hint = "시청 권한이 있는 cookies.txt 또는 username/password를 확인해주세요."
    if username_password_configured and login_attempted:
        hint = "username/password 로그인 후에도 권한 확인에 실패했습니다. 시청 권한을 확인해주세요."

    return SubscriptionPlusResolveError(
        "구독플러스 방송 재생 정보 확인에 실패했습니다. "
        f"SOOP RESULT={channel_result}. {hint}"
    )


def _cookies_to_dict(
    cookies: httpx.Cookies,
    *,
    exclude_names: set[str] | None = None,
) -> dict[str, str]:
    excluded = exclude_names or set()
    return {
        cookie.name: cookie.value
        for cookie in cookies.jar
        if cookie.name not in excluded
    }


def _cookie_names(cookies: httpx.Cookies) -> set[str]:
    return {cookie.name for cookie in cookies.jar}


def _load_cookie_file(cookies_txt_path: str | None) -> dict[str, str]:
    path_raw = (cookies_txt_path or "").strip()
    if not path_raw:
        return {}

    path = Path(path_raw)
    if not path.exists():
        return {}

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    cookies: dict[str, str] = {}
    now = int(time.time())
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        name = ""
        value = ""
        expires_at = 0
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[5].strip()
                value = parts[6].strip()
                try:
                    expires_at = int(parts[4])
                except ValueError:
                    expires_at = 0
        elif "=" in line:
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()

        if name in CLOUDFRONT_COOKIE_NAMES:
            continue

        if name and (expires_at <= 0 or expires_at > now):
            cookies[name] = value

        if len(cookies) >= 200:
            break

    return cookies


async def _fetch_channel_info(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    broad_no: int,
    stream_password: str | None,
    preferred_quality: str,
) -> dict[str, Any]:
    response = await client.post(
        CHANNEL_API_URL,
        params={"bjid": user_id},
        data={
            "bid": user_id,
            "bno": str(broad_no),
            "type": "live",
            "pwd": stream_password or "",
            "player_type": "html5",
            "stream_type": "common",
            "quality": _map_player_quality(preferred_quality),
            "mode": "landing",
            "from_api": "0",
            "is_revive": "false",
        },
    )
    if response.status_code >= 400:
        raise SubscriptionPlusResolveError(
            f"SOOP 재생 정보 요청에 실패했습니다: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise SubscriptionPlusResolveError("SOOP 재생 정보 응답이 JSON이 아닙니다.") from exc

    channel_info = data.get("CHANNEL") if isinstance(data, dict) else None
    if not isinstance(channel_info, dict):
        raise SubscriptionPlusResolveError("SOOP 재생 정보 응답에 CHANNEL이 없습니다.")
    return channel_info


async def _authorize_private_stream(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    broad_no: int,
    ts_url: str,
) -> dict[str, Any]:
    response = await client.post(
        PRIVATE_AUTH_URL,
        data={
            "type": "sub_timeshift",
            "strm_id": user_id,
            "broad_no": str(broad_no),
            "url": ts_url,
        },
    )
    if response.status_code >= 400:
        raise SubscriptionPlusResolveError(
            f"구독플러스 CDN 인증 요청에 실패했습니다: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise SubscriptionPlusResolveError("구독플러스 CDN 인증 응답이 JSON이 아닙니다.") from exc

    if not isinstance(data, dict):
        raise SubscriptionPlusResolveError("구독플러스 CDN 인증 응답 형식이 올바르지 않습니다.")
    return data


async def _login(client: httpx.AsyncClient, *, username: str, password: str) -> bool:
    response = await client.post(
        LOGIN_URL,
        data={
            "szWork": "login",
            "szType": "json",
            "szUid": username,
            "szPassword": password,
            "isSaveId": "true",
            "isSavePw": "false",
            "isSaveJoin": "false",
            "isLoginRetain": "Y",
        },
    )
    if response.status_code >= 400:
        return False

    try:
        data = response.json()
    except ValueError:
        return False

    return _parse_int(data.get("RESULT")) == LOGIN_RESULT_OK if isinstance(data, dict) else False


def _build_browser_headers(*, user_id: str, broad_no: int) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Origin": PLAYBACK_ORIGIN,
        "Referer": _build_subscription_referer(user_id=user_id, broad_no=broad_no),
    }


def _build_record_headers(
    cookies: httpx.Cookies,
    *,
    user_id: str,
    broad_no: int,
) -> dict[str, str]:
    cookie_header = "; ".join(
        f"{name}={value}" for name, value in _cookies_to_dict(cookies).items()
    )
    return {
        "Cookie": cookie_header,
        "User-Agent": USER_AGENT,
        "Referer": _build_subscription_referer(user_id=user_id, broad_no=broad_no),
        "Origin": PLAYBACK_ORIGIN,
    }


def _build_subscription_referer(*, user_id: str, broad_no: int) -> str:
    return f"{PLAYBACK_ORIGIN}/{user_id}/{broad_no}"


def _is_subscription_plus_channel(channel_info: dict[str, Any]) -> bool:
    ts_url = str(channel_info.get("TS") or "").strip()
    ts_type = str(channel_info.get("TS_TYPE") or "").strip()
    return ts_type == "2" and "playlist.m3u8" in ts_url and "live-tm-sub-" in ts_url


def _map_player_quality(preferred_quality: str) -> str:
    value = preferred_quality.strip().lower()
    return {
        "worst": "sd",
        "360p": "sd",
        "540p": "hd",
        "720p": "hd4k",
        "1080p": "original",
        "best": "HD",
    }.get(value, preferred_quality.strip() or "HD")


def _channel_result(channel_info: dict[str, Any]) -> int | None:
    return _parse_int(channel_info.get("RESULT"))


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None
