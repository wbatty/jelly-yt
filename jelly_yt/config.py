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
    # Storage & paths
    media_root: Path = Path("/srv/media/youtube")
    cookies_path: Path | None = Path("/srv/media/youtube/cookies.txt")
    database_path: Path = Path("/srv/media/youtube/data/seen.db")

    # Proxy
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8080
    proxy_bind: str = "0.0.0.0"
    proxy_public_url: str | None = None  # overrides http://PROXY_HOST:PROXY_PORT in .strm files
    proxy_mode: str = "redirect"  # 'redirect' (HTTP 302) | 'stream' (chunked reverse proxy)
    proxy_format: str | None = None  # yt-dlp format override; default depends on proxy_mode

    # Jellyfin
    jellyfin_url: str | None = "http://127.0.0.1:8096"
    jellyfin_api_key: str | None = None
    # Path of MEDIA_ROOT as seen from inside the Jellyfin server (for targeted rescans).
    jellyfin_media_root: str | None = None

    # Telegram
    telegram_bot_token: str | None = None
    allowed_telegram_user_id: int | None = None

    # Sync behaviour
    initial_backfill_limit: int = 10
    poll_interval_minutes: int = 60
    feed_concurrency: int = 16
    playlist_full_resync_hours: int = 24
    http_timeout_seconds: float = 15.0

    extra: dict = field(default_factory=dict)

    @property
    def proxy_base_url(self) -> str:
        if self.proxy_public_url:
            return self.proxy_public_url.rstrip("/")
        return f"http://{self.proxy_host}:{self.proxy_port}"

    @classmethod
    def from_env(cls) -> "Settings":
        media_root = Path(_env("MEDIA_ROOT", "/srv/media/youtube"))
        cookies = _env("COOKIES_PATH", str(media_root / "cookies.txt"))
        allowed = _env("ALLOWED_TELEGRAM_USER_ID")
        mode = (_env("PROXY_MODE", "redirect") or "redirect").lower()
        if mode not in {"redirect", "stream"}:
            raise ValueError("PROXY_MODE must be 'redirect' or 'stream'")
        return cls(
            media_root=media_root,
            cookies_path=Path(cookies) if cookies else None,
            database_path=Path(_env("DATABASE_PATH", str(media_root / "data" / "seen.db"))),
            proxy_host=_env("PROXY_HOST", "127.0.0.1"),
            proxy_port=_env_int("PROXY_PORT", 8080),
            proxy_bind=_env("PROXY_BIND", "0.0.0.0"),
            proxy_public_url=_env("PROXY_PUBLIC_URL"),
            proxy_mode=mode,
            proxy_format=_env("PROXY_FORMAT"),
            jellyfin_url=_env("JELLYFIN_URL"),
            jellyfin_api_key=_env("JELLYFIN_API_KEY"),
            jellyfin_media_root=_env("JELLYFIN_MEDIA_ROOT"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            allowed_telegram_user_id=int(allowed) if allowed else None,
            initial_backfill_limit=_env_int("INITIAL_BACKFILL_LIMIT", 10),
            poll_interval_minutes=_env_int("POLL_INTERVAL_MINUTES", 60),
            feed_concurrency=_env_int("FEED_CONCURRENCY", 16),
            playlist_full_resync_hours=_env_int("PLAYLIST_FULL_RESYNC_HOURS", 24),
            http_timeout_seconds=float(_env("HTTP_TIMEOUT_SECONDS", "15")),
        )

    def usable_cookies(self) -> Path | None:
        """Return the cookies path only if the file actually exists."""
        if self.cookies_path and self.cookies_path.is_file():
            return self.cookies_path
        return None
