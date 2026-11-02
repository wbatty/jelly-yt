from jelly_yt.feeds import parse_feed
from jelly_yt.urls import Link, classify, extract_links

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <yt:channelId>UCHnyfMqiRRG1u-2MsSQLbXA</yt:channelId>
 <title>Veritasium</title>
 <author><name>Veritasium</name></author>
 <entry>
  <id>yt:video:dQw4w9WgXcQ</id>
  <yt:videoId>dQw4w9WgXcQ</yt:videoId>
  <yt:channelId>UCHnyfMqiRRG1u-2MsSQLbXA</yt:channelId>
  <title>Why Gravity is Weird</title>
  <author><name>Veritasium</name></author>
  <published>2026-10-15T14:00:00+00:00</published>
  <media:group><media:description>Exploring gravity</media:description></media:group>
 </entry>
 <entry>
  <yt:videoId>aaaaaaaaaaa</yt:videoId>
  <title>Second</title>
  <published>2026-10-01T10:00:00+00:00</published>
 </entry>
</feed>"""


def test_parse_feed():
    feed = parse_feed(FEED)
    assert feed.title == "Veritasium"
    assert feed.channel_id == "UCHnyfMqiRRG1u-2MsSQLbXA"
    assert [e.video_id for e in feed.entries] == ["dQw4w9WgXcQ", "aaaaaaaaaaa"]
    first = feed.entries[0]
    assert first.title == "Why Gravity is Weird"
    assert first.description == "Exploring gravity"
    assert first.published.isoformat() == "2026-10-15T14:00:00+00:00"
    assert feed.entries[1].channel_id == "UCHnyfMqiRRG1u-2MsSQLbXA"  # inherited from feed
    assert len(feed.fingerprint()) == 64


def test_classify_links():
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == Link("video", "dQw4w9WgXcQ")
    assert classify("https://youtu.be/dQw4w9WgXcQ?t=10") == Link("video", "dQw4w9WgXcQ")
    assert classify("youtube.com/shorts/dQw4w9WgXcQ") == Link("video", "dQw4w9WgXcQ")
    assert classify("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc123") == Link("playlist", "PLabc123")
    assert classify("https://www.youtube.com/playlist?list=WL") == Link("playlist", "WL")
    # Mixes are not playlists
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ") == Link("video", "dQw4w9WgXcQ")
    assert classify("https://www.youtube.com/@veritasium/videos") == Link("channel", "https://www.youtube.com/@veritasium")
    assert classify("@veritasium") == Link("channel", "https://www.youtube.com/@veritasium")
    assert classify("https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA") == Link(
        "channel", "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA")
    assert classify("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert classify("hello") is None


def test_extract_links_from_message():
    msg = "check https://youtu.be/dQw4w9WgXcQ and @veritasium, also https://youtu.be/dQw4w9WgXcQ"
    assert extract_links(msg) == [
        Link("video", "dQw4w9WgXcQ"),
        Link("channel", "https://www.youtube.com/@veritasium"),
    ]
    assert extract_links("email me at a@example.com") == []
