import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from app.config import Settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL UNIQUE,
  display_name TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  skip_subscription_plus INTEGER NOT NULL DEFAULT 0,
  output_template TEXT,
  stream_password TEXT,
  preferred_quality TEXT NOT NULL DEFAULT 'best',
  last_status TEXT NOT NULL DEFAULT 'offline',
  last_broad_no INTEGER,
  last_probe_at TEXT,
  last_error TEXT,
  offline_streak INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  broad_no INTEGER NOT NULL,
  broad_title TEXT NOT NULL DEFAULT '',
  broad_start_at TEXT,
  status TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  recording_started_at TEXT,
  recording_stopped_at TEXT,
  final_path TEXT,
  temp_path TEXT,
  file_size_bytes INTEGER,
  ffmpeg_exit_code INTEGER,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_recordings_channel_status ON recordings(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_recordings_user_broad_status
ON recordings(user_id, broad_no, status);
"""


def initialize_database(settings: Settings) -> None:
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    Path(settings.output_root_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.temp_root_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.cookies_dir).mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript(SCHEMA_SQL)
        _migrate_schema(conn)
        conn.commit()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_recordings_stopped_at;")
    _migrate_channels_table(conn)
    _migrate_settings_table(conn)
    _migrate_recordings_table(conn)


def _migrate_channels_table(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(channels)").fetchall()
        if row and len(row) >= 2
    }
    if "skip_subscription_plus" not in columns:
        conn.execute(
            "ALTER TABLE channels "
            "ADD COLUMN skip_subscription_plus INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_settings_table(conn: sqlite3.Connection) -> None:
    columns = [
        str(row[1])
        for row in conn.execute("PRAGMA table_info(settings)").fetchall()
        if row and len(row) >= 2
    ]
    if columns == ["key", "value"]:
        return

    conn.execute("ALTER TABLE settings RENAME TO settings_old")
    conn.execute(
        """
        CREATE TABLE settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """
    )

    if {"key", "value"}.issubset(set(columns)):
        conn.execute(
            """
            INSERT OR REPLACE INTO settings (key, value)
            SELECT key, value FROM settings_old
            """
        )

    conn.execute("DROP TABLE settings_old")


def _migrate_recordings_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'recordings'"
    ).fetchone()
    table_sql = str(row[0] or "") if row is not None else ""
    normalized_sql = "".join(table_sql.lower().split())
    if "unique(user_id,broad_no)" not in normalized_sql:
        return

    conn.execute("DROP INDEX IF EXISTS idx_recordings_channel_status;")
    conn.execute("DROP INDEX IF EXISTS idx_recordings_user_broad_status;")
    conn.execute("ALTER TABLE recordings RENAME TO recordings_old")
    conn.execute(
        """
        CREATE TABLE recordings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id INTEGER NOT NULL,
          user_id TEXT NOT NULL,
          broad_no INTEGER NOT NULL,
          broad_title TEXT NOT NULL DEFAULT '',
          broad_start_at TEXT,
          status TEXT NOT NULL,
          detected_at TEXT NOT NULL,
          recording_started_at TEXT,
          recording_stopped_at TEXT,
          final_path TEXT,
          temp_path TEXT,
          file_size_bytes INTEGER,
          ffmpeg_exit_code INTEGER,
          error_message TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO recordings (
          id, channel_id, user_id, broad_no, broad_title, broad_start_at,
          status, detected_at, recording_started_at, recording_stopped_at,
          final_path, temp_path, file_size_bytes, ffmpeg_exit_code,
          error_message, created_at, updated_at
        )
        SELECT
          id, channel_id, user_id, broad_no, broad_title, broad_start_at,
          status, detected_at, recording_started_at, recording_stopped_at,
          final_path, temp_path, file_size_bytes, ffmpeg_exit_code,
          error_message, created_at, updated_at
        FROM recordings_old
        """
    )
    conn.execute("DROP TABLE recordings_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recordings_channel_status "
        "ON recordings(channel_id, status);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recordings_user_broad_status "
        "ON recordings(user_id, broad_no, status);"
    )


@contextmanager
def connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
    finally:
        conn.close()


def database_ping(settings: Settings) -> bool:
    try:
        with connect(settings) as conn:
            conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False

