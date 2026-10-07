import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from starlette.requests import Request

from app.config import Settings
from app.db import initialize_database
from app.main import app
from app.models import channel, event_log, recording
from app.models import settings as settings_model
from app.routers import api_system
from app.services.poller import SupervisorState
from app.utils.asyncio import run_blocking
from app.utils.time import now_utc


def add_event(settings, message="event"):
    return event_log.add_event_log(settings, level="info", event_type="test", message=message)


def test_recent_events_reuse_cache_and_cleanup_updates_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    read = Mock(wraps=event_log._read_event_records)
    monkeypatch.setattr(event_log, "_read_event_records", read)
    ids = [add_event(settings, str(index)) for index in range(3)]
    assert read.call_count == 1
    for _ in range(3):
        events = event_log.list_recent_event_logs(settings, limit=12)
        assert [item["id"] for item in events] == list(reversed(ids))
        events[0]["message"] = "modified by UI"
    assert read.call_count == 1
    assert event_log.list_recent_event_logs(settings)[0]["message"] == "2"
    monkeypatch.setattr(event_log, "EVENT_LOG_MAX_LINES", 2)
    assert event_log.cleanup_event_logs(settings) == 1
    assert read.call_count == 2
    assert [item["id"] for item in event_log.list_recent_event_logs(settings)] == ids[:0:-1]
    assert read.call_count == 2
    assert add_event(settings) > ids[-1]
    assert read.call_count == 2
    add_event(settings, "  event  ")
    assert event_log.list_recent_event_logs(settings)[0]["message"] == "event"
    assert event_log.list_recent_event_logs(settings, limit=501)[0]["message"] == "event"


def test_log_cache_handles_large_limits_and_external_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    path = event_log._event_log_path(settings)
    path.parent.mkdir(parents=True)
    record = {
        "created_at": now_utc().isoformat(),
        "level": "info",
        "event_type": "test",
        "message": "event",
        "payload_json": None,
    }
    with path.open("w", encoding="utf-8") as stream:
        for index in range(1, 601):
            stream.write(json.dumps({**record, "id": index}) + "\n")
        stream.write("invalid JSON\n")
    assert len(event_log.list_recent_event_logs(settings, limit=500)) == 500
    assert len(event_log.list_recent_event_logs(settings, limit=600)) == 600
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({**record, "id": 800, "message": "external"}) + "\n")
    assert event_log.list_recent_event_logs(settings)[0]["message"] == "external"
    assert add_event(settings) == 801


@pytest.mark.asyncio
async def test_concurrent_log_appends_keep_unique_ids_and_recent_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    ids = await asyncio.gather(*(run_blocking(add_event, settings) for _ in range(24)))
    assert sorted(ids) == list(range(1, 25))
    events = await run_blocking(event_log.list_recent_event_logs, settings)
    assert [item["id"] for item in events] == list(range(24, 0, -1))
    assert len(event_log._event_log_path(settings).read_text(encoding="utf-8").splitlines()) == 24


@pytest.mark.asyncio
async def test_status_and_sse_share_cursor_without_blocking_health(monkeypatch):
    started, release = threading.Event(), threading.Event()
    state = SupervisorState()
    monkeypatch.setattr(app.state, "settings", Settings(_env_file=None), raising=False)
    monkeypatch.setattr(app.state, "supervisor", SimpleNamespace(state=state), raising=False)
    monkeypatch.setattr(app.state, "stream_db_cursor", None, raising=False)
    monkeypatch.setattr(app.state, "stream_db_cursor_lock", asyncio.Lock(), raising=False)

    def read(*args):
        started.set()
        assert release.wait(5)
        return (100, 123, 7, "first cursor")

    fetch = Mock(side_effect=read)
    monkeypatch.setattr(api_system, "_fetch_stream_db_cursor", fetch)
    request = Request({"type": "http", "app": app})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        tasks = [asyncio.create_task(client.get("/api/system/status")) for _ in range(8)]
        tasks.append(asyncio.create_task(api_system._build_stream_state_key(request)))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
            assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
        finally:
            release.set()
            responses = await asyncio.gather(*tasks)
        assert all(response.status_code == 200 for response in responses[:-1])
        assert responses[-1] == (0, 100, 123, 7, "first cursor")
        assert fetch.call_count == 1
        state.active_recorder_count = 2
        assert (await api_system._build_stream_state_key(request))[0] == 2
        assert fetch.call_count == 1
        app.state.stream_db_cursor = (0, app.state.stream_db_cursor[1])
        fetch.side_effect = None
        fetch.return_value = (200, 456, 8, "changed cursor")
        assert (await api_system._build_stream_state_key(request))[-1] == "changed cursor"
        assert fetch.call_count == 2


def test_unchanged_recording_metadata_does_not_write(tmp_path, monkeypatch):
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
    payload = {"broadTitle": "title", "broadStart": "2026-10-08T00:00:00Z"}
    session, _ = recording.create_or_get_recording_for_live(
        settings,
        channel_id=item["id"],
        user_id="test",
        broad_no=1,
        payload=payload,
    )
    connect = recording.connect
    statements = []

    @contextmanager
    def traced_connect(settings):
        with connect(settings) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(recording, "connect", traced_connect)
    recording.update_recording_with_probe_payload(settings, session["id"], payload)
    assert not any(sql.startswith(("UPDATE", "BEGIN", "COMMIT")) for sql in statements)
    assert (
        recording.get_recording_by_id(settings, session["id"])["updated_at"]
        == session["updated_at"]
    )
    statements.clear()
    recording.update_recording_with_probe_payload(
        settings, session["id"], {**payload, "broadTitle": "new"}
    )
    assert sum(sql.startswith("COMMIT") for sql in statements) == 1
    assert recording.get_recording_by_id(settings, session["id"])["broad_title"] == "new"


def test_auth_read_uses_one_connection_and_preserves_password_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, app_secret_key="isolated test key")
    initialize_database(settings)
    settings_model.update_auth_settings(
        settings,
        username="test",
        password="test password",
        cookies_txt_path="cookies.txt",
    )
    connect = Mock(wraps=settings_model.connect)
    monkeypatch.setattr(settings_model, "connect", connect)
    auth = settings_model.get_auth_settings(settings)
    assert auth == {"username": "test", "has_password": True, "cookies_txt_path": "cookies.txt"}
    assert connect.call_count == 1
    connect.reset_mock()
    credentials = settings_model.get_auth_credentials(settings)
    assert credentials["password"] == "test password"
    assert connect.call_count == 1
    assert (
        settings_model.list_settings(settings)[settings_model.SOOP_PASSWORD_KEY] != "test password"
    )
