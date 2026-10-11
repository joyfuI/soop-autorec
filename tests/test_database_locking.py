import asyncio
import sqlite3
import threading
from unittest.mock import Mock

import httpx
import pytest

from app import db
from app.config import Settings
from app.main import app
from app.utils.asyncio import run_blocking


@pytest.mark.asyncio
async def test_connection_close_serializes_other_connections_without_blocking_health(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    db.initialize_database(settings)
    closing, release = threading.Event(), threading.Event()
    second_started, second_opened = threading.Event(), threading.Event()
    original_connect = sqlite3.connect
    connections = []

    class SlowClose(sqlite3.Connection):
        def close(self):
            if self is connections[0]:
                closing.set()
                assert release.wait(5)
            super().close()

    def open_connection(path):
        if connections:
            second_opened.set()
        connection = original_connect(path, factory=SlowClose)
        connections.append(connection)
        return connection

    def read(*, second=False):
        if second:
            second_started.set()
        with db.connect(settings) as connection:
            return connection.execute("SELECT 1").fetchone()[0]

    monkeypatch.setattr(db.sqlite3, "connect", open_connection)
    tasks = [asyncio.create_task(run_blocking(read))]
    try:
        assert await asyncio.to_thread(closing.wait, 2)
        tasks.append(asyncio.create_task(run_blocking(read, second=True)))
        assert await asyncio.to_thread(second_started.wait, 2)
        assert not await asyncio.to_thread(second_opened.wait, 0.1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert results == [1, 1]
    assert second_opened.is_set()


@pytest.mark.parametrize("failure_stage", ["setup", "body", "close"])
def test_connection_failure_closes_connection_and_releases_lock(monkeypatch, failure_stage):
    connection = Mock()
    error = sqlite3.OperationalError("test failure")
    if failure_stage == "setup":
        connection.execute.side_effect = error
    elif failure_stage == "close":
        connection.close.side_effect = error
    monkeypatch.setattr(db.sqlite3, "connect", Mock(return_value=connection))
    with pytest.raises(sqlite3.OperationalError, match="test failure"):
        with db.connect(Settings(_env_file=None)):
            if failure_stage == "body":
                raise error
    connection.close.assert_called_once()
    assert db._DB_LOCK.acquire(blocking=False)
    db._DB_LOCK.release()


@pytest.mark.asyncio
async def test_concurrent_database_reads_and_commits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    db.initialize_database(settings)
    with db.connect(settings) as connection:
        connection.execute("CREATE TABLE counter (value INTEGER)")
        connection.execute("INSERT INTO counter VALUES (0)")
        connection.commit()

    def access(*, write):
        with db.connect(settings) as connection:
            if write:
                connection.execute("UPDATE counter SET value = value + 1")
                connection.commit()
            return connection.execute("SELECT value FROM counter").fetchone()[0]

    results = await asyncio.gather(*(run_blocking(access, write=i < 16) for i in range(24)))
    assert all(0 <= result <= 16 for result in results)
    assert await run_blocking(access, write=False) == 16
