"""YouTube Atom feed ingestion (no API key, no yt-dlp)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .naming import parse_datetime

log = logging.getLogger(__name__)

CHANNEL_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
PLAYLIST_FEED = "https://www.youtube.com/feeds/videos.xml?playlist_id={}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass
class FeedEntry:
    video_id: str
    channel_id: str
    title: str
    published: datetime | None
    author: str = ""
    description: str = ""


@dataclass
class Feed:
    title: str
    channel_id: str = ""
    author: str = ""
    entries: list[FeedEntry] = field(default_factory=list)

    def fingerprint(self) -> str:
        return hashlib.sha256("\n".join(e.video_id for e in self.entries).encode()).hexdigest()


class FeedError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(xml_bytes: bytes | str) -> Feed:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FeedError(f"Malformed feed XML: {exc}") from exc
    feed = Feed(
        title=_text(root, "atom:title"),
        channel_id=_text(root, "yt:channelId"),
        author=_text(root, "atom:author/atom:name"),
    )
    for entry in root.findall("atom:entry", NS):
        video_id = _text(entry, "yt:videoId")
        if not video_id:
            continue
        feed.entries.append(
            FeedEntry(
                video_id=video_id,
                channel_id=_text(entry, "yt:channelId") or feed.channel_id,
                title=_text(entry, "atom:title"),
                published=parse_datetime(_text(entry, "atom:published")),
                author=_text(entry, "atom:author/atom:name"),
                description=_text(entry, "media:group/media:description"),
            )
        )
    return feed


def feed_url(target_type: str, target_id: str) -> str:
    return (PLAYLIST_FEED if target_type == "playlist" else CHANNEL_FEED).format(target_id)


async def fetch_feed(client: httpx.AsyncClient, target_type: str, target_id: str) -> Feed:
    url = feed_url(target_type, target_id)
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise FeedError(f"{url}: {exc}") from exc
    if resp.status_code != 200:
        raise FeedError(f"{url}: HTTP {resp.status_code}", status=resp.status_code)
    return parse_feed(resp.content)


async def fetch_feeds(
    targets: list[tuple[str, str]], concurrency: int = 16, timeout: float = 15.0
) -> dict[str, Feed | FeedError]:
    """Fetch many feeds concurrently. ``targets`` is ``[(target_type, target_id), ...]``."""
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, Feed | FeedError] = {}

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "jelly-yt/1.0 (+feed poller)"},
    ) as client:

        async def one(target_type: str, target_id: str) -> None:
            async with sem:
                try:
                    results[target_id] = await fetch_feed(client, target_type, target_id)
                except FeedError as exc:
                    log.warning("Feed fetch failed for %s %s: %s", target_type, target_id, exc)
                    results[target_id] = exc

        await asyncio.gather(*(one(t, i) for t, i in targets))
    return results
