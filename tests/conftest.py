from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from jelly_yt.config import Settings
from jelly_yt.db import Database
from jelly_yt.ingest import Pipeline
from jelly_yt.ytdlp import ChannelInfo, PlaylistEntry, PlaylistListing, VideoUnavailable

JPEG = b"\xff\xd8\xff\xe0fakejpeg"
CHANNEL_ID = "UCHnyfMqiRRG1u-2MsSQLbXA"


def vid(n: int) -> str:
    return f"vid{n:08d}"  # 11 chars


class FakeYtDlp:
    """Stands in for YtDlpClient; counts every call like the real one."""

    def __init__(self):
        self.calls = 0
        self.videos: dict[str, dict] = {}
        self.playlists: dict[str, PlaylistListing] = {}
        self.channels: dict[str, ChannelInfo] = {}
        self.errors: dict[str, Exception] = {}

    def add_video(self, video_id: str, title: str, when: datetime, channel_id: str = CHANNEL_ID,
                  channel: str = "Veritasium", **extra) -> dict:
        info = {
            "id": video_id,
            "title": title,
            "description": f"About {title}",
            "duration": 1080,
            "channel": channel,
            "channel_id": channel_id,
            "timestamp": int(when.timestamp()),
            "categories": ["Education"],
            "live_status": "not_live",
            **extra,
        }
        self.videos[video_id] = info
        return info

    def probe_video(self, video_id):
        self.calls += 1
        if video_id in self.errors:
            raise self.errors[video_id]
        if video_id not in self.videos:
            raise VideoUnavailable(f"Video unavailable: {video_id}")
        return self.videos[video_id]

    def flat_playlist(self, playlist_id):
        self.calls += 1
        if playlist_id not in self.playlists:
            raise VideoUnavailable("The playlist does not exist")
        return self.playlists[playlist_id]

    def channel_info(self, channel_url, recent=0):
        self.calls += 1
        for info in self.channels.values():
            if channel_url.rstrip("/").endswith((info.channel_id, info.name.replace(" ", ""))):
                return replace(info, recent_video_ids=info.recent_video_ids[:recent])
        raise VideoUnavailable("no channel")

    def channel_info_by_id(self, channel_id, recent=0):
        return self.channel_info(f"https://www.youtube.com/channel/{channel_id}", recent)

    def close(self):
        pass


def image_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "maxresdefault" in request.url.path and "nomaxres" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    return httpx.MockTransport(handler)


class FakeJellyfin:
    def __init__(self):
        self.refreshes: list[list[Path] | None] = []

    def refresh(self, changed_paths=None):
        self.refreshes.append(changed_paths)
        return True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        media_root=tmp_path / "media",
        database_path=tmp_path / "data" / "state.db",
        ytdlp2strm_url="http://10.0.0.5:5005/",
        channel_list_path=tmp_path / "ytdlp2strm" / "channel_list.json",
        jellyfin_url="http://jf:8096",
        jellyfin_api_key="key",
        allowed_telegram_user_ids=frozenset({42}),
    )


@pytest.fixture
def fake_ytdlp() -> FakeYtDlp:
    return FakeYtDlp()


@pytest.fixture
def jellyfin() -> FakeJellyfin:
    return FakeJellyfin()


@pytest.fixture
def pipeline(settings, fake_ytdlp, jellyfin) -> Pipeline:
    settings.media_root.mkdir(parents=True)
    db = Database(settings.database_path)
    p = Pipeline(settings, db, fake_ytdlp, http=httpx.Client(transport=image_transport()), jellyfin=jellyfin)
    yield p
    db.close()


def base_time() -> datetime:
    return datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def days(n: int) -> timedelta:
    return timedelta(days=n)
