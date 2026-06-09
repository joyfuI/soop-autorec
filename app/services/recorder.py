from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models import channel as channel_model
from app.models import event_log as event_log_model
from app.models import recording as recording_model
from app.models import settings as settings_model
from app.services.filename_renderer import FilenameRenderer
from app.services.playback_url import build_playback_url
from app.services.soop_subscription import (
    SubscriptionPlusHlsProxy,
    SubscriptionPlusResolveError,
    create_direct_soop_login_cookies,
    has_subscription_plus_hint,
    load_soop_cookie_file,
    resolve_subscription_plus_stream,
)
from app.utils.sanitize import sanitize_filename_component
from app.utils.time import now_utc

logger = logging.getLogger(__name__)
FORCE_KILL_DELAY_SEC = 30
DEFAULT_OUTPUT_TEMPLATE = "${displayName}/${YY}${MM}${DD} ${title} [${broadNo}].mp4"
MAX_FINAL_PATH_CANDIDATES = 500


class StreamUrlNotReadyError(ValueError):
    """Raised when streamlink cannot provide a playable stream URL yet."""


@dataclass
class EnsureRecordingResult:
    active: bool
    started: bool
    recording_id: int
    error: str | None = None
    standby_no_stream: bool = False
    finalizing: bool = False


@dataclass
class RecordingHandle:
    channel_id: int
    recording_id: int
    user_id: str
    broad_no: int
    temp_path: Path
    remux_temp_path: Path
    final_path: Path
    process: asyncio.subprocess.Process
    watch_task: asyncio.Task[None] | None = None
    subscription_proxy: SubscriptionPlusHlsProxy | None = None
    stop_requested: bool = False
    stop_reason: str | None = None
    capture_done: asyncio.Event = field(default_factory=asyncio.Event)


class RecorderManager:
    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings
        self._filename_renderer = FilenameRenderer(settings.timezone)
        self._handles: dict[int, RecordingHandle] = {}
        self._finalize_tasks: set[asyncio.Task[None]] = set()
        self._finalizing_broadcasts: set[tuple[str, int]] = set()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._handles)

    @property
    def finalizing_count(self) -> int:
        return len(self._finalizing_broadcasts)

    async def ensure_recording(
        self,
        *,
        channel: dict[str, Any],
        recording: dict[str, Any],
        payload: dict[str, Any],
    ) -> EnsureRecordingResult:
        channel_id = int(channel["id"])
        broad_no = int(recording["broad_no"])
        user_id = str(recording.get("user_id") or channel["user_id"])

        async with self._lock:
            existing_handle = self._handles.get(channel_id)
            finalizing_same_broadcast = (
                user_id,
                broad_no,
            ) in self._finalizing_broadcasts

        if existing_handle is not None and existing_handle.broad_no == broad_no:
            recording_model.update_recording_with_probe_payload(
                self.settings,
                existing_handle.recording_id,
                payload,
            )
            process_active = existing_handle.process.returncode is None
            return EnsureRecordingResult(
                active=process_active,
                started=False,
                recording_id=existing_handle.recording_id,
                finalizing=not process_active,
            )

        if finalizing_same_broadcast:
            recording_id = int(recording["id"])
            recording_model.update_recording_with_probe_payload(
                self.settings,
                recording_id,
                payload,
            )
            return EnsureRecordingResult(
                active=False,
                started=False,
                recording_id=recording_id,
                finalizing=True,
            )

        if existing_handle is not None and existing_handle.broad_no != broad_no:
            await self.stop_recording(channel_id, reason="new_broadcast_detected")
            await existing_handle.capture_done.wait()
            watch_task = existing_handle.watch_task
            if watch_task is not None and watch_task.done():
                try:
                    watch_task.result()
                except Exception:  # pragma: no cover
                    logger.exception(
                        "Previous recording capture cleanup failed for channel_id=%s",
                        channel_id,
                    )

        return await self._start_recording(channel=channel, recording=recording, payload=payload)

    async def _wait_for_finalize_tasks(self) -> None:
        while True:
            async with self._lock:
                tasks = [task for task in self._finalize_tasks if not task.done()]

            if not tasks:
                return

            await asyncio.gather(*tasks, return_exceptions=True)

    async def _mark_capture_done(self, handle: RecordingHandle) -> None:
        async with self._lock:
            current = self._handles.get(handle.channel_id)
            if current is handle:
                self._handles.pop(handle.channel_id, None)
            self._finalizing_broadcasts.add((handle.user_id, handle.broad_no))

        handle.capture_done.set()

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

        await self._wait_for_finalize_tasks()

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
        broad_title = str(payload.get("broadTitle") or "제목없음")

        broad_start_at = self._parse_broad_start(recording.get("broad_start_at"))
        relative_output = self._render_relative_output_path(
            template=str(output_template),
            display_name=str(channel.get("display_name") or user_id),
            user_id=user_id,
            title=broad_title,
            broad_no=broad_no,
            broad_start_at=broad_start_at,
        )

        final_path = Path(self.settings.output_root_dir) / relative_output
        final_path.parent.mkdir(parents=True, exist_ok=True)

        temp_root = Path(self.settings.temp_root_dir)
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_path = temp_root / self._build_temp_filename(user_id=user_id, broad_no=broad_no)
        remux_temp_path = temp_root / self._build_remux_temp_filename(
            user_id=user_id,
            broad_no=broad_no,
        )

        quality = str(channel.get("preferred_quality") or "best")
        resolver_name = "streamlink"
        resolver_metadata: dict[str, Any] = {}
        subscription_proxy: SubscriptionPlusHlsProxy | None = None
        try:
            proxy_settings = settings_model.get_proxy_settings(self.settings)
            resolver_proxy_url = str(proxy_settings.get("proxy_url") or "").strip() or None
            auth = settings_model.get_auth_credentials(self.settings)
            subscription_stream = None
            if has_subscription_plus_hint(payload):
                subscription_stream = await resolve_subscription_plus_stream(
                    user_id=user_id,
                    broad_no=broad_no,
                    stream_password=str(channel.get("stream_password") or "").strip() or None,
                    preferred_quality=quality,
                    cookies_txt_path=str(auth.get("cookies_txt_path") or "").strip() or None,
                    username=str(auth.get("username") or "").strip() or None,
                    password=str(auth.get("password") or "") or None,
                    proxy_url=resolver_proxy_url,
                )

            if subscription_stream is not None:
                subscription_proxy = SubscriptionPlusHlsProxy(
                    stream=subscription_stream,
                    user_id=user_id,
                    broad_no=broad_no,
                    proxy_url=resolver_proxy_url,
                )
                resolver_name = "soop_subscription_plus"
                resolver_metadata = {
                    **subscription_stream.metadata,
                    "local_hls_proxy": True,
                }
            else:
                auth_args, auth_metadata = await self._build_auth_args(
                    channel,
                    auth=auth,
                    user_id=user_id,
                    broad_no=broad_no,
                    proxy_url=resolver_proxy_url,
                )
                resolver_metadata.update(auth_metadata)
                resolve_cmd = self._build_resolve_stream_url_cmd(
                    playback_url=playback_url,
                    quality=quality,
                    auth_args=auth_args,
                    proxy_url=resolver_proxy_url,
                )
                stream_url = await self._resolve_stream_url(resolve_cmd)
        except SubscriptionPlusResolveError as exc:
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
        except StreamUrlNotReadyError as exc:
            standby_message = str(exc)
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="standby_no_stream",
                error_message=standby_message,
            )
            return EnsureRecordingResult(
                active=False,
                started=False,
                recording_id=recording_id,
                error=standby_message,
                standby_no_stream=True,
            )
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
            if subscription_proxy is not None:
                # External proxy is used only by resolver/auth flows.
                # Subscription media traffic goes through a local HLS proxy and then direct to CDN.
                stream_url = subscription_proxy.start()
            cmd = self._build_record_cmd(
                input_url=stream_url,
                temp_path=temp_path,
            )
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except SubscriptionPlusResolveError as exc:
            if subscription_proxy is not None:
                subscription_proxy.stop()
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
        except OSError as exc:
            if subscription_proxy is not None:
                subscription_proxy.stop()
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
            message=f"스트림 URL로 녹화를 시작했습니다. 제목: {broad_title}",
            payload={
                "playback_url": playback_url,
                "quality": quality,
                "resolver": resolver_name,
                "proxy_enabled_for_resolve": resolver_proxy_url is not None,
                "temp_path": str(temp_path),
                "remux_temp_path": str(remux_temp_path),
                "final_path": str(final_path),
                **resolver_metadata,
            },
        )

        handle = RecordingHandle(
            channel_id=channel_id,
            recording_id=recording_id,
            user_id=user_id,
            broad_no=broad_no,
            temp_path=temp_path,
            remux_temp_path=remux_temp_path,
            final_path=final_path,
            process=process,
            subscription_proxy=subscription_proxy,
        )

        async with self._lock:
            self._handles[channel_id] = handle
            watch_task = asyncio.create_task(
                self._watch_process(handle),
                name=f"watch-recording-{channel_id}",
            )
            handle.watch_task = watch_task
            self._finalize_tasks.add(watch_task)
            watch_task.add_done_callback(self._finalize_tasks.discard)

        return EnsureRecordingResult(
            active=True,
            started=True,
            recording_id=recording_id,
        )

    async def _watch_process(self, handle: RecordingHandle) -> None:
        channel_id = handle.channel_id
        stderr_text = ""
        try:
            if handle.process.stderr is not None:
                _, stderr_bytes = await handle.process.communicate()
                stderr_text = stderr_bytes.decode("utf-8", errors="ignore")
            else:
                await handle.process.wait()

            if handle.subscription_proxy is not None:
                handle.subscription_proxy.stop()

            exit_code = handle.process.returncode
            stopped_at = now_utc().isoformat()
            stderr_tail = self._tail_text(stderr_text)

            recording_model.update_recording_fields(
                self.settings,
                handle.recording_id,
                recording_stopped_at=stopped_at,
            )

            await self._mark_capture_done(handle)

            remux_result, resolved_final_path = await self._run_remux(
                recording_id=handle.recording_id,
                temp_path=handle.temp_path,
                remux_temp_path=handle.remux_temp_path,
                final_path=handle.final_path,
                stop_requested=handle.stop_requested,
                stop_reason=handle.stop_reason,
                recorder_exit_code=exit_code,
                recorder_stderr=stderr_tail,
            )

            if remux_result:
                requested_final_path = handle.final_path
                handle.final_path = resolved_final_path
                completed_filename = handle.final_path.name
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
                    message=f"녹화 및 remux가 완료되었습니다. 파일명: {completed_filename}",
                    payload=payload,
                )
            else:
                error_message = "녹화가 실패 상태로 종료되었습니다."
                recovery_path: str | None = None
                latest_status = "failed"
                latest_recording = recording_model.get_recording_by_id(
                    self.settings,
                    handle.recording_id,
                )
                if latest_recording is not None:
                    latest_error = str(latest_recording.get("error_message") or "").strip()
                    if latest_error:
                        error_message = latest_error
                    latest_status = str(latest_recording.get("status") or "failed")
                    recovery_path_raw = latest_recording.get("temp_path")
                    if recovery_path_raw is not None:
                        recovery_path = str(recovery_path_raw).strip() or None

                payload: dict[str, Any] = {"exit_code": exit_code, "stderr_tail": stderr_tail}
                if recovery_path:
                    payload["recovery_path"] = recovery_path
                if latest_recording is not None:
                    final_path_value = str(latest_recording.get("final_path") or "").strip()
                    if final_path_value:
                        payload["final_path"] = final_path_value

                if latest_status == "partial":
                    partial_message = "녹화가 partial 상태로 종료되었습니다."
                    if recovery_path:
                        partial_message = f"{partial_message} 복구 파일: {recovery_path}"
                    event_log_model.add_event_log(
                        self.settings,
                        level="warning",
                        event_type="record_partial",
                        channel_id=handle.channel_id,
                        recording_id=handle.recording_id,
                        message=partial_message,
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
                        payload=payload,
                    )
                channel_model.update_last_error(
                    self.settings,
                    handle.channel_id,
                    last_error=error_message,
                )
        except Exception:
            logger.exception(
                "Recording finalization failed for channel_id=%s recording_id=%s",
                channel_id,
                handle.recording_id,
            )
            raise
        finally:
            if not handle.capture_done.is_set():
                await self._mark_capture_done(handle)
            async with self._lock:
                self._finalizing_broadcasts.discard((handle.user_id, handle.broad_no))

    async def _run_remux(
        self,
        *,
        recording_id: int,
        temp_path: Path,
        remux_temp_path: Path,
        final_path: Path,
        stop_requested: bool,
        stop_reason: str | None,
        recorder_exit_code: int | None,
        recorder_stderr: str,
    ) -> tuple[bool, Path]:
        source_exists = temp_path.exists() and temp_path.stat().st_size > 0
        if not source_exists:
            message = "녹화 프로세스가 종료됐지만 녹화 데이터가 생성되지 않았습니다."
            if recorder_stderr:
                message = f"{message} stderr: {recorder_stderr}"
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                temp_path=None,
                error_message=message,
            )
            return False, final_path

        recording_model.update_recording_fields(
            self.settings,
            recording_id,
            temp_path=str(temp_path),
            final_path=str(final_path),
        )

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

            remux_output_path = self._build_remux_output_candidate(
                remux_temp_path=remux_temp_path,
                index=index,
            )
            remux_output_path.parent.mkdir(parents=True, exist_ok=True)
            remux_output_path.unlink(missing_ok=True)

            ffmpeg_cmd = [
                self.settings.ffmpeg_binary,
                "-y",
                "-i",
                str(temp_path),
                "-c",
                "copy",
                str(remux_output_path),
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
                and remux_output_path.exists()
                and remux_output_path.stat().st_size > 0
            ):
                try:
                    candidate_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(remux_output_path, candidate_path)
                except OSError as exc:
                    reason = (
                        f"remux 완료 파일 이동에 실패했습니다: {exc}. "
                        f"복구 파일: {remux_output_path}"
                    )
                    recording_model.update_recording_fields(
                        self.settings,
                        recording_id,
                        status="partial",
                        temp_path=str(remux_output_path),
                        final_path=str(candidate_path),
                        ffmpeg_exit_code=ffmpeg_exit_code,
                        file_size_bytes=remux_output_path.stat().st_size,
                        error_message=reason,
                    )
                    return False, final_path

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
                for cleanup_path in (temp_path, remux_output_path):
                    try:
                        cleanup_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("임시 파일 삭제에 실패했습니다: %s", cleanup_path)
                return True, candidate_path

            recovery_path = self._resolve_recovery_path(
                remux_output_path=remux_output_path,
                temp_path=temp_path,
            )
            failure_status = "partial" if recovery_path is not None else "failed"
            reason = "ffmpeg remux에 실패했습니다"
            if ffmpeg_tail:
                reason = f"{reason}: {ffmpeg_tail}"
            if stop_requested and stop_reason:
                reason = f"{reason} (stop_reason={stop_reason})"
            if recorder_exit_code not in (0, None):
                reason = f"{reason}; record_exit_code={recorder_exit_code}"
            if recovery_path is not None:
                reason = f"{reason}; 복구 파일: {recovery_path}"

            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status=failure_status,
                temp_path=str(recovery_path) if recovery_path is not None else None,
                ffmpeg_exit_code=ffmpeg_exit_code,
                file_size_bytes=recovery_path.stat().st_size if recovery_path is not None else None,
                final_path=str(candidate_path),
                error_message=reason,
            )
            return False, candidate_path

        recovery_path = self._resolve_recovery_path(
            remux_output_path=remux_temp_path,
            temp_path=temp_path,
        )

        reason = (
            "ffmpeg remux 출력 경로가 모두 사용 중이라 저장에 실패했습니다. "
            f"확인한 후보 수: {MAX_FINAL_PATH_CANDIDATES}"
        )
        if recovery_path is not None:
            reason = f"{reason}; 복구 파일: {recovery_path}"
        recording_model.update_recording_fields(
            self.settings,
            recording_id,
            status="partial" if recovery_path is not None else "failed",
            temp_path=str(recovery_path) if recovery_path is not None else None,
            file_size_bytes=recovery_path.stat().st_size if recovery_path is not None else None,
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

    def _build_remux_output_candidate(self, *, remux_temp_path: Path, index: int) -> Path:
        if index <= 0:
            return remux_temp_path
        return remux_temp_path.with_name(
            f"{remux_temp_path.stem} ({index}){remux_temp_path.suffix}"
        )

    def _resolve_recovery_path(
        self,
        *,
        remux_output_path: Path,
        temp_path: Path,
    ) -> Path | None:
        for candidate in (remux_output_path, temp_path):
            try:
                if candidate.exists() and candidate.stat().st_size > 0:
                    return candidate
            except OSError:
                continue
        return None

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
            part for part in re.split(r"[/\\]+", rendered) if part and part not in {".", ".."}
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

    def _build_remux_temp_filename(self, *, user_id: str, broad_no: int) -> str:
        user = sanitize_filename_component(user_id, fallback="unknown")
        stamp = now_utc().strftime("%Y%m%d_%H%M%S")
        return f"{user}_{broad_no}_{stamp}.mp4"

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
            raise StreamUrlNotReadyError(reason)

        if not stream_url.startswith(("http://", "https://")):
            raise ValueError("streamlink resolver가 HTTP URL이 아닌 값을 반환했습니다.")

        return stream_url

    def _build_record_cmd(
        self,
        *,
        input_url: str,
        temp_path: Path,
    ) -> list[str]:
        return [
            self.settings.ffmpeg_binary,
            "-y",
            "-i",
            input_url,
            "-c",
            "copy",
            str(temp_path),
        ]

    async def _build_auth_args(
        self,
        channel: dict[str, Any],
        *,
        auth: dict[str, str | None],
        user_id: str,
        broad_no: int,
        proxy_url: str | None,
    ) -> tuple[list[str], dict[str, Any]]:
        args: list[str] = []
        metadata: dict[str, Any] = {}
        auth_sources: list[str] = []

        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "")
        cookies_txt_path = str(auth.get("cookies_txt_path") or "").strip()
        if cookies_txt_path:
            cookie_args = self._build_http_cookie_args(load_soop_cookie_file(cookies_txt_path))
            if cookie_args:
                args.extend(cookie_args)
                auth_sources.append("cookies_txt")

        if username and password:
            if proxy_url:
                login_cookies = await create_direct_soop_login_cookies(
                    username=username,
                    password=password,
                    user_id=user_id,
                    broad_no=broad_no,
                )
                if login_cookies:
                    args.extend(self._build_http_cookie_args(login_cookies))
                    auth_sources.append("direct_login_cookies")
                    metadata["direct_login_cookie_count"] = len(login_cookies)
                else:
                    logger.warning(
                        "SOOP direct login did not produce cookies for %s/%s",
                        user_id,
                        broad_no,
                    )
            else:
                args.extend(["--soop-username", username, "--soop-password", password])
                auth_sources.append("streamlink_username_password")

        stream_password = str(channel.get("stream_password") or "").strip()
        if stream_password:
            args.extend(["--soop-stream-password", stream_password])

        if auth_sources:
            metadata["auth_sources"] = auth_sources

        return args, metadata

    def _build_http_cookie_args(self, cookies: dict[str, str]) -> list[str]:
        args: list[str] = []
        for index, (name, value) in enumerate(cookies.items()):
            if index >= 200:
                break
            if name:
                args.extend(["--http-cookie", f"{name}={value}"])

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
