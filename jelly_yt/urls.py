"""Classify user-supplied YouTube links (Telegram bot / CLI input routing)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from .naming import is_valid_video_id

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
                 "www.youtube-nocookie.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}

_URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/\S*", re.I)
_HANDLE_RE = re.compile(r"(?<![\w/@.])@([A-Za-z0-9._-]{3,30})\b")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


@dataclass(frozen=True)
class Link:
    kind: str  # 'playlist' | 'channel' | 'video'
    value: str  # playlist ID | canonical channel URL | video ID

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def _playlist_from_query(query: dict[str, list[str]]) -> str | None:
    pid = (query.get("list") or [""])[0]
    # RD… are auto-generated "mixes" (endless, per-user); treat the link as the underlying video.
    if pid and _PLAYLIST_ID_RE.match(pid) and not pid.startswith("RD"):
        return pid
    return None


def classify(text: str) -> Link | None:
    raw = text.strip().rstrip(".,;)>]")
    if raw.startswith("@") and _HANDLE_RE.fullmatch(raw):
        return Link("channel", f"https://www.youtube.com/{raw}")
    if _CHANNEL_ID_RE.match(raw):
        return Link("channel", f"https://www.youtube.com/channel/{raw}")
    if is_valid_video_id(raw):
        return Link("video", raw)
    if raw.upper() in {"WL", "LL"}:
        return Link("playlist", raw.upper())

    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    parts = [p for p in parsed.path.split("/") if p]

    if host in SHORT_HOSTS:
        if parts and is_valid_video_id(parts[0]):
            playlist = _playlist_from_query(query)
            return Link("playlist", playlist) if playlist else Link("video", parts[0])
        return None
    if host not in YOUTUBE_HOSTS:
        return None

    playlist = _playlist_from_query(query)
    if playlist:
        return Link("playlist", playlist)

    if parts and parts[0] == "watch":
        vid = (query.get("v") or [""])[0]
        return Link("video", vid) if is_valid_video_id(vid) else None
    if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v", "e"} and is_valid_video_id(parts[1]):
        return Link("video", parts[1])
    if parts and parts[0].startswith("@") and len(parts[0]) > 1:
        return Link("channel", f"https://www.youtube.com/{parts[0]}")
    if len(parts) >= 2 and parts[0] == "channel" and _CHANNEL_ID_RE.match(parts[1]):
        return Link("channel", f"https://www.youtube.com/channel/{parts[1]}")
    if len(parts) >= 2 and parts[0] in {"c", "user"}:
        return Link("channel", f"https://www.youtube.com/{parts[0]}/{parts[1]}")
    return None


def extract_links(message: str) -> list[Link]:
    """All recognisable links/handles in a free-form message, de-duplicated, in order."""
    found: list[Link] = []
    candidates = [m.group(0) for m in _URL_RE.finditer(message)]
    stripped = _URL_RE.sub(" ", message)
    candidates += [f"@{m.group(1)}" for m in _HANDLE_RE.finditer(stripped)]
    if not candidates:
        candidates = message.split()
    for candidate in candidates:
        link = classify(candidate)
        if link and link not in found:
            found.append(link)
    return found
