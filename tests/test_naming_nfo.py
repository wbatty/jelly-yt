from datetime import datetime, timezone

from jelly_yt import naming
from jelly_yt.nfo import EpisodeInfo, ShowInfo, episode_xml, read_unique_id, set_episode_number, tvshow_xml


def test_sanitize_strips_illegal_characters():
    assert naming.sanitize('What/is: "Gravity"?*') == "What is Gravity"
    assert naming.sanitize("  ...  ") == "Untitled"
    assert naming.sanitize("CON") == "_CON"
    assert naming.sanitize("trailing dots...") == "trailing dots"
    long = naming.sanitize("é" * 500)
    assert len(long.encode()) <= naming.MAX_COMPONENT_BYTES


def test_video_id_validation():
    assert naming.is_valid_video_id("dQw4w9WgXcQ")
    assert naming.is_valid_video_id("a-b_c-d_e-f")
    assert not naming.is_valid_video_id("short")
    assert not naming.is_valid_video_id("dQw4w9WgXcQ1")
    assert not naming.is_valid_video_id("../../etc/p")


def test_channel_and_playlist_stems():
    when = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert naming.channel_episode_stem(when, "Why Gravity is Weird") == "Season 2026/S2026E0105 - Why Gravity is Weird"
    assert naming.playlist_episode_stem(3, "Intro", 12) == "Season 01/S01E03 - Intro"
    assert naming.playlist_episode_stem(7, "Intro", 150) == "Season 01/S01E007 - Intro"
    assert naming.playlist_folder("My: List") == "My List [Playlist]"


def test_parse_datetime_variants():
    assert naming.parse_datetime("20261015").date().isoformat() == "2026-10-15"
    assert naming.parse_datetime("2026-10-15T12:00:00+02:00").hour == 10
    assert naming.parse_datetime(0).year == 1970
    assert naming.parse_datetime("garbage") is None


def test_tvshow_nfo_matches_spec():
    xml = tvshow_xml(ShowInfo("Veritasium", "UCHnyfMqiRRG1u-2MsSQLbXA", "An element of truth", "Veritasium",
                              ["Education"]))
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<tvshow>')
    for fragment in ("<title>Veritasium</title>", "<showtitle>Veritasium</showtitle>", "<genre>Education</genre>",
                     '<uniqueid type="youtube" default="true">UCHnyfMqiRRG1u-2MsSQLbXA</uniqueid>'):
        assert fragment in xml


def test_episode_nfo_matches_spec(tmp_path):
    xml = episode_xml(EpisodeInfo(
        title="Why Gravity is Weird & <Strange>", show_title="Veritasium", season=2026, episode=1015,
        video_id="dQw4w9WgXcQ", plot="Plot\x07 text", aired=datetime(2026, 10, 15, tzinfo=timezone.utc),
        runtime_minutes=18, studio="Veritasium",
    ))
    for fragment in ("<episodedetails>", "<title>Why Gravity is Weird &amp; &lt;Strange&gt;</title>",
                     "<season>2026</season>", "<episode>1015</episode>", "<aired>2026-10-15</aired>",
                     "<premiered>2026-10-15</premiered>", "<runtime>18</runtime>", "<plot>Plot text</plot>",
                     '<uniqueid type="youtube" default="true">dQw4w9WgXcQ</uniqueid>'):
        assert fragment in xml
    path = tmp_path / "ep.nfo"
    path.write_text(xml)
    set_episode_number(path, 7)
    assert "<episode>7</episode>" in path.read_text()
    assert read_unique_id(path) == "dQw4w9WgXcQ"
