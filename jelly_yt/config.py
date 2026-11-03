"""Environment-driven configuration (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # Where the bot writes curated playlists / single videos (its own Jellyfin library).
    media_root: Path = Path("/media/YoutubeCurated")
    database_path: Path = Path("/media/YoutubeCurated/.jelly-yt/state.db")
    cookies_path: Path | None = None

    # ytdlp2STRM integration
    ytdlp2strm_url: str = "http://127.0.0.1:5005"  # base URL written into .strm files
    ytdlp2strm_method: str = "direct"  # 'direct' | 'bridge' (ytdlp2STRM playback endpoints)
    channel_list_path: Path | None = None  # ytdlp2STRM plugins/youtube/channel_list.json

    # Jellyfin
    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None
    # Path of MEDIA_ROOT as seen from inside the Jellyfin server (for targeted rescans).
    jellyfin_media_root: str | None = None

    # Telegram
    telegram_bot_token: str | None = None
    allowed_telegram_user_ids: frozenset[int] = field(default_factory=frozenset)

    # Behaviour
    playlist_sync_interval_minutes: int = 60
    http_timeout_seconds: float = 15.0

    def strm_url(self, video_id: str) -> str:
        return f"{self.ytdlp2strm_url.rstrip('/')}/youtube/{self.ytdlp2strm_method}/{video_id}"

    @classmethod
    def from_env(cls) -> "Settings":
        media_root = Path(_env("MEDIA_ROOT", "/media/YoutubeCurated"))
        cookies = _env("COOKIES_PATH")
        channel_list = _env("YTDLP2STRM_CHANNEL_LIST")
        method = (_env("YTDLP2STRM_METHOD", "direct") or "direct").lower()
        if method not in {"direct", "bridge"}:
            raise ValueError("YTDLP2STRM_METHOD must be 'direct' or 'bridge'")
        users = _env("ALLOWED_TELEGRAM_USER_IDS") or _env("ALLOWED_TELEGRAM_USER_ID") or ""
        try:
            allowed = frozenset(int(u) for u in users.replace(" ", "").split(",") if u)
        except ValueError as exc:
            raise ValueError("ALLOWED_TELEGRAM_USER_IDS must be comma-separated numeric IDs") from exc
        return cls(
            media_root=media_root,
            database_path=Path(_env("DATABASE_PATH", str(media_root / ".jelly-yt" / "state.db"))),
            cookies_path=Path(cookies) if cookies else None,
            ytdlp2strm_url=_env("YTDLP2STRM_URL", "http://127.0.0.1:5005"),
            ytdlp2strm_method=method,
            channel_list_path=Path(channel_list) if channel_list else None,
            jellyfin_url=_env("JELLYFIN_URL"),
            jellyfin_api_key=_env("JELLYFIN_API_KEY"),
            jellyfin_media_root=_env("JELLYFIN_MEDIA_ROOT"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            allowed_telegram_user_ids=allowed,
            playlist_sync_interval_minutes=_env_int("PLAYLIST_SYNC_INTERVAL_MINUTES", 60),
            http_timeout_seconds=float(_env("HTTP_TIMEOUT_SECONDS", "15")),
        )
