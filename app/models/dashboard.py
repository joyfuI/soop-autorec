from __future__ import annotations

from app.config import Settings
from app.db import connect


def fetch_dashboard_summary(settings: Settings) -> dict[str, int]:
    with connect(settings) as conn:
        total_channels = conn.execute("SELECT COUNT(*) AS count FROM channels").fetchone()["count"]
        enabled_channels = conn.execute(
            "SELECT COUNT(*) AS count FROM channels WHERE enabled = 1"
        ).fetchone()["count"]
        recording_channels = conn.execute(
            "SELECT COUNT(*) AS count FROM channels WHERE last_status = 'recording'"
        ).fetchone()["count"]
        error_channels = conn.execute(
            "SELECT COUNT(*) AS count FROM channels WHERE last_status = 'error'"
        ).fetchone()["count"]

    return {
        "total_channels": int(total_channels),
        "enabled_channels": int(enabled_channels),
        "recording_channels": int(recording_channels),
        "error_channels": int(error_channels),
    }
