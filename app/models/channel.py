from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import connect
from app.utils.time import now_utc

CHANNEL_COLUMNS = (
    "id, user_id, display_name, enabled, output_template, "
    "stream_password_enc, preferred_quality, last_status, last_broad_no, "
    "last_probe_at, last_error, offline_streak, updated_at"
)


def _row_to_channel_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "display_name": row["display_name"],
        "enabled": bool(row["enabled"]),
        "output_template": row["output_template"],
        "stream_password_enc": row["stream_password_enc"],
        "preferred_quality": row["preferred_quality"],
        "last_status": row["last_status"],
        "last_broad_no": row["last_broad_no"],
        "last_probe_at": row["last_probe_at"],
        "last_error": row["last_error"],
        "offline_streak": row["offline_streak"],
        "updated_at": row["updated_at"],
    }


def list_channels(settings: Settings, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    where_clause = "WHERE enabled = 1" if enabled_only else ""
    query = f"SELECT {CHANNEL_COLUMNS} FROM channels {where_clause} ORDER BY id ASC"

    with connect(settings) as conn:
        rows = conn.execute(query).fetchall()

    return [_row_to_channel_dict(row) for row in rows]


def get_channel(settings: Settings, channel_id: int) -> dict[str, Any] | None:
    with connect(settings) as conn:
        row = conn.execute(
            f"SELECT {CHANNEL_COLUMNS} FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()

    if row is None:
        return None
    return _row_to_channel_dict(row)


def create_channel(
    settings: Settings,
    *,
    user_id: str,
    display_name: str | None,
    enabled: bool,
    output_template: str | None,
    stream_password_enc: str | None,
    preferred_quality: str,
) -> dict[str, Any]:
    timestamp = now_utc().isoformat()

    with connect(settings) as conn:
        cursor = conn.execute(
            """
            INSERT INTO channels (
              user_id, display_name, enabled, output_template,
              stream_password_enc, preferred_quality, last_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'offline', ?)
            """,
            (
                user_id,
                display_name,
                1 if enabled else 0,
                output_template,
                stream_password_enc,
                preferred_quality,
                timestamp,
            ),
        )
        conn.commit()
        channel_id = int(cursor.lastrowid)

    created = get_channel(settings, channel_id)
    if created is None:
        raise RuntimeError("Failed to load created channel")
    return created


def update_channel(
    settings: Settings,
    channel_id: int,
    *,
    display_name: str | None,
    enabled: bool,
    output_template: str | None,
    stream_password_enc: str | None,
    preferred_quality: str,
) -> dict[str, Any] | None:
    timestamp = now_utc().isoformat()

    with connect(settings) as conn:
        cursor = conn.execute(
            """
            UPDATE channels
            SET
              display_name = ?,
              enabled = ?,
              output_template = ?,
              stream_password_enc = ?,
              preferred_quality = ?,
              updated_at = ?
            WHERE id = ?
            """,
            (
                display_name,
                1 if enabled else 0,
                output_template,
                stream_password_enc,
                preferred_quality,
                timestamp,
                channel_id,
            ),
        )
        conn.commit()

    if cursor.rowcount == 0:
        return None
    return get_channel(settings, channel_id)


def delete_channel(settings: Settings, channel_id: int) -> bool:
    with connect(settings) as conn:
        cursor = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.commit()

    return cursor.rowcount > 0


def update_probe_state(
    settings: Settings,
    channel_id: int,
    *,
    last_status: str,
    last_broad_no: int | None,
    last_probe_at: str,
    last_error: str | None,
    offline_streak: int,
) -> None:
    timestamp = now_utc().isoformat()
    with connect(settings) as conn:
        conn.execute(
            """
            UPDATE channels
            SET
              last_status = ?,
              last_broad_no = ?,
              last_probe_at = ?,
              last_error = ?,
              offline_streak = ?,
              updated_at = ?
            WHERE id = ?
            """,
            (
                last_status,
                last_broad_no,
                last_probe_at,
                last_error,
                offline_streak,
                timestamp,
                channel_id,
            ),
        )
        conn.commit()
