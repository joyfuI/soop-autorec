from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx


class ProbeStatus(StrEnum):
    LIVE = "LIVE"
    OFFLINE = "OFFLINE"
    PROBE_ERROR = "PROBE_ERROR"


@dataclass
class ProbeResult:
    status: ProbeStatus
    payload: dict[str, Any] | None = None
    error: str | None = None
    http_status: int | None = None


def build_probe_url(user_id: str) -> str:
    return f"https://api-channel.sooplive.co.kr/v1.1/channel/{user_id}/home/section/broad"


async def probe_channel(
    user_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_sec: float = 8.0,
) -> ProbeResult:
    url = build_probe_url(user_id)
    created_client = client is None
    local_client = client or httpx.AsyncClient(timeout=timeout_sec)

    try:
        response = await local_client.get(url)
        if response.status_code >= 400:
            return ProbeResult(
                status=ProbeStatus.PROBE_ERROR,
                error=f"HTTP 오류 {response.status_code}",
                http_status=response.status_code,
            )

        body = response.text.strip()
        if not body:
            return ProbeResult(status=ProbeStatus.OFFLINE, http_status=response.status_code)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            return ProbeResult(
                status=ProbeStatus.PROBE_ERROR,
                error=f"JSON 응답 파싱 실패: {exc}",
                http_status=response.status_code,
            )

        if isinstance(data, dict) and data:
            return ProbeResult(
                status=ProbeStatus.LIVE,
                payload=data,
                http_status=response.status_code,
            )

        return ProbeResult(status=ProbeStatus.OFFLINE, http_status=response.status_code)
    except httpx.HTTPError as exc:
        return ProbeResult(status=ProbeStatus.PROBE_ERROR, error=f"요청 실패: {exc}")
    finally:
        if created_client:
            await local_client.aclose()
