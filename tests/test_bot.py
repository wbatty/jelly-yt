from jelly_yt.bot import handle_link
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
    fake_ytdlp.playlists["WL"] = PlaylistListing("WL", "Watch later", entries=[PlaylistEntry(vid(1), "One")])
    text, reports = handle_link(pipeline, Link("playlist", "WL"))
    assert "Watch later" in text and "1 added" in text
    assert pipeline.db.get_target("WL").target_type == "playlist"

    fake_ytdlp.errors[vid(2)] = AuthRequired("cookies")
    text, _ = handle_link(pipeline, Link("video", vid(2)))
    assert "retry" in text


def test_unknown_playlist_is_not_tracked(pipeline):
    text, _ = handle_link(pipeline, Link("playlist", "PLdoesnotexist"))
    assert "Not available" in text
    assert pipeline.db.get_target("PLdoesnotexist") is None
