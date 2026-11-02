"""Filesystem naming conventions for the Jellyfin *Shows* layout."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# Leave room for "S2026E1015 - ", "-thumb.jpg" and filesystem limits (255 bytes).
MAX_COMPONENT_BYTES = 180


def is_valid_video_id(value: str | None) -> bool:
    return bool(value) and VIDEO_ID_RE.match(value) is not None


def sanitize(name: str | None, fallback: str = "Untitled", max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Make ``name`` safe for use as a single path component on Linux/SMB/NTFS shares."""
    text = unicodedata.normalize("NFC", name or "")
    text = _INVALID_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    if not text:
        text = fallback
    if text.upper().split(".")[0] in _WINDOWS_RESERVED:
        text = f"_{text}"
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    return text


def channel_folder(display_name: str, custom_folder: str | None = None) -> str:
    return sanitize(custom_folder or display_name)


def playlist_folder(title: str, custom_folder: str | None = None) -> str:
    if custom_folder:
        return sanitize(custom_folder)
    return f"{sanitize(title, max_bytes=MAX_COMPONENT_BYTES - 11)} [Playlist]"


def channel_episode_stem(published: datetime, title: str) -> str:
    """``Season <YYYY>/S<YYYY>E<MMDD> - <Title>`` (relative to the show folder)."""
    year = f"{published.year:04d}"
    episode = f"{published.month:02d}{published.day:02d}"
    return f"Season {year}/S{year}E{episode} - {sanitize(title)}"


def episode_width(total: int) -> int:
    return max(2, len(str(max(total, 1))))


def playlist_episode_stem(index: int, title: str, total: int) -> str:
    """``Season 01/S01E<NN> - <Title>`` (relative to the show folder)."""
    return f"Season 01/S01E{index:0{episode_width(total)}d} - {sanitize(title)}"


def parse_datetime(value: str | int | float | None) -> datetime | None:
    """Parse ISO-8601 strings, ``YYYYMMDD`` strings or POSIX timestamps into aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
