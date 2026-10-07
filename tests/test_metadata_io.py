import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.config import Settings
from app.db import initialize_database
from app.main import app
from app.models import channel, event_log, recording
from app.services.poller import Supervisor
from app.services.recorder import EnsureRecordingResult, RecorderManager, RecordingHandle
from app.utils.time import now_utc


@pytest.mark.parametrize(
    "path,module,name",
    [
        ("/api/channels", channel, "list_channels"),
        ("/api/events", event_log, "list_recent_event_logs"),
    ],
)
@pytest.mark.asyncio
async def test_slow_metadata_request_does_not_block_health(monkeypatch, path, module, name):
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    monkeypatch.setattr(app.state, "settings", Settings(_env_file=None), raising=False)

    def slow_read(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(module, name, slow_read)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(client.get(path))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
            response = await asyncio.wait_for(client.get("/health"), 1)
            assert response.status_code == 200
            assert not task.done()
        finally:
            release.set()
            response = await task
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_maintenance_does_not_block_polling_and_shutdown_waits(monkeypatch):
    started, release = threading.Event(), threading.Event()
    supervisor = Supervisor(Settings(_env_file=None))
    supervisor.state.running = True

    def maintenance():
        started.set()
        assert release.wait(5)

    work = Mock(side_effect=maintenance)
    monkeypatch.setattr(supervisor, "_run_maintenance", work)
    monkeypatch.setattr(channel, "list_channels", lambda *args: [])
    monkeypatch.setattr(supervisor.recorder, "stop_all", AsyncMock())
    supervisor._schedule_maintenance(now_utc(), force=True)
    stop = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
        supervisor._schedule_maintenance(now_utc(), force=True)
        await asyncio.wait_for(supervisor._poll_channels(), 1)
        assert supervisor.state.iteration_count == 1
        assert work.call_count == 1
        stop = asyncio.create_task(supervisor.stop())
        await asyncio.sleep(0)
        assert not stop.done()
    finally:
        release.set()
        if stop is not None:
            await stop
        else:
            await supervisor._maintenance_task
    assert supervisor.state.running is False


def create_session(settings, channel_id):
    return recording.create_or_get_recording_for_live(
        settings,
        channel_id=channel_id,
        user_id="test",
        broad_no=1,
        payload={"broadTitle": "title"},
    )[0]


@pytest.mark.asyncio
async def test_delete_rechecks_active_session_after_initial_api_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    initialize_database(settings)
    item = channel.create_channel(
        settings,
        user_id="test",
        display_name=None,
        enabled=True,
        skip_subscription_plus=False,
        output_template=None,
        stream_password=None,
        preferred_quality="best",
    )
    monkeypatch.setattr(app.state, "settings", settings, raising=False)

    def stale_check(*args):
        create_session(settings, item["id"])
        return None

    monkeypatch.setattr(recording, "get_active_recording_for_channel", stale_check)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(f"/api/channels/{item['id']}")
    assert response.status_code == 409
    assert channel.get_channel(settings, item["id"]) is not None


def test_late_session_updates_do_not_overwrite_newer_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    initialize_database(settings)
    item = channel.create_channel(
        settings,
        user_id="test",
        display_name=None,
        enabled=True,
        skip_subscription_plus=False,
        output_template=None,
        stream_password=None,
        preferred_quality="best",
    )
    first = create_session(settings, item["id"])
    recording.update_recording_fields(settings, first["id"], status="completed")
    second = create_session(settings, item["id"])
    channel.update_probe_state(
        settings,
        item["id"],
        last_status="recording",
        last_broad_no=1,
        last_probe_at=now_utc().isoformat(),
        last_error=None,
        offline_streak=0,
    )
    assert not channel.mark_recording_error_if_current_broadcast(
        settings,
        item["id"],
        broad_no=1,
        recording_id=first["id"],
        last_error="old error",
    )
    assert channel.get_channel(settings, item["id"])["last_status"] == "recording"
    assert recording.get_recording_by_id(settings, second["id"])["status"] == "starting"
    recording.update_recording_fields(
        settings,
        first["id"],
        only_statuses=("starting", "recording"),
        status="stopping",
    )
    assert recording.get_recording_by_id(settings, first["id"])["status"] == "completed"
    assert not channel.update_status_if_current_broadcast(
        settings,
        item["id"],
        broad_no=1,
        recording_id=first["id"],
        last_status="recording",
    )
    recording.update_recording_fields(settings, second["id"], status="completed")
    channel.update_probe_state(
        settings,
        item["id"],
        last_status="offline",
        last_broad_no=1,
        last_probe_at=now_utc().isoformat(),
        offline_streak=1,
    )
    assert not channel.update_status_if_current_broadcast(
        settings,
        item["id"],
        broad_no=1,
        recording_id=second["id"],
        last_status="recording",
    )
    assert channel.get_channel(settings, item["id"])["last_status"] == "offline"


@pytest.mark.asyncio
async def test_start_cancellation_during_database_write_cleans_child(tmp_path, monkeypatch):
    from app.services import recorder as service

    started, release = threading.Event(), threading.Event()
    monkeypatch.chdir(tmp_path)
    manager = RecorderManager(Settings(_env_file=None))
    monkeypatch.setattr(manager, "_validate_binaries", lambda: None)
    monkeypatch.setattr(manager, "_resolve_stream_url", AsyncMock(return_value="http://test/hls"))
    monkeypatch.setattr(service.settings_model, "get_proxy_settings", lambda *args: {})
    monkeypatch.setattr(service.settings_model, "get_auth_credentials", lambda *args: {})
    monkeypatch.setattr(recording, "update_recording_with_probe_payload", lambda *args: None)
    monkeypatch.setattr(event_log, "add_event_log", lambda *args, **kwargs: None)
    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    process.terminate = Mock(side_effect=lambda: setattr(process, "returncode", 0))
    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    def update(*args, **fields):
        if fields.get("status") == "recording":
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(recording, "update_recording_fields", update)
    task = asyncio.create_task(
        manager._start_recording(
            channel={"id": 1, "user_id": "test", "preferred_quality": "best"},
            recording={"id": 1, "channel_id": 1, "broad_no": 1},
            payload={"broadTitle": "title"},
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.terminate.assert_called_once()
        assert manager.active_count == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_capture_exit_releases_new_broadcast_before_proxy_cleanup(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    manager = RecorderManager(Settings(_env_file=None))

    def stop_proxy():
        started.set()
        assert release.wait(5)

    handle = RecordingHandle(
        channel_id=1,
        recording_id=1,
        user_id="test",
        broad_no=1,
        temp_path=tmp_path / "old.ts",
        remux_temp_path=tmp_path / "old.mp4",
        final_path=tmp_path / "final.mp4",
        process=SimpleNamespace(
            stderr=True, communicate=AsyncMock(return_value=(None, b"")), returncode=0
        ),
        subscription_proxy=SimpleNamespace(stop=stop_proxy),
    )
    manager._handles[1] = handle
    monkeypatch.setattr(recording, "update_recording_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(event_log, "add_event_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_run_remux", AsyncMock(return_value=(True, handle.final_path)))
    start = AsyncMock(return_value=EnsureRecordingResult(True, True, 2))
    monkeypatch.setattr(manager, "_start_recording", start)
    task = asyncio.create_task(manager._watch_process(handle))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
        result = await asyncio.wait_for(
            manager.ensure_recording(
                channel={"id": 1},
                recording={"id": 2, "channel_id": 1, "broad_no": 2},
                payload={},
            ),
            1,
        )
        assert result.started
        assert handle.capture_done.is_set()
        assert not task.done()
        start.assert_awaited_once()
    finally:
        release.set()
        await task
