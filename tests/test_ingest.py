from datetime import timedelta

import pytest

from jelly_yt.naming import utcnow
from jelly_yt.ytdlp import AuthRequired, ChannelInfo, PlaylistEntry, PlaylistListing, VideoUnavailable

from .conftest import CHANNEL_ID, JPEG, base_time, days, vid


def files_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".jelly-yt" not in p.parts)


def test_single_video_writes_ytdlp2strm_stub(pipeline, fake_ytdlp, settings):
    fake_ytdlp.add_video("dQw4w9WgXcQ", "Why Gravity is Weird", base_time())
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium", "An element of truth",
                                                  "https://yt3/avatar.jpg")

    result = pipeline.add_video("dQw4w9WgXcQ")

    assert result.status == "added"
    root = settings.media_root
    assert files_under(root) == [
        "Veritasium/Season 2026/S2026E1015 - Why Gravity is Weird-thumb.jpg",
        "Veritasium/Season 2026/S2026E1015 - Why Gravity is Weird.nfo",
        "Veritasium/Season 2026/S2026E1015 - Why Gravity is Weird.strm",
        "Veritasium/poster.jpg",
        "Veritasium/tvshow.nfo",
    ]
    ep = root / "Veritasium/Season 2026/S2026E1015 - Why Gravity is Weird"
    assert ep.with_name(ep.name + ".strm").read_text() == "http://10.0.0.5:5005/youtube/direct/dQw4w9WgXcQ\n"
    nfo = ep.with_name(ep.name + ".nfo").read_text()
    assert "<season>2026</season>" in nfo and "<episode>1015</episode>" in nfo and "<runtime>18</runtime>" in nfo
    assert "<plot>An element of truth</plot>" in (root / "Veritasium/tvshow.nfo").read_text()
    assert (root / "Veritasium/poster.jpg").read_bytes() == JPEG
    assert pipeline.db.pending_requests() == []

    calls = fake_ytdlp.calls
    assert pipeline.add_video("dQw4w9WgXcQ").status == "exists"
    assert fake_ytdlp.calls == calls


def test_same_day_same_title_collision_is_disambiguated(pipeline, fake_ytdlp, settings):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    fake_ytdlp.add_video(vid(1), "Live Q&A", base_time())
    fake_ytdlp.add_video(vid(2), "Live Q&A", base_time() + timedelta(hours=2))
    pipeline.add_video(vid(1))
    pipeline.add_video(vid(2))
    strms = [p for p in files_under(settings.media_root) if p.endswith(".strm")]
    assert strms == [
        "Veritasium/Season 2026/S2026E1015 - Live Q&A [vid00000002].strm",
        "Veritasium/Season 2026/S2026E1015 - Live Q&A.strm",
    ]


def test_waiting_single_video_is_retried_by_sync_all(pipeline, fake_ytdlp):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    fake_ytdlp.errors[vid(1)] = AuthRequired("Sign in to confirm you're not a bot")
    assert pipeline.add_video(vid(1)).status == "deferred"
    assert pipeline.db.pending_requests() == [vid(1)]

    del fake_ytdlp.errors[vid(1)]
    fake_ytdlp.add_video(vid(1), "Now works", base_time())
    reports = pipeline.sync_all()
    assert [r.video_id for r in reports[-1].added] == [vid(1)]
    assert pipeline.db.pending_requests() == []


def test_upcoming_and_unavailable_videos(pipeline, fake_ytdlp):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    premiere = utcnow() + timedelta(days=2)
    fake_ytdlp.add_video(vid(1), "Premiere", premiere, live_status="is_upcoming",
                         release_timestamp=int(premiere.timestamp()))
    fake_ytdlp.errors[vid(2)] = VideoUnavailable("Private video")

    assert pipeline.add_video(vid(1)).status == "deferred"
    assert pipeline.add_video(vid(2)).status == "unavailable"
    assert pipeline.db.pending_requests() == [vid(1)]  # unavailable ones are not retried forever
    assert pipeline.db.blocked([vid(1), vid(2)], utcnow().isoformat()) == {vid(1), vid(2)}
    calls = fake_ytdlp.calls
    pipeline.sync_all()
    assert fake_ytdlp.calls == calls  # premiere not due yet -> no probe


def test_playlist_whole_list_add_reorder_prune(pipeline, fake_ytdlp, settings):
    pid = "PLtest123"
    for i in range(1, 30):
        fake_ytdlp.add_video(vid(i), f"Track {i}", base_time() - days(i))
    fake_ytdlp.playlists[pid] = PlaylistListing(
        pid, "Bedtime Stories", "Calm ones", "Me", None,
        [PlaylistEntry(vid(i), f"Track {i}") for i in range(1, 26)],
    )
    target, report = pipeline.track_playlist(pid)
    assert target.display_name == "Bedtime Stories"
    assert len(report.added) == 25  # whole playlist, no 10-item cap
    folder = settings.media_root / "Bedtime Stories [Playlist]"
    assert (folder / "tvshow.nfo").exists() and (folder / "poster.jpg").exists()
    assert (folder / "Season 01" / "S01E25 - Track 25.strm").read_text() == \
        f"http://10.0.0.5:5005/youtube/direct/{vid(25)}\n"

    # Upstream: remove track 1, add track 29 at the front.
    fake_ytdlp.playlists[pid].entries = [PlaylistEntry(vid(29), "Track 29")] + [
        PlaylistEntry(vid(i), f"Track {i}") for i in range(2, 26)]
    report = pipeline.sync_playlist(pid)
    assert report.removed == [vid(1)]
    assert [r.video_id for r in report.added] == [vid(29)]
    assert report.renumbered == 0
    assert (folder / "Season 01" / "S01E01 - Track 29.strm").exists()
    assert not list((folder / "Season 01").glob("*Track 1.*"))

    # Reorder -> renamed in place, nfo episode numbers rewritten, only the listing call.
    calls = fake_ytdlp.calls
    fake_ytdlp.playlists[pid].entries = list(reversed(fake_ytdlp.playlists[pid].entries))
    report = pipeline.sync_playlist(pid)
    assert report.renumbered == 24 and not report.added  # middle item keeps its number
    assert fake_ytdlp.calls == calls + 1
    assert "<episode>1</episode>" in (folder / "Season 01" / "S01E01 - Track 25.nfo").read_text()
    assert not list((folder / "Season 01").glob(".renaming-*"))


def test_unknown_playlist_is_not_tracked(pipeline):
    with pytest.raises(VideoUnavailable):
        pipeline.track_playlist("PLnope")
    assert pipeline.db.get_target("PLnope") is None


def test_sync_all_survives_broken_playlist(pipeline, fake_ytdlp):
    fake_ytdlp.add_video(vid(1), "One", base_time())
    fake_ytdlp.playlists["PLok"] = PlaylistListing("PLok", "OK", entries=[PlaylistEntry(vid(1), "One")])
    pipeline.track_playlist("PLok")
    del fake_ytdlp.playlists["PLok"]
    reports = pipeline.sync_all()
    assert reports[0].error


def test_library_refuses_path_escape(pipeline):
    with pytest.raises(ValueError):
        pipeline.library.path("../outside")
