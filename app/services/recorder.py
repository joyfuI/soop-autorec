from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models import event_log as event_log_model
from app.models import recording as recording_model
from app.models import settings as settings_model
from app.services.filename_renderer import FilenameRenderer
from app.services.hls_relay import HlsRelayManager
from app.services.playback_url import build_playback_url
from app.utils.sanitize import sanitize_filename_component
from app.utils.time import now_utc

logger = logging.getLogger(__name__)
FORCE_KILL_DELAY_SEC = 30
DEFAULT_OUTPUT_TEMPLATE = "${displayName}/${YYMMDD} ${title} [${broadNo}].mp4"
MAX_FINAL_PATH_CANDIDATES = 500


@dataclass
class EnsureRecordingResult:
    active: bool
    started: bool
    recording_id: int
    error: str | None = None


@dataclass
class RecordingHandle:
    channel_id: int
    recording_id: int
    user_id: str
    broad_no: int
    temp_path: Path
    final_path: Path
    process: asyncio.subprocess.Process
    watch_task: asyncio.Task[None]
    relay_session_token: str
    stop_requested: bool = False
    stop_reason: str | None = None


class RecorderManager:
    def __init__(
        self,
        settings: Settings,
        *,
        relay_manager: HlsRelayManager,
        relay_base_url: str,
    ) -> None:
        self.settings = settings
        self.relay_manager = relay_manager
        self.relay_base_url = relay_base_url.rstrip("/")
        self._filename_renderer = FilenameRenderer(settings.timezone)
        self._handles: dict[int, RecordingHandle] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._handles)

    async def ensure_recording(
        self,
        *,
        channel: dict[str, Any],
        recording: dict[str, Any],
        payload: dict[str, Any],
    ) -> EnsureRecordingResult:
        channel_id = int(channel["id"])
        broad_no = int(recording["broad_no"])

        async with self._lock:
            existing_handle = self._handles.get(channel_id)

        if (
            existing_handle is not None
            and existing_handle.broad_no == broad_no
            and existing_handle.process.returncode is None
        ):
            recording_model.update_recording_with_probe_payload(
                self.settings,
                existing_handle.recording_id,
                payload,
            )
            return EnsureRecordingResult(
                active=True,
                started=False,
                recording_id=existing_handle.recording_id,
            )

        if existing_handle is not None and existing_handle.broad_no != broad_no:
            await self.stop_recording(channel_id, reason="new_broadcast_detected")
            try:
                await existing_handle.watch_task
            except Exception:  # pragma: no cover
                logger.exception(
                    "Previous recording cleanup failed for channel_id=%s",
                    channel_id,
                )

        return await self._start_recording(channel=channel, recording=recording, payload=payload)

    async def stop_recording(self, channel_id: int, *, reason: str) -> bool:
        async with self._lock:
            handle = self._handles.get(channel_id)

        if handle is None:
            return False

        if handle.stop_requested:
            return True

        handle.stop_requested = True
        handle.stop_reason = reason

        recording_model.update_recording_fields(
            self.settings,
            handle.recording_id,
            status="stopping",
            error_message=None,
        )
        event_log_model.add_event_log(
            self.settings,
            level="info",
            event_type="record_stop_requested",
            channel_id=handle.channel_id,
            recording_id=handle.recording_id,
            message="녹화 프로세스 중지를 요청했습니다.",
            payload={"reason": reason},
        )

        if handle.process.returncode is None:
            handle.process.terminate()
            asyncio.create_task(self._force_kill_if_needed(handle))

        return True

    async def stop_all(self, *, reason: str = "shutdown") -> None:
        async with self._lock:
            handles = list(self._handles.values())

        for handle in handles:
            await self.stop_recording(handle.channel_id, reason=reason)

        if handles:
            await asyncio.gather(*(handle.watch_task for handle in handles), return_exceptions=True)

    async def _start_recording(
        self,
        *,
        channel: dict[str, Any],
        recording: dict[str, Any],
        payload: dict[str, Any],
    ) -> EnsureRecordingResult:
        channel_id = int(channel["id"])
        recording_id = int(recording["id"])
        user_id = str(channel["user_id"])
        broad_no = int(recording["broad_no"])

        binary_error = self._validate_binaries()
        if binary_error is not None:
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                error_message=binary_error,
            )
            event_log_model.add_event_log(
                self.settings,
                level="error",
                event_type="record_start_failed",
                channel_id=channel_id,
                recording_id=recording_id,
                message=binary_error,
            )
            return EnsureRecordingResult(
                active=False,
                started=False,
                recording_id=recording_id,
                error=binary_error,
            )

        try:
            playback_url = build_playback_url(user_id)
        except ValueError as exc:
            error_message = str(exc)
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                error_message=error_message,
            )
            event_log_model.add_event_log(
                self.settings,
                level="error",
                event_type="record_start_failed",
                channel_id=channel_id,
                recording_id=recording_id,
                message=error_message,
            )
            return EnsureRecordingResult(
                active=False,
                started=False,
                recording_id=recording_id,
                error=error_message,
            )

        output_template = channel.get("output_template") or DEFAULT_OUTPUT_TEMPLATE

        broad_start_at = self._parse_broad_start(recording.get("broad_start_at"))
        relative_output = self._render_relative_output_path(
            template=str(output_template),
            display_name=str(channel.get("display_name") or user_id),
            user_id=user_id,
            title=str(payload.get("broadTitle") or "제목없음"),
            broad_no=broad_no,
            broad_start_at=broad_start_at,
        )

        final_path = Path(self.settings.output_root_dir) / relative_output
        final_path.parent.mkdir(parents=True, exist_ok=True)

        temp_root = Path(self.settings.temp_root_dir)
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_path = temp_root / self._build_temp_filename(user_id=user_id, broad_no=broad_no)

        quality = str(channel.get("preferred_quality") or "best")
        proxy_settings = settings_model.get_proxy_settings(self.settings)
        resolver_proxy_url = str(proxy_settings.get("proxy_url") or "").strip() or None
        try:
            auth_args = self._build_auth_args(channel)
            resolve_cmd = self._build_resolve_stream_url_cmd(
                playback_url=playback_url,
                quality=quality,
                auth_args=auth_args,
                proxy_url=resolver_proxy_url,
            )
            stream_url = await self._resolve_stream_url(resolve_cmd)
        except ValueError as exc:
            error_message = str(exc)
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                error_message=error_message,
            )
            event_log_model.add_event_log(
                self.settings,
                level="error",
                event_type="record_start_failed",
                channel_id=channel_id,
                recording_id=recording_id,
                message=error_message,
            )
            return EnsureRecordingResult(
                active=False,
                started=False,
                recording_id=recording_id,
                error=error_message,
            )

        # Proxy is used only for one-time stream URL resolution.
        # Ongoing playlist/key fetches during recording are direct.
        relay_session_token = await self.relay_manager.create_session(proxy_url=None)
        relay_input_url = self.relay_manager.build_playlist_url(
            relay_base_url=self.relay_base_url,
            token=relay_session_token,
            upstream_url=stream_url,
        )
        cmd = self._build_record_cmd(
            relay_input_url=relay_input_url,
            temp_path=temp_path,
        )

        recording_model.update_recording_with_probe_payload(self.settings, recording_id, payload)
        recording_model.update_recording_fields(
            self.settings,
            recording_id,
            status="starting",
            temp_path=str(temp_path),
            final_path=str(final_path),
            error_message=None,
            recording_started_at=None,
            recording_stopped_at=None,
            ffmpeg_exit_code=None,
            file_size_bytes=None,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            await self.relay_manager.remove_session(relay_session_token)
            error_message = f"녹화 프로세스 시작에 실패했습니다: {exc}"
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                error_message=error_message,
            )
            event_log_model.add_event_log(
                self.settings,
                level="error",
                event_type="record_start_failed",
                channel_id=channel_id,
                recording_id=recording_id,
                message=error_message,
            )
            return EnsureRecordingResult(
                active=False,
                started=False,
                recording_id=recording_id,
                error=error_message,
            )

        started_at = now_utc().isoformat()
        recording_model.update_recording_fields(
            self.settings,
            recording_id,
            status="recording",
            recording_started_at=started_at,
        )

        event_log_model.add_event_log(
            self.settings,
            level="info",
            event_type="record_start",
            channel_id=channel_id,
            recording_id=recording_id,
            message="중계 입력 URL로 녹화를 시작했습니다.",
            payload={
                "playback_url": playback_url,
                "quality": quality,
                "proxy_enabled_for_resolve": resolver_proxy_url is not None,
                "relay_input_url": relay_input_url,
                "temp_path": str(temp_path),
                "final_path": str(final_path),
            },
        )

        watch_task = asyncio.create_task(
            self._watch_process(channel_id),
            name=f"watch-recording-{channel_id}",
        )
        handle = RecordingHandle(
            channel_id=channel_id,
            recording_id=recording_id,
            user_id=user_id,
            broad_no=broad_no,
            temp_path=temp_path,
            final_path=final_path,
            process=process,
            watch_task=watch_task,
            relay_session_token=relay_session_token,
        )

        async with self._lock:
            self._handles[channel_id] = handle

        return EnsureRecordingResult(
            active=True,
            started=True,
            recording_id=recording_id,
        )

    async def _watch_process(self, channel_id: int) -> None:
        async with self._lock:
            handle = self._handles.get(channel_id)

        if handle is None:
            return

        stderr_text = ""
        if handle.process.stderr is not None:
            _, stderr_bytes = await handle.process.communicate()
            stderr_text = stderr_bytes.decode("utf-8", errors="ignore")
        else:
            await handle.process.wait()

        exit_code = handle.process.returncode
        stopped_at = now_utc().isoformat()
        stderr_tail = self._tail_text(stderr_text)

        recording_model.update_recording_fields(
            self.settings,
            handle.recording_id,
            recording_stopped_at=stopped_at,
        )

        await self.relay_manager.remove_session(handle.relay_session_token)

        remux_result, resolved_final_path = await self._run_remux(
            recording_id=handle.recording_id,
            temp_path=handle.temp_path,
            final_path=handle.final_path,
            stop_requested=handle.stop_requested,
            stop_reason=handle.stop_reason,
            recorder_exit_code=exit_code,
            recorder_stderr=stderr_tail,
        )

        if remux_result:
            requested_final_path = handle.final_path
            handle.final_path = resolved_final_path
            payload: dict[str, Any] = {"final_path": str(handle.final_path)}
            if handle.final_path != requested_final_path:
                payload["requested_final_path"] = str(requested_final_path)
                payload["renamed_due_to_collision"] = True
            event_log_model.add_event_log(
                self.settings,
                level="info",
                event_type="record_complete",
                channel_id=handle.channel_id,
                recording_id=handle.recording_id,
                message="녹화 및 remux가 완료되었습니다.",
                payload=payload,
            )
        else:
            event_log_model.add_event_log(
                self.settings,
                level="error",
                event_type="record_failed",
                channel_id=handle.channel_id,
                recording_id=handle.recording_id,
                message="녹화가 실패 상태로 종료되었습니다.",
                payload={"exit_code": exit_code, "stderr_tail": stderr_tail},
            )

        async with self._lock:
            current = self._handles.get(channel_id)
            if current is handle:
                self._handles.pop(channel_id, None)

    async def _run_remux(
        self,
        *,
        recording_id: int,
        temp_path: Path,
        final_path: Path,
        stop_requested: bool,
        stop_reason: str | None,
        recorder_exit_code: int | None,
        recorder_stderr: str,
    ) -> tuple[bool, Path]:
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            message = "녹화 프로세스가 종료됐지만 녹화 데이터가 생성되지 않았습니다."
            if recorder_stderr:
                message = f"{message} stderr: {recorder_stderr}"
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                error_message=message,
            )
            return False, final_path

        recording_model.update_recording_fields(
            self.settings,
            recording_id,
            status="remuxing",
            error_message=None,
        )

        for index in range(MAX_FINAL_PATH_CANDIDATES):
            candidate_path = self._build_final_output_candidate(base_path=final_path, index=index)
            if candidate_path.exists():
                continue

            ffmpeg_cmd = [
                self.settings.ffmpeg_binary,
                "-n",
                "-i",
                str(temp_path),
                "-c",
                "copy",
                str(candidate_path),
            ]

            try:
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                recording_model.update_recording_fields(
                    self.settings,
                    recording_id,
                    status="failed",
                    error_message=f"ffmpeg 실행에 실패했습니다: {exc}",
                )
                return False, final_path

            ffmpeg_stderr_bytes = b""
            if process.stderr is not None:
                _, ffmpeg_stderr_bytes = await process.communicate()
            else:
                await process.wait()

            ffmpeg_exit_code = process.returncode
            ffmpeg_stderr = ffmpeg_stderr_bytes.decode("utf-8", errors="ignore")
            ffmpeg_tail = self._tail_text(ffmpeg_stderr)

            if (
                ffmpeg_exit_code == 0
                and candidate_path.exists()
                and candidate_path.stat().st_size > 0
            ):
                file_size = candidate_path.stat().st_size
                recording_model.update_recording_fields(
                    self.settings,
                    recording_id,
                    status="completed",
                    ffmpeg_exit_code=ffmpeg_exit_code,
                    file_size_bytes=file_size,
                    final_path=str(candidate_path),
                    error_message=None,
                )
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("임시 파일 삭제에 실패했습니다: %s", temp_path)
                return True, candidate_path

            if self._is_ffmpeg_output_exists_error(ffmpeg_stderr):
                continue

            failure_status = (
                "partial"
                if candidate_path.exists() and candidate_path.stat().st_size > 0
                else "failed"
            )
            reason = "ffmpeg remux에 실패했습니다"
            if ffmpeg_tail:
                reason = f"{reason}: {ffmpeg_tail}"
            if stop_requested and stop_reason:
                reason = f"{reason} (stop_reason={stop_reason})"
            if recorder_exit_code not in (0, None):
                reason = f"{reason}; record_exit_code={recorder_exit_code}"

            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status=failure_status,
                ffmpeg_exit_code=ffmpeg_exit_code,
                final_path=str(candidate_path),
                error_message=reason,
            )
            return False, candidate_path

        reason = (
            "ffmpeg remux 출력 경로가 모두 사용 중이라 저장에 실패했습니다. "
            f"확인한 후보 수: {MAX_FINAL_PATH_CANDIDATES}"
        )
        recording_model.update_recording_fields(
            self.settings,
            recording_id,
            status="failed",
            error_message=reason,
        )
        return False, final_path

    async def _force_kill_if_needed(self, handle: RecordingHandle) -> None:
        await asyncio.sleep(FORCE_KILL_DELAY_SEC)
        if handle.process.returncode is None:
            handle.process.kill()

    def _validate_binaries(self) -> str | None:
        missing: list[str] = []
        for binary in (self.settings.streamlink_binary, self.settings.ffmpeg_binary):
            if shutil.which(binary) is None:
                missing.append(binary)

        if not missing:
            return None
        return f"필수 바이너리를 찾을 수 없습니다: {', '.join(missing)}"

    def _build_final_output_candidate(self, *, base_path: Path, index: int) -> Path:
        if index <= 0:
            return base_path
        return base_path.with_name(f"{base_path.stem} ({index}){base_path.suffix}")

    def _is_ffmpeg_output_exists_error(self, stderr_text: str) -> bool:
        normalized = stderr_text.lower()
        return "already exists" in normalized

    def _render_relative_output_path(
        self,
        *,
        template: str,
        display_name: str,
        user_id: str,
        title: str,
        broad_no: int,
        broad_start_at: datetime,
    ) -> Path:
        rendered = self._filename_renderer.render(
            template,
            display_name=display_name,
            user_id=user_id,
            title=title,
            broad_no=broad_no,
            broad_start_at=broad_start_at,
        )

        raw_parts = [
            part
            for part in re.split(r"[/\\]+", rendered)
            if part and part not in {".", ".."}
        ]
        if not raw_parts:
            raw_parts = [f"{sanitize_filename_component(user_id)}_{broad_no}.mp4"]

        safe_parts = [sanitize_filename_component(part) for part in raw_parts]
        relative_path = Path(*safe_parts)

        if relative_path.suffix.lower() != ".mp4":
            relative_path = relative_path.with_suffix(".mp4")

        return relative_path

    def _build_temp_filename(self, *, user_id: str, broad_no: int) -> str:
        user = sanitize_filename_component(user_id, fallback="unknown")
        stamp = now_utc().strftime("%Y%m%d_%H%M%S")
        return f"{user}_{broad_no}_{stamp}.mkv"

    def _build_resolve_stream_url_cmd(
        self,
        *,
        playback_url: str,
        quality: str,
        auth_args: list[str],
        proxy_url: str | None,
    ) -> list[str]:
        cmd = [
            self.settings.streamlink_binary,
            *auth_args,
        ]
        if proxy_url:
            cmd.extend(["--http-proxy", proxy_url])
        cmd.extend(["--stream-url", playback_url, quality])
        return cmd

    async def _resolve_stream_url(self, cmd: list[str]) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ValueError(f"streamlink resolver 실행에 실패했습니다: {exc}") from exc

        stdout_data, stderr_data = await process.communicate()
        stdout_text = stdout_data.decode("utf-8", errors="ignore")
        stderr_text = stderr_data.decode("utf-8", errors="ignore")
        stderr_tail = self._tail_text(stderr_text)

        stream_url = ""
        for line in stdout_text.splitlines():
            if line.strip():
                stream_url = line.strip()

        if process.returncode != 0 or not stream_url:
            reason = "streamlink로 스트림 URL 해석에 실패했습니다."
            if stderr_tail:
                reason = f"{reason} stderr: {stderr_tail}"
            raise ValueError(reason)

        if not stream_url.startswith(("http://", "https://")):
            raise ValueError("streamlink resolver가 HTTP URL이 아닌 값을 반환했습니다.")

        return stream_url

    def _build_record_cmd(
        self,
        *,
        relay_input_url: str,
        temp_path: Path,
    ) -> list[str]:
        return [
            self.settings.ffmpeg_binary,
            "-y",
            "-i",
            relay_input_url,
            "-c",
            "copy",
            str(temp_path),
        ]

    def _build_auth_args(self, channel: dict[str, Any]) -> list[str]:
        args: list[str] = []
        auth = settings_model.get_auth_credentials(self.settings)

        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "")
        if username and password:
            args.extend(["--soop-username", username, "--soop-password", password])

        stream_password = str(channel.get("stream_password_enc") or "").strip()
        if stream_password:
            args.extend(["--soop-stream-password", stream_password])

        cookies_txt_path = str(auth.get("cookies_txt_path") or "").strip()
        if cookies_txt_path:
            args.extend(self._build_cookie_args(cookies_txt_path))

        return args

    def _build_cookie_args(self, cookies_txt_path: str) -> list[str]:
        path = Path(cookies_txt_path)
        if not path.exists():
            return []

        cookies: list[str] = []
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            name = ""
            value = ""

            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 7:
                    name = parts[5].strip()
                    value = parts[6].strip()
            elif "=" in line:
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip()

            if name:
                cookies.append(f"{name}={value}")

            if len(cookies) >= 200:
                break

        args: list[str] = []
        for cookie in cookies:
            args.extend(["--http-cookie", cookie])

        return args

    def _parse_broad_start(self, broad_start_raw: Any) -> datetime:
        if isinstance(broad_start_raw, datetime):
            return broad_start_raw

        if isinstance(broad_start_raw, str) and broad_start_raw.strip():
            value = broad_start_raw.strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass

        return now_utc()

    def _tail_text(self, text: str, *, max_chars: int = 700) -> str:
        trimmed = text.strip()
        if len(trimmed) <= max_chars:
            return trimmed
        return trimmed[-max_chars:]
