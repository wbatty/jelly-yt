from datetime import timedelta

import pytest

from jelly_yt import ingest as ingest_mod
from jelly_yt.db import Target
from jelly_yt.naming import utcnow
from jelly_yt.ytdlp import AuthRequired, ChannelInfo, PlaylistEntry, PlaylistListing, VideoUnavailable

from .conftest import CHANNEL_ID, JPEG, base_time, days, make_feed, vid


def files_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_single_video_ingest_writes_strm_nfo_thumb(pipeline, fake_ytdlp, settings):
    fake_ytdlp.add_video("dQw4w9WgXcQ", "Why Gravity is Weird", base_time())
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium", "An element of truth",
                                                  "https://yt3/avatar.jpg")

    result = pipeline.ingest_channel_video("dQw4w9WgXcQ")

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
    assert ep.with_name(ep.name + ".strm").read_text() == "http://10.0.0.5:8080/watch?v=dQw4w9WgXcQ\n"
    nfo = ep.with_name(ep.name + ".nfo").read_text()
    assert "<season>2026</season>" in nfo and "<episode>1015</episode>" in nfo
    assert "<runtime>18</runtime>" in nfo and "<aired>2026-10-15</aired>" in nfo
    assert "<genre>Education</genre>" in nfo
    show = (root / "Veritasium/tvshow.nfo").read_text()
    assert "<plot>An element of truth</plot>" in show and CHANNEL_ID in show
    assert (root / "Veritasium/poster.jpg").read_bytes() == JPEG
    assert pipeline.db.is_processed("dQw4w9WgXcQ")

    # Idempotent second ingest – no probe.
    calls = fake_ytdlp.calls
    assert pipeline.ingest_channel_video("dQw4w9WgXcQ").status == "exists"
    assert fake_ytdlp.calls == calls


def test_same_day_same_title_collision_is_disambiguated(pipeline, fake_ytdlp, settings):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    fake_ytdlp.add_video(vid(1), "Live Q&A", base_time())
    fake_ytdlp.add_video(vid(2), "Live Q&A", base_time() + timedelta(hours=2))
    pipeline.ingest_channel_video(vid(1))
    pipeline.ingest_channel_video(vid(2))
    strms = [p for p in files_under(settings.media_root) if p.endswith(".strm")]
    assert strms == [
        "Veritasium/Season 2026/S2026E1015 - Live Q&A [vid00000002].strm",
        "Veritasium/Season 2026/S2026E1015 - Live Q&A.strm",
    ]


def test_channel_backfill_then_idempotent_poll(pipeline, fake_ytdlp, settings):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    entries = []
    for i in range(6):  # newest first, like the real feed
        when = base_time() - days(i)
        fake_ytdlp.add_video(vid(i), f"Video {i}", when)
        entries.append((vid(i), f"Video {i}", when))
    feed = make_feed(entries)
    target = Target(CHANNEL_ID, "channel", "Veritasium")
    pipeline.db.add_target(target)

    report = pipeline.poll_channel(target, feed)
    assert [r.video_id for r in report.added] == [vid(2), vid(1), vid(0)]  # limit=3, oldest first
    assert pipeline.db.get_state(CHANNEL_ID).backfill_cutoff.startswith("2026-10-13")

    # AC-03: nothing new -> zero yt-dlp calls.
    calls = fake_ytdlp.calls
    report = pipeline.poll_channel(target, feed)
    assert not report.changed and fake_ytdlp.calls == calls

    # A new upload is picked up; an *older* item never is.
    fake_ytdlp.add_video(vid(9), "Fresh", base_time() + days(1))
    feed = make_feed([(vid(9), "Fresh", base_time() + days(1))] + entries)
    report = pipeline.poll_channel(target, feed)
    assert [r.video_id for r in report.added] == [vid(9)]
    assert fake_ytdlp.calls == calls + 1


def test_poll_all_runs_concurrently_and_refreshes_only_on_change(pipeline, fake_ytdlp, jellyfin, monkeypatch):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    fake_ytdlp.add_video(vid(1), "One", base_time())
    pipeline.db.add_target(Target(CHANNEL_ID, "channel", "Veritasium"))
    other = "UC" + "x" * 22
    pipeline.db.add_target(Target(other, "channel", "Broken"))
    feeds = {CHANNEL_ID: make_feed([(vid(1), "One", base_time())])}

    async def fake_fetch(targets, concurrency=16, timeout=15.0):
        from jelly_yt.feeds import FeedError
        return {tid: feeds.get(tid, FeedError("HTTP 404", 404)) for _, tid in targets}

    monkeypatch.setattr(ingest_mod, "fetch_feeds", fake_fetch)
    reports = pipeline.poll_all()
    assert sum(len(r.added) for r in reports) == 1
    assert any(r.error for r in reports)  # broken feed reported, not fatal
    assert len(jellyfin.refreshes) == 1

    pipeline.poll_all()
    assert len(jellyfin.refreshes) == 1  # no change -> no refresh


def test_upcoming_and_unavailable_videos_are_deferred(pipeline, fake_ytdlp):
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium")
    premiere = utcnow() + timedelta(days=2)
    fake_ytdlp.add_video(vid(1), "Premiere", premiere, live_status="is_upcoming",
                         release_timestamp=int(premiere.timestamp()))
    fake_ytdlp.errors[vid(2)] = VideoUnavailable("Private video")
    fake_ytdlp.errors[vid(3)] = AuthRequired("Sign in to confirm you're not a bot")

    assert pipeline.ingest_channel_video(vid(1)).status == "deferred"
    assert pipeline.ingest_channel_video(vid(2)).status == "unavailable"
    assert pipeline.ingest_channel_video(vid(3)).status == "deferred"
    blocked = pipeline.db.blocked([vid(1), vid(2), vid(3)], utcnow().isoformat())
    assert blocked == {vid(1), vid(2)}  # auth failures retry next run


def test_playlist_sync_add_reorder_prune(pipeline, fake_ytdlp, settings, jellyfin):
    pid = "PLtest123"
    for i in range(1, 5):
        fake_ytdlp.add_video(vid(i), f"Track {i}", base_time() - days(i))
    fake_ytdlp.playlists[pid] = PlaylistListing(
        pid, "Road Trip", "Songs", "Me", None,
        [PlaylistEntry(vid(i), f"Track {i}") for i in (1, 2, 3)],
    )
    target, report = pipeline.track_playlist(pid)
    assert target.display_name == "Road Trip"
    assert len(report.added) == 3
    folder = settings.media_root / "Road Trip [Playlist]"
    assert (folder / "tvshow.nfo").exists() and (folder / "poster.jpg").exists()
    assert sorted(p.name for p in (folder / "Season 01").glob("*.strm")) == [
        "S01E01 - Track 1.strm", "S01E02 - Track 2.strm", "S01E03 - Track 3.strm"]

    # Upstream: remove track 1, add track 4 at the front  -> [4, 2, 3]
    fake_ytdlp.playlists[pid].entries = [PlaylistEntry(vid(i), f"Track {i}") for i in (4, 2, 3)]
    report = pipeline.sync_playlist(pid, force=True)
    assert report.removed == [vid(1)]
    assert [r.video_id for r in report.added] == [vid(4)]
    assert report.renumbered == 0  # 2 and 3 kept their positions
    names = sorted(p.name for p in (folder / "Season 01").iterdir())
    assert names == [
        "S01E01 - Track 4-thumb.jpg", "S01E01 - Track 4.nfo", "S01E01 - Track 4.strm",
        "S01E02 - Track 2-thumb.jpg", "S01E02 - Track 2.nfo", "S01E02 - Track 2.strm",
        "S01E03 - Track 3-thumb.jpg", "S01E03 - Track 3.nfo", "S01E03 - Track 3.strm",
    ]

    # Swap order -> renumber in place (two-phase), nfo episode numbers updated, no probes.
    calls = fake_ytdlp.calls
    fake_ytdlp.playlists[pid].entries = [PlaylistEntry(vid(i), f"Track {i}") for i in (3, 4, 2)]
    report = pipeline.sync_playlist(pid, force=True)
    assert report.renumbered == 3 and not report.added
    assert fake_ytdlp.calls == calls + 1  # only the listing
    nfo = (folder / "Season 01" / "S01E01 - Track 3.nfo").read_text()
    assert "<episode>1</episode>" in nfo
    assert not list((folder / "Season 01").glob(".renaming-*"))


def test_playlist_feed_fingerprint_skips_listing(pipeline, fake_ytdlp):
    pid = "PLtest123"
    fake_ytdlp.add_video(vid(1), "Track 1", base_time())
    fake_ytdlp.playlists[pid] = PlaylistListing(pid, "Mix", entries=[PlaylistEntry(vid(1), "Track 1")])
    feed = make_feed([(vid(1), "Track 1", base_time())])
    pipeline.db.add_target(Target(pid, "playlist", "Mix"))
    pipeline.sync_playlist(pid, feed=feed)
    calls = fake_ytdlp.calls
    report = pipeline.sync_playlist(pid, feed=feed)
    assert report.skipped and fake_ytdlp.calls == calls
    # Private playlists (no feed) always do a full listing.
    report = pipeline.sync_playlist(pid, feed=None)
    assert not report.skipped and fake_ytdlp.calls == calls + 1


def test_track_channel_backfills_from_listing_when_feed_unavailable(pipeline, fake_ytdlp, settings, monkeypatch):
    async def no_feed(targets, concurrency=16, timeout=15.0):
        from jelly_yt.feeds import FeedError
        return {tid: FeedError("down") for _, tid in targets}

    monkeypatch.setattr(ingest_mod, "fetch_feeds", no_feed)
    ids = [vid(i) for i in range(5)]
    for i, v in enumerate(ids):
        fake_ytdlp.add_video(v, f"V{i}", base_time() - days(i))
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Veritasium", "desc", None, ids)

    target, report = pipeline.track_channel(f"https://www.youtube.com/channel/{CHANNEL_ID}")
    assert target.target_id == CHANNEL_ID
    assert len(report.added) == 3
    assert pipeline.db.get_target(CHANNEL_ID).display_name == "Veritasium"
    assert pipeline.db.get_state(CHANNEL_ID).backfill_cutoff is not None


def test_custom_folder_and_folder_collisions(pipeline, fake_ytdlp, settings):
    other = "UC" + "y" * 22
    fake_ytdlp.channels[CHANNEL_ID] = ChannelInfo(CHANNEL_ID, "Same Name")
    fake_ytdlp.channels[other] = ChannelInfo(other, "Same Name")
    fake_ytdlp.add_video(vid(1), "A", base_time(), channel="Same Name")
    fake_ytdlp.add_video(vid(2), "B", base_time(), channel_id=other, channel="Same Name")
    pipeline.ingest_channel_video(vid(1))
    pipeline.ingest_channel_video(vid(2))
    assert (settings.media_root / "Same Name").is_dir()
    assert (settings.media_root / f"Same Name [{other}]").is_dir()


def test_library_refuses_path_escape(pipeline):
    with pytest.raises(ValueError):
        pipeline.library.path("../outside")
