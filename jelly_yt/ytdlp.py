"""In-process yt-dlp wrapper (metadata only – playback is served by ytdlp2STRM)."""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

log = logging.getLogger(__name__)

WATCH_URL = "https://www.youtube.com/watch?v={}"
PLAYLIST_URL = "https://www.youtube.com/playlist?list={}"
CHANNEL_VIDEOS_URL = "https://www.youtube.com/channel/{}/videos"

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}


class YtDlpError(Exception):
    """Base class for resolution failures."""


class VideoUnavailable(YtDlpError):
    """Permanent: removed, private, region-locked, members-only without access, ..."""


class AuthRequired(YtDlpError):
    """Cookies missing/expired, bot-check, or rate limiting (HTTP 429)."""


# Order matters: many "unavailable" messages also suggest "--cookies", so strong
# auth signals are checked first, then unavailability, then generic sign-in hints.
_STRONG_AUTH_MARKERS = (
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "http error 429",
    "too many requests",
    "rate-limit",
    "rate limit",
    "available to this channel's members",
    "join this channel",
    "members-only",
    "confirm your age",
)
_UNAVAILABLE_MARKERS = (
    "video unavailable",
    "private video",
    "has been removed",
    "does not exist",
    "is not available",
    "account associated with this video has been terminated",
    "incomplete youtube id",
    "not a valid url",
    "unsupported url",
    "http error 404",
)
_WEAK_AUTH_MARKERS = ("sign in", "cookies", "login required", "http error 403")


def classify_error(exc: BaseException) -> YtDlpError:
    message = str(exc).lower()
    if any(m in message for m in _STRONG_AUTH_MARKERS):
        return AuthRequired(str(exc))
    if any(m in message for m in _UNAVAILABLE_MARKERS):
        return VideoUnavailable(str(exc))
    if any(m in message for m in _WEAK_AUTH_MARKERS):
        return AuthRequired(str(exc))
    return YtDlpError(str(exc))


@dataclass
class ChannelInfo:
    channel_id: str
    name: str
    description: str = ""
    avatar_url: str | None = None
    recent_video_ids: list[str] = field(default_factory=list)


@dataclass
class PlaylistEntry:
    video_id: str
    title: str


@dataclass
class PlaylistListing:
    playlist_id: str
    title: str
    description: str = ""
    uploader: str = ""
    thumbnail_url: str | None = None
    entries: list[PlaylistEntry] = field(default_factory=list)


def _best_thumbnail(thumbnails: list[dict] | None, prefer_ids: tuple[str, ...] = ()) -> str | None:
    if not thumbnails:
        return None
    by_id = {t.get("id"): t for t in thumbnails if t.get("url")}
    for pid in prefer_ids:
        if pid in by_id:
            return by_id[pid]["url"]
    ranked = sorted(
        (t for t in thumbnails if t.get("url")),
        key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)),
    )
    return ranked[-1]["url"] if ranked else None


def _square_thumbnail(thumbnails: list[dict] | None) -> str | None:
    """Channel avatars are square; banners are wide. Prefer the largest square image."""
    if not thumbnails:
        return None
    url = _best_thumbnail(thumbnails, prefer_ids=("avatar_uncropped",))
    if url and any(t.get("id") == "avatar_uncropped" for t in thumbnails):
        return url
    squares = [t for t in thumbnails if t.get("url") and t.get("width") and t.get("width") == t.get("height")]
    if squares:
        return max(squares, key=lambda t: t["width"])["url"]
    return _best_thumbnail(thumbnails)


class _CookieCopy:
    """yt-dlp rewrites its cookie file on close; work on a private copy so the
    mounted ``cookies.txt`` may be read-only and is never clobbered."""

    def __init__(self, source: Path | None):
        self.source = source
        self._dir = tempfile.mkdtemp(prefix="jelly-yt-cookies-")
        self._mtime: float | None = None
        self._lock = threading.Lock()

    def path(self) -> str | None:
        if not self.source or not self.source.is_file():
            return None
        target = Path(self._dir) / "cookies.txt"
        with self._lock:
            mtime = self.source.stat().st_mtime
            if mtime != self._mtime or not target.exists():
                shutil.copyfile(self.source, target)
                target.chmod(0o600)
                self._mtime = mtime
        return str(target)

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


class YtDlpClient:
    def __init__(self, cookies_path: Path | None = None, extra_opts: dict[str, Any] | None = None):
        self._cookies = _CookieCopy(cookies_path)
        self._extra = dict(extra_opts or {})
        self.calls = 0  # number of extractor invocations (for idempotency checks / metrics)

    def _opts(self, **overrides: Any) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "logger": log,
        }
        cookiefile = self._cookies.path()
        if cookiefile:
            opts["cookiefile"] = cookiefile
        opts.update(self._extra)
        opts.update(overrides)
        return opts

    def _extract(self, url: str, **overrides: Any) -> dict[str, Any]:
        self.calls += 1
        try:
            with yt_dlp.YoutubeDL(self._opts(**overrides)) as ydl:
                info = ydl.extract_info(url, download=False)
                return ydl.sanitize_info(info) if info else {}
        except (DownloadError, ExtractorError) as exc:
            raise classify_error(exc) from exc

    # -- public API ------------------------------------------------------------
    def probe_video(self, video_id: str) -> dict[str, Any]:
        """Full metadata for one video (``yt-dlp --dump-json --skip-download``)."""
        return self._extract(WATCH_URL.format(video_id), ignore_no_formats_error=True)

    def flat_playlist(self, playlist_id: str) -> PlaylistListing:
        """``yt-dlp --flat-playlist`` – one request regardless of playlist size."""
        info = self._extract(PLAYLIST_URL.format(playlist_id), extract_flat="in_playlist", noplaylist=False)
        entries: list[PlaylistEntry] = []
        seen: set[str] = set()
        for entry in info.get("entries") or []:
            if not entry:
                continue
            vid = entry.get("id")
            title = entry.get("title") or ""
            if not vid or vid in seen or title.strip().lower() in UNAVAILABLE_TITLES:
                continue
            seen.add(vid)
            entries.append(PlaylistEntry(video_id=vid, title=title))
        return PlaylistListing(
            playlist_id=info.get("id") or playlist_id,
            title=info.get("title") or playlist_id,
            description=info.get("description") or "",
            uploader=info.get("uploader") or info.get("channel") or "",
            thumbnail_url=_best_thumbnail(info.get("thumbnails")),
            entries=entries,
        )

    def channel_info(self, channel_url: str, recent: int = 0) -> ChannelInfo:
        """Resolve a channel URL (``/@handle``, ``/channel/UC…``, ``/c/…``, ``/user/…``)."""
        url = channel_url.rstrip("/")
        if not url.endswith("/videos"):
            url = f"{url}/videos"
        info = self._extract(
            url,
            extract_flat="in_playlist",
            noplaylist=False,
            playlistend=max(recent, 1),
        )
        channel_id = info.get("channel_id") or info.get("uploader_id") or info.get("id")
        if not channel_id or not str(channel_id).startswith("UC"):
            raise VideoUnavailable(f"Could not resolve a channel ID from {channel_url}")
        recent_ids = [e["id"] for e in (info.get("entries") or []) if e and e.get("id")][:recent]
        name = info.get("channel") or info.get("uploader") or info.get("title") or channel_id
        if name.endswith(" - Videos"):
            name = name[: -len(" - Videos")]
        return ChannelInfo(
            channel_id=channel_id,
            name=name,
            description=info.get("description") or "",
            avatar_url=_square_thumbnail(info.get("thumbnails")),
            recent_video_ids=recent_ids,
        )

    def channel_info_by_id(self, channel_id: str, recent: int = 0) -> ChannelInfo:
        return self.channel_info(CHANNEL_VIDEOS_URL.format(channel_id), recent=recent)

    def close(self) -> None:
        self._cookies.cleanup()
