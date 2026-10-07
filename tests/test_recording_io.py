import asyncio
import errno
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.main import app
from app.services import recorder
from app.utils.asyncio import run_blocking


async def remux(tmp_path, monkeypatch, *, install=None):
    manager = recorder.RecorderManager(Settings(_env_file=None))
    source = tmp_path / "recording.ts"
    output = tmp_path / "remux.mp4"
    final = tmp_path / "final.mp4"
    source.write_bytes(b"recorded media")
    updates = []
    monkeypatch.setattr(
        recorder.recording_model,
        "update_recording_fields",
        lambda *args, **fields: updates.append(fields),
    )

    async def start_process(*args, **kwargs):
        Path(args[-1]).write_bytes(b"remuxed media")
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (None, b"")
        return process

    start = AsyncMock(side_effect=start_process)
    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", start)
    if install is not None:
        monkeypatch.setattr(manager, "_install_file_without_overwrite", install)
    result = await manager._run_remux(
        recording_id=1,
        temp_path=source,
        remux_temp_path=output,
        final_path=final,
        stop_requested=False,
        stop_reason=None,
        recorder_exit_code=0,
        recorder_stderr="",
    )
    return result, updates, start


@pytest.mark.asyncio
async def test_collision_reuses_remux_and_preserves_existing_file(tmp_path, monkeypatch):
    original = recorder.RecorderManager._install_file_without_overwrite

    def install(**kwargs):
        if kwargs["destination_path"].name == "final.mp4":
            kwargs["destination_path"].write_bytes(b"existing media")
        return original(recorder.RecorderManager(Settings(_env_file=None)), **kwargs)

    result, updates, start = await remux(tmp_path, monkeypatch, install=install)
    assert result == (True, tmp_path / "final (1).mp4")
    assert start.await_count == 1
    assert (tmp_path / "final.mp4").read_bytes() == b"existing media"
    assert result[1].read_bytes() == b"remuxed media"
    assert updates[-1]["status"] == "completed"
    assert not (tmp_path / "recording.ts").exists()


@pytest.mark.asyncio
async def test_copy_failure_preserves_recovery_files(tmp_path, monkeypatch):
    def no_link(*args):
        raise OSError(errno.EXDEV, "cross-device link")

    def failed_copy(source, destination):
        destination.write(b"partial copy")
        raise OSError("disk write failed")

    monkeypatch.setattr(recorder.os, "link", no_link)
    monkeypatch.setattr(recorder.shutil, "copyfileobj", failed_copy)
    result, updates, _ = await remux(tmp_path, monkeypatch)
    assert result[0] is False
    assert updates[-1]["status"] == "partial"
    assert updates[-1]["temp_path"] == str(tmp_path / "remux.mp4")
    assert (tmp_path / "recording.ts").read_bytes() == b"recorded media"
    assert (tmp_path / "remux.mp4").read_bytes() == b"remuxed media"
    assert not (tmp_path / "final.mp4").exists()


@pytest.mark.asyncio
async def test_slow_copy_does_not_block_health(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    copy = recorder.shutil.copyfileobj

    def no_link(*args):
        raise OSError(errno.EXDEV, "cross-device link")

    def slow_copy(source, destination):
        started.set()
        assert release.wait(5)
        copy(source, destination)

    monkeypatch.setattr(recorder.os, "link", no_link)
    monkeypatch.setattr(recorder.shutil, "copyfileobj", slow_copy)
    task = asyncio.create_task(remux(tmp_path, monkeypatch))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await asyncio.wait_for(client.get("/health"), 1)
        assert response.status_code == 200
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_cancelled_blocking_work_is_drained_even_on_repeated_cancel():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        started.set()
        assert release.wait(5)
        finished.set()

    task = asyncio.create_task(run_blocking(work))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
