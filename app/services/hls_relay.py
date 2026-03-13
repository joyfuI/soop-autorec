from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

PLAYLIST_MEDIA_TYPE = "application/vnd.apple.mpegurl"
USER_AGENT = "soop-autorec-relay/1.0"
URI_ATTR_PATTERN = re.compile(r'URI="([^"]+)"')


@dataclass
class RelaySession:
    token: str
    proxy_url: str | None


class HlsRelayManager:
    def __init__(self) -> None:
        self._sessions: dict[str, RelaySession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, *, proxy_url: str | None) -> str:
        token = secrets.token_urlsafe(18)
        session = RelaySession(
            token=token,
            proxy_url=(proxy_url or "").strip() or None,
        )
        async with self._lock:
            self._sessions[token] = session
        return token

    async def remove_session(self, token: str) -> None:
        async with self._lock:
            self._sessions.pop(token, None)

    async def fetch_playlist(
        self,
        *,
        token: str,
        upstream_url: str,
        relay_base_url: str,
    ) -> tuple[str, str]:
        session = await self._require_session(token)
        response = await self._request_upstream(upstream_url, proxy_url=session.proxy_url)
        content_type = response.headers.get("content-type") or PLAYLIST_MEDIA_TYPE
        text = response.text
        rewritten = self._rewrite_playlist(
            body=text,
            upstream_url=str(response.url),
            token=token,
            relay_base_url=relay_base_url,
        )
        return rewritten, content_type

    async def fetch_key(
        self,
        *,
        token: str,
        upstream_url: str,
    ) -> tuple[bytes, str]:
        session = await self._require_session(token)
        response = await self._request_upstream(upstream_url, proxy_url=session.proxy_url)
        content_type = response.headers.get("content-type") or "application/octet-stream"
        return response.content, content_type

    def build_playlist_url(self, *, relay_base_url: str, token: str, upstream_url: str) -> str:
        encoded = self.encode_upstream_url(upstream_url)
        return f"{relay_base_url.rstrip('/')}/api/relay/hls/{token}/playlist.m3u8?u={encoded}"

    def build_key_url(self, *, relay_base_url: str, token: str, upstream_url: str) -> str:
        encoded = self.encode_upstream_url(upstream_url)
        return f"{relay_base_url.rstrip('/')}/api/relay/hls/{token}/key?u={encoded}"

    def encode_upstream_url(self, url: str) -> str:
        return quote(url, safe="")

    def decode_upstream_url(self, encoded: str) -> str:
        url = unquote(encoded).strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("유효하지 않은 upstream URL입니다.")
        return url

    async def _require_session(self, token: str) -> RelaySession:
        async with self._lock:
            session = self._sessions.get(token)
        if session is None:
            raise KeyError("릴레이 세션을 찾을 수 없습니다.")
        return session

    async def _request_upstream(self, url: str, *, proxy_url: str | None) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT}
        if proxy_url:
            for proxy_kw in ("proxy", "proxies"):
                try:
                    async with httpx.AsyncClient(
                        **{proxy_kw: proxy_url},
                        follow_redirects=True,
                        timeout=12.0,
                        headers=headers,
                    ) as client:
                        response = await client.get(url)
                        response.raise_for_status()
                        return response
                except TypeError:
                    continue
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"upstream 요청에 실패했습니다: {exc}") from exc
            raise RuntimeError("현재 런타임은 릴레이 요청에서 HTTP 프록시를 지원하지 않습니다.")

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=12.0,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response
        except httpx.HTTPError as exc:
            raise RuntimeError(f"upstream 요청에 실패했습니다: {exc}") from exc

    def _rewrite_playlist(
        self,
        *,
        body: str,
        upstream_url: str,
        token: str,
        relay_base_url: str,
    ) -> str:
        output_lines: list[str] = []
        base_url = upstream_url

        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                output_lines.append(raw_line)
                continue

            if line.startswith("#EXT-X-KEY") or line.startswith("#EXT-X-SESSION-KEY"):
                output_lines.append(
                    self._rewrite_tag_uri(
                        raw_line,
                        base_url=base_url,
                        transform=lambda absolute: self.build_key_url(
                            relay_base_url=relay_base_url,
                            token=token,
                            upstream_url=absolute,
                        ),
                    )
                )
                continue

            if line.startswith("#EXT-X-MAP"):
                output_lines.append(
                    self._rewrite_tag_uri(
                        raw_line,
                        base_url=base_url,
                        transform=lambda absolute: absolute,
                    )
                )
                continue

            if line.startswith("#"):
                output_lines.append(raw_line)
                continue

            absolute_url = urljoin(base_url, line)
            if self._looks_like_playlist(absolute_url):
                output_lines.append(
                    self.build_playlist_url(
                        relay_base_url=relay_base_url,
                        token=token,
                        upstream_url=absolute_url,
                    )
                )
            else:
                output_lines.append(absolute_url)

        trailing_newline = "\n" if body.endswith("\n") else ""
        return "\n".join(output_lines) + trailing_newline

    def _rewrite_tag_uri(
        self,
        line: str,
        *,
        base_url: str,
        transform: Callable[[str], str],
    ) -> str:
        match = URI_ATTR_PATTERN.search(line)
        if match is None:
            return line
        raw_uri = match.group(1)
        absolute = urljoin(base_url, raw_uri)
        rewritten = transform(absolute)
        return f"{line[:match.start(1)]}{rewritten}{line[match.end(1):]}"

    def _looks_like_playlist(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return path.endswith(".m3u8") or path.endswith(".m3u")
