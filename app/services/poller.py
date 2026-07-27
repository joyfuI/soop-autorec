from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.models import channel as channel_model
from app.models import event_log as event_log_model
from app.models import recording as recording_model
from app.services.recorder import EnsureRecordingResult, RecorderManager
from app.services.soop_probe import ProbeResult, ProbeStatus, probe_channel
from app.services.soop_subscription import has_subscription_plus_hint
from app.utils.time import now_utc

logger = logging.getLogger(__name__)
MAINTENANCE_INTERVAL = timedelta(hours=1)
RECORDINGS_RETENTION_DAYS = 90
MAX_CONCURRENT_RECORDING_STARTS = 4


@dataclass
class SupervisorState:
    started_at: datetime | None = None
    last_probe_at: datetime | None = None
    last_iteration_finished_at: datetime | None = None
    iteration_count: int = 0
    running: bool = False
    last_error: str | None = None
    last_channel_count: int = 0
    active_recorder_count: int = 0


@dataclass
class RecordingStartTask:
    recording_id: int
    broad_no: int
    task: asyncio.Task[None]


class Supervisor:
    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings
        self.state = SupervisorState()
        self.recorder = RecorderManager(settings)
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._last_maintenance_at: datetime | None = None
        self._force_probe_channel_ids: set[int] = set()
        self._manual_record_request_channel_ids: set[int] = set()
        self._manual_stop_hold_broad_no_by_channel_id: dict[int, int] = {}
        self._recording_start_tasks: dict[int, RecordingStartTask] = {}
        self._recording_start_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RECORDING_STARTS)

    async def start(self) -> None:
        if self.state.running:
            return

        interrupted_count = recording_model.mark_active_recordings_interrupted(self.settings)
        interrupted_recordings = recording_model.list_interrupted_recordings(self.settings)
        recovery_count = await self.recorder.recover_interrupted_recordings(
            interrupted_recordings
        )
        if interrupted_count > 0 or recovery_count > 0:
            event_log_model.add_event_log(
                self.settings,
                level="warning",
                event_type="startup_recovery",
                message=(
                    f"앱 시작 시 남아 있던 활성 녹화 {interrupted_count}건을 중단 처리하고, "
                    f"임시 파일 {recovery_count}건의 자동 복구를 예약했습니다."
                ),
                payload={
                    "interrupted_count": interrupted_count,
                    "recovery_count": recovery_count,
                },
            )

        self._run_maintenance(now_utc(), force=True)

        self._stop_event.clear()
        self._wake_event.clear()
        self._force_probe_channel_ids.clear()
        self._manual_record_request_channel_ids.clear()
        self._manual_stop_hold_broad_no_by_channel_id.clear()
        self._recording_start_tasks.clear()
        self.state.running = True
        self.state.started_at = now_utc()
        self._http_client = httpx.AsyncClient(timeout=8.0)
        self._task = asyncio.create_task(self._run(), name="supervisor-poller")

    async def stop(self, *, recorder_stop_reason: str = "app_shutdown") -> None:
        if not self.state.running:
            return

        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            await self._task

        await self._cancel_recording_start_tasks()
        await self.recorder.stop_all(reason=recorder_stop_reason)

        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

        self.state.active_recorder_count = self.recorder.active_count
        self.state.running = False

    def hold_manual_stop_for_broadcast(self, channel_id: int, broad_no: int | None) -> None:
        if broad_no is None:
            self._manual_stop_hold_broad_no_by_channel_id.pop(channel_id, None)
            return
        self._manual_stop_hold_broad_no_by_channel_id[channel_id] = broad_no

    def clear_manual_stop_hold(self, channel_id: int) -> None:
        self._manual_stop_hold_broad_no_by_channel_id.pop(channel_id, None)

    def request_manual_record(self, channel_id: int) -> None:
        self._manual_record_request_channel_ids.add(channel_id)
        self.request_force_probe(channel_id)

    def request_force_probe(self, channel_id: int) -> None:
        self._force_probe_channel_ids.add(channel_id)
        self._wake_event.set()

    async def _cancel_recording_start_tasks(self) -> None:
        tasks = [task_info.task for task_info in self._recording_start_tasks.values()]
        self._recording_start_tasks.clear()

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _get_recording_start_task(self, channel_id: int) -> RecordingStartTask | None:
        task_info = self._recording_start_tasks.get(channel_id)
        if task_info is None:
            return None
        if task_info.task.done():
            self._recording_start_tasks.pop(channel_id, None)
            return None
        return task_info

    def _schedule_recording_start(
        self,
        *,
        channel: dict,
        recording: dict,
        payload: dict,
        created: bool,
        prior_status: str,
        prior_broad_no: int | None,
        broad_no: int,
    ) -> bool:
        channel_id = int(channel["id"])
        if self._get_recording_start_task(channel_id) is not None:
            return False

        recording_id = int(recording["id"])
        task = asyncio.create_task(
            self._run_recording_start(
                channel=dict(channel),
                recording=dict(recording),
                payload=dict(payload),
                created=created,
                prior_status=prior_status,
                prior_broad_no=prior_broad_no,
                broad_no=broad_no,
            ),
            name=f"start-recording-{channel_id}-{recording_id}",
        )
        self._recording_start_tasks[channel_id] = RecordingStartTask(
            recording_id=recording_id,
            broad_no=broad_no,
            task=task,
        )
        task.add_done_callback(
            lambda done_task, done_channel_id=channel_id: self._discard_recording_start_task(
                done_channel_id,
                done_task,
            )
        )
        return True

    def _discard_recording_start_task(
        self,
        channel_id: int,
        done_task: asyncio.Task[None],
    ) -> None:
        current = self._recording_start_tasks.get(channel_id)
        if current is not None and current.task is done_task:
            self._recording_start_tasks.pop(channel_id, None)

    async def _run_recording_start(
        self,
        *,
        channel: dict,
        recording: dict,
        payload: dict,
        created: bool,
        prior_status: str,
        prior_broad_no: int | None,
        broad_no: int,
    ) -> None:
        channel_id = int(channel["id"])
        recording_id = int(recording["id"])

        try:
            async with self._recording_start_semaphore:
                ensure_result = await self.recorder.ensure_recording(
                    channel=channel,
                    recording=recording,
                    payload=payload,
                )

            next_status = self._status_from_ensure_result(ensure_result)

            if created and ensure_result.active:
                event_log_model.add_event_log(
                    self.settings,
                    level="info",
                    event_type="live_detected",
                    channel_id=channel_id,
                    recording_id=recording_id,
                    message="라이브를 감지했고 녹화 세션을 연결했습니다.",
                    payload={"broad_no": broad_no},
                )

            self._log_live_status_transition(
                channel=channel,
                recording_id=recording_id,
                broad_no=broad_no,
                prior_status=prior_status,
                prior_broad_no=prior_broad_no,
                next_status=next_status,
                error=ensure_result.error,
            )

            channel_model.update_status_if_current_broadcast(
                self.settings,
                channel_id,
                broad_no=broad_no,
                last_status=next_status,
                last_error=ensure_result.error,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            error_message = f"녹화 시작 백그라운드 작업에서 오류가 발생했습니다: {exc}"
            logger.exception(
                "Recording start task failed for channel_id=%s recording_id=%s",
                channel_id,
                recording_id,
            )
            recording_model.update_recording_fields(
                self.settings,
                recording_id,
                status="failed",
                error_message=error_message,
            )
            channel_model.update_status_if_current_broadcast(
                self.settings,
                channel_id,
                broad_no=broad_no,
                last_status="error",
                last_error=error_message,
            )
            event_log_model.add_event_log(
                self.settings,
                level="error",
                event_type="record_start_failed",
                channel_id=channel_id,
                recording_id=recording_id,
                message=error_message,
            )
        finally:
            self.state.active_recorder_count = self.recorder.active_count

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._poll_channels()
                self.state.last_iteration_finished_at = now_utc()
                self.state.active_recorder_count = self.recorder.active_count
                self.state.last_error = None
            except Exception as exc:  # pragma: no cover
                self.state.last_error = str(exc)
                logger.exception("Supervisor 루프에서 오류가 발생했습니다: %s", exc)

            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.settings.poll_interval_sec,
                )
                self._wake_event.clear()
            except TimeoutError:
                continue

    async def _poll_channels(self) -> None:
        now = now_utc()
        self._run_maintenance(now)
        all_channels = channel_model.list_channels(self.settings)
        forced_probe_channel_ids = self._pop_forced_probe_channel_ids(all_channels)
        manual_record_request_channel_ids = self._pop_manual_record_request_channel_ids(
            all_channels
        )

        self.state.iteration_count += 1
        self.state.last_channel_count = len(all_channels)

        for channel in all_channels:
            channel_id = int(channel["id"])
            force_probe = channel_id in forced_probe_channel_ids
            if not force_probe and not self._is_probe_due(channel, now):
                continue

            result = await self._run_probe(channel["user_id"])
            self.state.last_probe_at = now_utc()

            if result.status == ProbeStatus.LIVE:
                manual_record_requested = channel_id in manual_record_request_channel_ids
                allow_auto_start = bool(channel.get("enabled")) or (
                    manual_record_requested
                )
                await self._handle_live(
                    channel,
                    result,
                    allow_auto_start=allow_auto_start,
                    manual_record_requested=manual_record_requested,
                )
            elif result.status == ProbeStatus.OFFLINE:
                await self._handle_offline(channel)
            else:
                self._handle_probe_error(channel, result)

    def _pop_forced_probe_channel_ids(self, channels: list[dict]) -> set[int]:
        if not self._force_probe_channel_ids:
            return set()

        channel_ids = {int(channel["id"]) for channel in channels}
        forced_channel_ids = self._force_probe_channel_ids & channel_ids
        self._force_probe_channel_ids.difference_update(forced_channel_ids)
        self._force_probe_channel_ids.intersection_update(channel_ids)
        return forced_channel_ids

    def _pop_manual_record_request_channel_ids(self, channels: list[dict]) -> set[int]:
        if not self._manual_record_request_channel_ids:
            return set()

        channel_ids = {int(channel["id"]) for channel in channels}
        requested_channel_ids = self._manual_record_request_channel_ids & channel_ids
        self._manual_record_request_channel_ids.difference_update(requested_channel_ids)
        self._manual_record_request_channel_ids.intersection_update(channel_ids)
        return requested_channel_ids

    def _run_maintenance(self, now: datetime, *, force: bool = False) -> None:
        if (
            not force
            and self._last_maintenance_at is not None
            and now - self._last_maintenance_at < MAINTENANCE_INTERVAL
        ):
            return

        self._last_maintenance_at = now

        try:
            event_log_model.cleanup_event_logs(self.settings)
        except Exception:  # pragma: no cover
            logger.exception("이벤트 로그 정리 중 오류가 발생했습니다.")

        try:
            deleted_count = recording_model.cleanup_old_recordings(
                self.settings,
                retention_days=RECORDINGS_RETENTION_DAYS,
            )
        except Exception:  # pragma: no cover
            logger.exception("녹화 이력 정리 중 오류가 발생했습니다.")
            return

        if deleted_count <= 0:
            return

        try:
            event_log_model.add_event_log(
                self.settings,
                level="info",
                event_type="recordings_cleanup",
                message=(
                    f"{RECORDINGS_RETENTION_DAYS}일 지난 녹화 이력 "
                    f"{deleted_count}건을 정리했습니다."
                ),
                payload={
                    "retention_days": RECORDINGS_RETENTION_DAYS,
                    "deleted_count": deleted_count,
                    "scope": "db_only",
                },
            )
        except Exception:  # pragma: no cover
            logger.exception("녹화 이력 정리 이벤트 기록 중 오류가 발생했습니다.")

    async def _run_probe(self, user_id: str) -> ProbeResult:
        return await probe_channel(user_id, client=self._http_client)

    async def _handle_live(
        self,
        channel: dict,
        result: ProbeResult,
        *,
        allow_auto_start: bool,
        manual_record_requested: bool,
    ) -> None:
        payload = result.payload or {}
        channel_id = int(channel["id"])
        broad_no_raw = payload.get("broadNo")

        try:
            broad_no = int(broad_no_raw)
        except (TypeError, ValueError):
            self._handle_probe_error(
                channel,
                ProbeResult(
                    status=ProbeStatus.PROBE_ERROR,
                    error="프로브 응답에 유효한 broadNo가 없습니다.",
                ),
            )
            return

        if not manual_record_requested and self._is_manual_stop_hold_live(
            channel_id=channel_id,
            broad_no=broad_no,
        ):
            channel_model.update_probe_state(
                self.settings,
                channel_id,
                last_status=self._get_live_channel_status(channel_id),
                last_broad_no=broad_no,
                last_probe_at=now_utc().isoformat(),
                last_error=None,
                offline_streak=0,
            )
            return

        if not allow_auto_start:
            channel_model.update_probe_state(
                self.settings,
                channel_id,
                last_status=self._get_live_channel_status(channel_id),
                last_broad_no=broad_no,
                last_probe_at=now_utc().isoformat(),
                last_error=None,
                offline_streak=0,
            )
            return

        now_iso = now_utc().isoformat()

        if (
            not manual_record_requested
            and bool(channel.get("skip_subscription_plus"))
            and has_subscription_plus_hint(payload)
        ):
            channel_model.update_probe_state(
                self.settings,
                channel_id,
                last_status=self._get_live_channel_status(channel_id),
                last_broad_no=broad_no,
                last_probe_at=now_iso,
                last_error=None,
                offline_streak=0,
            )
            return

        recording, created = recording_model.create_or_get_recording_for_live(
            self.settings,
            channel_id=channel_id,
            user_id=channel["user_id"],
            broad_no=broad_no,
            payload=payload,
        )

        recording_model.update_recording_with_probe_payload(
            self.settings,
            recording["id"],
            payload,
        )

        prior_status = str(channel.get("last_status") or "")
        prior_broad_no = self._parse_optional_int(channel.get("last_broad_no"))
        existing_result = await self.recorder.get_existing_recording_result(
            recording=recording,
        )
        if existing_result is None:
            self._schedule_recording_start(
                channel=channel,
                recording=recording,
                payload=payload,
                created=created,
                prior_status=prior_status,
                prior_broad_no=prior_broad_no,
                broad_no=broad_no,
            )

            channel_model.update_probe_state(
                self.settings,
                channel_id,
                last_status="starting",
                last_broad_no=broad_no,
                last_probe_at=now_iso,
                last_error=None,
                offline_streak=0,
            )
            return

        ensure_result = existing_result
        next_status = self._status_from_ensure_result(ensure_result)

        if created and ensure_result.active:
            event_log_model.add_event_log(
                self.settings,
                level="info",
                event_type="live_detected",
                channel_id=channel["id"],
                recording_id=recording["id"],
                message="라이브를 감지했고 녹화 세션을 연결했습니다.",
                payload={"broad_no": broad_no},
            )

        self._log_live_status_transition(
            channel=channel,
            recording_id=recording["id"],
            broad_no=broad_no,
            prior_status=prior_status,
            prior_broad_no=prior_broad_no,
            next_status=next_status,
            error=ensure_result.error,
        )

        channel_model.update_probe_state(
            self.settings,
            channel_id,
            last_status=next_status,
            last_broad_no=broad_no,
            last_probe_at=now_iso,
            last_error=ensure_result.error,
            offline_streak=0,
        )

    def _log_live_status_transition(
        self,
        *,
        channel: dict[str, object],
        recording_id: int,
        broad_no: int,
        prior_status: str,
        prior_broad_no: int | None,
        next_status: str,
        error: str | None,
    ) -> None:
        same_broadcast = prior_broad_no == broad_no

        entering_standby = (
            next_status == "standby_no_stream"
            and (prior_status != "standby_no_stream" or not same_broadcast)
        )
        if entering_standby:
            event_log_model.add_event_log(
                self.settings,
                level="warning",
                event_type="stream_url_unavailable",
                channel_id=int(channel["id"]),
                recording_id=recording_id,
                message=(
                    "방송은 감지됐지만 재생 URL을 아직 확인하지 못했습니다. "
                    "대기 후 재시도합니다."
                ),
                payload={
                    "broad_no": broad_no,
                    "error": error,
                },
            )
            return

        recovering_from_standby = (
            next_status == "recording"
            and prior_status == "standby_no_stream"
            and same_broadcast
        )
        if recovering_from_standby:
            event_log_model.add_event_log(
                self.settings,
                level="info",
                event_type="stream_url_recovered",
                channel_id=int(channel["id"]),
                recording_id=recording_id,
                message="재생 URL을 확보해 녹화를 다시 시작했습니다.",
                payload={"broad_no": broad_no},
            )

    def _parse_optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _handle_offline(self, channel: dict) -> None:
        channel_id = int(channel["id"])
        now_iso = now_utc().isoformat()
        prior_streak = int(channel.get("offline_streak") or 0)
        offline_streak = prior_streak + 1
        self.clear_manual_stop_hold(channel_id)

        active_recording = recording_model.get_active_recording_for_channel(
            self.settings,
            channel_id,
        )

        active_status = ""
        if active_recording is not None:
            active_status = str(active_recording.get("status") or "")

        if active_status in {"remuxing", "stopping"}:
            status = active_status
        elif active_recording is not None and offline_streak >= self.settings.offline_confirm_count:
            await self.recorder.stop_recording(channel_id, reason="offline_confirmed")
            status = "stopping"
        elif active_recording is not None:
            status = "recording"
        else:
            status = "offline"

        channel_model.update_probe_state(
            self.settings,
            channel_id,
            last_status=status,
            last_broad_no=channel.get("last_broad_no"),
            last_probe_at=now_iso,
            last_error=None,
            offline_streak=offline_streak,
        )

    def _handle_probe_error(self, channel: dict, result: ProbeResult) -> None:
        channel_id = int(channel["id"])
        now_iso = now_utc().isoformat()
        error_message = result.error or "프로브 오류"

        next_status = self._get_live_channel_status(channel_id)
        if next_status == "online":
            next_status = "error"

        channel_model.update_probe_state(
            self.settings,
            channel_id,
            last_status=next_status,
            last_broad_no=channel.get("last_broad_no"),
            last_probe_at=now_iso,
            last_error=error_message,
            offline_streak=int(channel.get("offline_streak") or 0),
        )

        if (
            str(channel.get("last_error") or "") != error_message
            or str(channel.get("last_status") or "") != next_status
        ):
            event_log_model.add_event_log(
                self.settings,
                level="warning",
                event_type="probe_error",
                channel_id=channel_id,
                message="채널 프로브에 실패했습니다.",
                payload={"error": result.error},
            )

    def _get_live_channel_status(self, channel_id: int) -> str:
        active_recording = recording_model.get_active_recording_for_channel(
            self.settings,
            channel_id,
        )
        if active_recording is None:
            return "online"

        active_status = str(active_recording.get("status") or "")
        if active_status in {"remuxing", "stopping"}:
            return active_status
        return "recording"

    def _status_from_ensure_result(self, ensure_result: EnsureRecordingResult) -> str:
        if ensure_result.finalizing:
            return "remuxing"
        if ensure_result.standby_no_stream:
            return "standby_no_stream"
        return "recording" if ensure_result.active else "error"

    def _is_manual_stop_hold_live(self, *, channel_id: int, broad_no: int) -> bool:
        held_broad_no = self._manual_stop_hold_broad_no_by_channel_id.get(channel_id)
        if held_broad_no is None:
            return False
        if held_broad_no != broad_no:
            self._manual_stop_hold_broad_no_by_channel_id.pop(channel_id, None)
            return False
        return True

    def _is_probe_due(self, channel: dict, now: datetime) -> bool:
        last_probe_raw = channel.get("last_probe_at")
        if not last_probe_raw:
            return True

        interval_sec = int(self.settings.poll_interval_sec)
        if interval_sec <= 0:
            interval_sec = 1

        try:
            last_probe = datetime.fromisoformat(str(last_probe_raw))
            if last_probe.tzinfo is None:
                last_probe = last_probe.replace(tzinfo=UTC)
        except ValueError:
            return True

        return now - last_probe >= timedelta(seconds=interval_sec)
