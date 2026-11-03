from jelly_yt.urls import Link, classify, extract_links

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
