from __future__ import annotations

import json
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import Settings
from app.utils.time import now_utc, to_timezone

EVENT_LOG_RELATIVE_PATH = Path("logs/events.jsonl")
EVENT_LOG_RETENTION_DAYS = 30
EVENT_LOG_MAX_LINES = 20_000

_EVENT_LOG_LOCK = threading.RLock()
_EVENT_LOG_NEXT_ID: int | None = None


def _event_log_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / EVENT_LOG_RELATIVE_PATH


def _serialize_payload(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False)


def _normalize_event_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None

    try:
        event_id = int(record.get("id"))
    except (TypeError, ValueError):
        return None

    created_at = str(record.get("created_at") or "").strip()
    if not created_at:
        return None

    level = str(record.get("level") or "").strip()
    event_type = str(record.get("event_type") or "").strip()
    message = str(record.get("message") or "").strip()
    if not level or not event_type or not message:
        return None

    channel_id_raw = record.get("channel_id")
    recording_id_raw = record.get("recording_id")

    try:
        channel_id = int(channel_id_raw) if channel_id_raw is not None else None
    except (TypeError, ValueError):
        channel_id = None

    try:
        recording_id = int(recording_id_raw) if recording_id_raw is not None else None
    except (TypeError, ValueError):
        recording_id = None

    payload_json_raw = record.get("payload_json")
    if payload_json_raw is None:
        payload_json = None
    elif isinstance(payload_json_raw, str):
        payload_json = payload_json_raw
    else:
        payload_json = json.dumps(payload_json_raw, ensure_ascii=False)

    return {
        "id": event_id,
        "created_at": created_at,
        "level": level,
        "channel_id": channel_id,
        "recording_id": recording_id,
        "event_type": event_type,
        "message": message,
        "payload_json": payload_json,
    }


def _parse_iso8601(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_event_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_event_record(raw)
            if normalized is not None:
                records.append(normalized)
    return records


def _write_event_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        for record in records:
            fp.write(json.dumps(record, ensure_ascii=False))
            fp.write("\n")
    tmp_path.replace(path)


def _initialize_next_id(path: Path) -> None:
    global _EVENT_LOG_NEXT_ID
    if _EVENT_LOG_NEXT_ID is not None:
        return

    records = _read_event_records(path)
    last_id = records[-1]["id"] if records else 0
    _EVENT_LOG_NEXT_ID = last_id + 1


def cleanup_event_logs(settings: Settings) -> int:
    global _EVENT_LOG_NEXT_ID

    cutoff = now_utc() - timedelta(days=EVENT_LOG_RETENTION_DAYS)
    path = _event_log_path(settings)

    with _EVENT_LOG_LOCK:
        records = _read_event_records(path)
        if not records:
            if _EVENT_LOG_NEXT_ID is None:
                _EVENT_LOG_NEXT_ID = 1
            return 0

        kept: list[dict[str, Any]] = []
        for record in records:
            parsed = _parse_iso8601(str(record["created_at"]))
            if parsed is None or parsed >= cutoff:
                kept.append(record)

        if len(kept) > EVENT_LOG_MAX_LINES:
            kept = kept[-EVENT_LOG_MAX_LINES:]

        removed_count = len(records) - len(kept)
        if removed_count > 0:
            _write_event_records(path, kept)

        last_id = kept[-1]["id"] if kept else 0
        next_id = last_id + 1
        if _EVENT_LOG_NEXT_ID is None or _EVENT_LOG_NEXT_ID < next_id:
            _EVENT_LOG_NEXT_ID = next_id

        return removed_count


def get_event_log_cursor(settings: Settings) -> tuple[int, int]:
    path = _event_log_path(settings)
    with _EVENT_LOG_LOCK:
        try:
            stat = path.stat()
        except OSError:
            return 0, 0
    return int(stat.st_size), int(stat.st_mtime_ns)


def add_event_log(
    settings: Settings,
    *,
    level: str,
    event_type: str,
    message: str,
    channel_id: int | None = None,
    recording_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    global _EVENT_LOG_NEXT_ID

    timestamp = to_timezone(now_utc(), settings.timezone).isoformat(timespec="seconds")
    path = _event_log_path(settings)

    with _EVENT_LOG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _initialize_next_id(path)
        next_id = _EVENT_LOG_NEXT_ID or 1
        _EVENT_LOG_NEXT_ID = next_id + 1

        record = {
            "id": next_id,
            "created_at": timestamp,
            "level": str(level),
            "channel_id": channel_id,
            "recording_id": recording_id,
            "event_type": str(event_type),
            "message": str(message),
            "payload_json": _serialize_payload(payload),
        }
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False))
            fp.write("\n")

    return next_id


def list_recent_event_logs(settings: Settings, *, limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, limit)
    path = _event_log_path(settings)

    with _EVENT_LOG_LOCK:
        if not path.exists():
            return []

        recent: deque[dict[str, Any]] = deque(maxlen=safe_limit)
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                record = _normalize_event_record(raw)
                if record is not None:
                    recent.append(record)

        return list(reversed(list(recent)))
