from jelly_yt.bot import handle_link
from jelly_yt.channel_list import read_channels
from jelly_yt.config import Settings
from jelly_yt.urls import Link
from jelly_yt.ytdlp import AuthRequired, ChannelInfo, PlaylistEntry, PlaylistListing

from .conftest import CHANNEL_ID, base_time, vid


def test_video_link(pipeline, fake_ytdlp):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    fake_ytdlp.add_video("dQw4w9WgXcQ", "Why Gravity is Weird", base_time())
    text, reports = handle_link(pipeline, Link("video", "dQw4w9WgXcQ"))
    assert "Added" in text and "Veritasium" in text
    assert reports and reports[0].changed
    text, reports = handle_link(pipeline, Link("video", "dQw4w9WgXcQ"))
    assert "already" in text and reports == []


def test_playlist_link_and_auth_error(pipeline, fake_ytdlp):
    fake_ytdlp.add_video(vid(1), "One", base_time())
    fake_ytdlp.playlists["PLkids"] = PlaylistListing("PLkids", "Kids", entries=[PlaylistEntry(vid(1), "One")])
    text, reports = handle_link(pipeline, Link("playlist", "PLkids"))
    assert "Kids" in text and "1 videos added" in text
    assert pipeline.db.get_target("PLkids").target_type == "playlist"

    fake_ytdlp.errors[vid(2)] = AuthRequired("cookies")
    text, _ = handle_link(pipeline, Link("video", vid(2)))
    assert "keep trying" in text


def test_channel_goes_to_ytdlp2strm_list(pipeline, settings):
    text, reports = handle_link(pipeline, Link("channel", "https://www.youtube.com/@veritasium"))
    assert "restart" in text and reports == []
    assert read_channels(settings.channel_list_path) == ["https://www.youtube.com/@veritasium"]
    text, _ = handle_link(pipeline, Link("channel", "https://www.youtube.com/@veritasium"))
    assert "already" in text


def test_unknown_playlist(pipeline):
    text, _ = handle_link(pipeline, Link("playlist", "PLdoesnotexist"))
    assert "Not available" in text


def test_allowed_users_parsing(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "1, 2")
    assert Settings.from_env().allowed_telegram_user_ids == {1, 2}
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "7")
    assert Settings.from_env().allowed_telegram_user_ids == {7}


def test_application_builds_with_owner_filters(pipeline, settings):
    from dataclasses import replace

    from jelly_yt.bot import Bot

    app = Bot(replace(settings, telegram_bot_token="123:abc"), pipeline).build()
    handlers = app.handlers[0]
    assert len(handlers) == 5
    assert all(42 in h.filters.user_ids if hasattr(h.filters, "user_ids") else True for h in handlers[:4])
