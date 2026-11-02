"""SQLite state store (seen.db)."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tracked_targets (
    target_id TEXT PRIMARY KEY, -- Channel ID or Playlist ID
    target_type TEXT NOT NULL,  -- 'channel' | 'playlist'
    display_name TEXT NOT NULL,
    custom_folder TEXT
);

-- Pipeline bookkeeping beyond the core schema.
CREATE TABLE IF NOT EXISTS target_state (
    target_id TEXT PRIMARY KEY,
    backfill_cutoff TEXT,      -- ISO timestamp; feed entries older than this are ignored
    feed_fingerprint TEXT,     -- hash of the last seen playlist feed (cheap change detection)
    last_full_sync TEXT,       -- ISO timestamp of the last full playlist sync
    last_polled_at TEXT
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    file_stem TEXT NOT NULL,   -- path relative to MEDIA_ROOT, without extension
    PRIMARY KEY (playlist_id, video_id)
);

-- Videos that could not be ingested yet (upcoming premieres, transient or permanent errors).
CREATE TABLE IF NOT EXISTS deferred_videos (
    video_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT NOT NULL,   -- ISO timestamp
    gave_up INTEGER NOT NULL DEFAULT 0
);

-- Stable folder per show (survives channel renames; honours custom_folder).
CREATE TABLE IF NOT EXISTS show_folders (
    show_id TEXT PRIMARY KEY,   -- channel ID or playlist ID
    folder TEXT NOT NULL UNIQUE
);
"""


@dataclass(frozen=True)
class Target:
    target_id: str
    target_type: str
    display_name: str
    custom_folder: str | None = None


@dataclass(frozen=True)
class TargetState:
    target_id: str
    backfill_cutoff: str | None = None
    feed_fingerprint: str | None = None
    last_full_sync: str | None = None
    last_polled_at: str | None = None


@dataclass(frozen=True)
class PlaylistItem:
    playlist_id: str
    video_id: str
    position: int
    file_stem: str


class Database:
    """Thin, thread-safe wrapper around the SQLite state file."""

    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # -- processed_videos ---------------------------------------------------
    def is_processed(self, video_id: str) -> bool:
        return self._exec("SELECT 1 FROM processed_videos WHERE video_id = ?", (video_id,)).fetchone() is not None

    def unseen(self, video_ids: list[str]) -> list[str]:
        """Return the subset of ``video_ids`` not yet in processed_videos (order preserved)."""
        if not video_ids:
            return []
        placeholders = ",".join("?" * len(video_ids))
        rows = self._exec(
            f"SELECT video_id FROM processed_videos WHERE video_id IN ({placeholders})", tuple(video_ids)
        ).fetchall()
        seen = {r["video_id"] for r in rows}
        return [v for v in video_ids if v not in seen]

    def mark_processed(self, video_id: str, channel_id: str, title: str, published_at: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO processed_videos (video_id, channel_id, title, published_at) VALUES (?, ?, ?, ?)",
            (video_id, channel_id, title, published_at),
        )

    # -- deferred_videos ------------------------------------------------------
    def defer(self, video_id: str, reason: str, retry_after: str, count_attempt: bool, max_attempts: int) -> None:
        row = self._exec("SELECT attempts FROM deferred_videos WHERE video_id = ?", (video_id,)).fetchone()
        attempts = (row["attempts"] if row else 0) + (1 if count_attempt else 0)
        self._exec(
            "INSERT OR REPLACE INTO deferred_videos (video_id, reason, attempts, retry_after, gave_up) "
            "VALUES (?, ?, ?, ?, ?)",
            (video_id, reason[:500], attempts, retry_after, int(attempts >= max_attempts)),
        )

    def clear_deferred(self, video_id: str) -> None:
        self._exec("DELETE FROM deferred_videos WHERE video_id = ?", (video_id,))

    def blocked(self, video_ids: list[str], now_iso: str) -> set[str]:
        """IDs that must not be retried yet (back-off pending or given up)."""
        if not video_ids:
            return set()
        placeholders = ",".join("?" * len(video_ids))
        rows = self._exec(
            f"SELECT video_id FROM deferred_videos WHERE video_id IN ({placeholders}) "
            "AND (gave_up = 1 OR retry_after > ?)",
            (*video_ids, now_iso),
        ).fetchall()
        return {r["video_id"] for r in rows}

    # -- show_folders ---------------------------------------------------------
    def get_show_folder(self, show_id: str) -> str | None:
        row = self._exec("SELECT folder FROM show_folders WHERE show_id = ?", (show_id,)).fetchone()
        return row["folder"] if row else None

    def folder_owner(self, folder: str) -> str | None:
        row = self._exec("SELECT show_id FROM show_folders WHERE folder = ?", (folder,)).fetchone()
        return row["show_id"] if row else None

    def set_show_folder(self, show_id: str, folder: str) -> None:
        self._exec("INSERT OR REPLACE INTO show_folders (show_id, folder) VALUES (?, ?)", (show_id, folder))

    # -- tracked_targets ----------------------------------------------------
    def add_target(self, target: Target) -> None:
        self._exec(
            "INSERT INTO tracked_targets (target_id, target_type, display_name, custom_folder) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(target_id) DO UPDATE SET display_name = excluded.display_name, "
            "custom_folder = COALESCE(excluded.custom_folder, tracked_targets.custom_folder)",
            (target.target_id, target.target_type, target.display_name, target.custom_folder),
        )

    def remove_target(self, target_id: str) -> bool:
        cur = self._exec("DELETE FROM tracked_targets WHERE target_id = ?", (target_id,))
        self._exec("DELETE FROM target_state WHERE target_id = ?", (target_id,))
        return cur.rowcount > 0

    def get_target(self, target_id: str) -> Target | None:
        row = self._exec("SELECT * FROM tracked_targets WHERE target_id = ?", (target_id,)).fetchone()
        return Target(**dict(row)) if row else None

    def list_targets(self, target_type: str | None = None) -> list[Target]:
        if target_type:
            rows = self._exec(
                "SELECT * FROM tracked_targets WHERE target_type = ? ORDER BY display_name", (target_type,)
            ).fetchall()
        else:
            rows = self._exec("SELECT * FROM tracked_targets ORDER BY target_type, display_name").fetchall()
        return [Target(**dict(r)) for r in rows]

    # -- target_state -------------------------------------------------------
    def get_state(self, target_id: str) -> TargetState:
        row = self._exec("SELECT * FROM target_state WHERE target_id = ?", (target_id,)).fetchone()
        return TargetState(**dict(row)) if row else TargetState(target_id=target_id)

    def update_state(self, target_id: str, **fields: str | None) -> None:
        allowed = {"backfill_cutoff", "feed_fingerprint", "last_full_sync", "last_polled_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown target_state fields: {unknown}")
        self._exec("INSERT OR IGNORE INTO target_state (target_id) VALUES (?)", (target_id,))
        for key, value in fields.items():
            self._exec(f"UPDATE target_state SET {key} = ? WHERE target_id = ?", (value, target_id))

    # -- playlist_items -----------------------------------------------------
    def playlist_items(self, playlist_id: str) -> dict[str, PlaylistItem]:
        rows = self._exec("SELECT * FROM playlist_items WHERE playlist_id = ?", (playlist_id,)).fetchall()
        return {r["video_id"]: PlaylistItem(**dict(r)) for r in rows}

    def upsert_playlist_item(self, item: PlaylistItem) -> None:
        self._exec(
            "INSERT OR REPLACE INTO playlist_items (playlist_id, video_id, position, file_stem) VALUES (?, ?, ?, ?)",
            (item.playlist_id, item.video_id, item.position, item.file_stem),
        )

    def delete_playlist_item(self, playlist_id: str, video_id: str) -> None:
        self._exec("DELETE FROM playlist_items WHERE playlist_id = ? AND video_id = ?", (playlist_id, video_id))
