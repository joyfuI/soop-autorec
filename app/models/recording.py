from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.config import Settings
from app.db import connect
from app.utils.time import now_utc

ACTIVE_RECORDING_STATUSES = ("starting", "recording", "stopping", "remuxing")

RECORDING_COLUMNS = (
    "id, channel_id, user_id, broad_no, broad_title, broad_start_at, status, "
    "detected_at, recording_started_at, recording_stopped_at, final_path, temp_path, "
    "file_size_bytes, ffmpeg_exit_code, error_message, created_at, updated_at"
)

UPDATABLE_FIELDS = {
    "status",
    "broad_title",
    "broad_start_at",
    "recording_started_at",
    "recording_stopped_at",
    "final_path",
    "temp_path",
    "file_size_bytes",
    "ffmpeg_exit_code",
    "error_message",
}


def _row_to_recording_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "user_id": row["user_id"],
        "broad_no": row["broad_no"],
        "broad_title": row["broad_title"],
        "broad_start_at": row["broad_start_at"],
        "status": row["status"],
        "detected_at": row["detected_at"],
        "recording_started_at": row["recording_started_at"],
        "recording_stopped_at": row["recording_stopped_at"],
        "final_path": row["final_path"],
        "temp_path": row["temp_path"],
        "file_size_bytes": row["file_size_bytes"],
        "ffmpeg_exit_code": row["ffmpeg_exit_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_active_recording_for_channel(settings: Settings, channel_id: int) -> dict[str, Any] | None:
    placeholders = ", ".join(["?" for _ in ACTIVE_RECORDING_STATUSES])
    query = (
        f"SELECT {RECORDING_COLUMNS} FROM recordings "
        f"WHERE channel_id = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1"
    )

    with connect(settings) as conn:
        row = conn.execute(query, (channel_id, *ACTIVE_RECORDING_STATUSES)).fetchone()

    if row is None:
        return None
    return _row_to_recording_dict(row)


def get_recording_by_user_and_broad(
    settings: Settings,
    user_id: str,
    broad_no: int,
) -> dict[str, Any] | None:
    with connect(settings) as conn:
        row = conn.execute(
            f"SELECT {RECORDING_COLUMNS} FROM recordings WHERE user_id = ? AND broad_no = ?",
            (user_id, broad_no),
        ).fetchone()

    if row is None:
        return None
    return _row_to_recording_dict(row)


def get_recording_by_id(settings: Settings, recording_id: int) -> dict[str, Any] | None:
    with connect(settings) as conn:
        row = conn.execute(
            f"SELECT {RECORDING_COLUMNS} FROM recordings WHERE id = ?",
            (recording_id,),
        ).fetchone()

    if row is None:
        return None
    return _row_to_recording_dict(row)


def create_or_get_recording_for_live(
    settings: Settings,
    *,
    channel_id: int,
    user_id: str,
    broad_no: int,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    existing = get_recording_by_user_and_broad(settings, user_id, broad_no)
    if existing is not None:
        return existing, False

    timestamp = now_utc().isoformat()
    broad_title = str(payload.get("broadTitle") or "")

    with connect(settings) as conn:
        cursor = conn.execute(
            """
            INSERT INTO recordings (
              channel_id, user_id, broad_no, broad_title, broad_start_at,
              status, detected_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                user_id,
                broad_no,
                broad_title,
                payload.get("broadStart"),
                "starting",
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        recording_id = int(cursor.lastrowid)

    created = get_recording_by_id(settings, recording_id)
    if created is None:
        raise RuntimeError("Failed to load created recording")
    return created, True


def update_recording_fields(
    settings: Settings,
    recording_id: int,
    **fields: Any,
) -> None:
    if not fields:
        return

    unknown_keys = [key for key in fields if key not in UPDATABLE_FIELDS]
    if unknown_keys:
        raise ValueError(f"Unsupported recording fields: {', '.join(sorted(unknown_keys))}")

    updated_at = now_utc().isoformat()
    set_parts: list[str] = []
    values: list[Any] = []

    for key, value in fields.items():
        set_parts.append(f"{key} = ?")
        values.append(value)

    set_parts.append("updated_at = ?")
    values.append(updated_at)
    values.append(recording_id)

    sql = f"UPDATE recordings SET {', '.join(set_parts)} WHERE id = ?"

    with connect(settings) as conn:
        conn.execute(sql, values)
        conn.commit()


def mark_active_recordings_interrupted(settings: Settings) -> int:
    timestamp = now_utc().isoformat()
    placeholders = ", ".join(["?" for _ in ACTIVE_RECORDING_STATUSES])
    sql = (
        "UPDATE recordings "
        "SET status = 'interrupted', recording_stopped_at = ?, updated_at = ? "
        f"WHERE status IN ({placeholders})"
    )

    with connect(settings) as conn:
        cursor = conn.execute(sql, (timestamp, timestamp, *ACTIVE_RECORDING_STATUSES))
        conn.commit()

    return int(cursor.rowcount)


def cleanup_old_recordings(settings: Settings, *, retention_days: int) -> int:
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")

    cutoff_iso = (now_utc() - timedelta(days=retention_days)).isoformat()
    placeholders = ", ".join(["?" for _ in ACTIVE_RECORDING_STATUSES])
    sql = (
        "DELETE FROM recordings "
        f"WHERE status NOT IN ({placeholders}) "
        "AND COALESCE(recording_stopped_at, detected_at, created_at) < ?"
    )

    with connect(settings) as conn:
        cursor = conn.execute(sql, (*ACTIVE_RECORDING_STATUSES, cutoff_iso))
        conn.commit()

    return int(cursor.rowcount)


def list_recent_recordings(settings: Settings, *, limit: int = 20) -> list[dict[str, Any]]:
    with connect(settings) as conn:
        rows = conn.execute(
            f"SELECT {RECORDING_COLUMNS} FROM recordings ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [_row_to_recording_dict(row) for row in rows]


def update_recording_with_probe_payload(
    settings: Settings,
    recording_id: int,
    payload: dict[str, Any],
) -> None:
    update_recording_fields(
        settings,
        recording_id,
        broad_title=str(payload.get("broadTitle") or ""),
        broad_start_at=payload.get("broadStart"),
    )
