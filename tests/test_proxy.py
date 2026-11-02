import httpx
import pytest
from starlette.testclient import TestClient

from jelly_yt.proxy import create_app, pick_stream
from jelly_yt.ytdlp import AuthRequired

from .conftest import base_time

HLS = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/9999999999/id/x/file/index.m3u8"
MP4 = "https://rr1.googlevideo.com/videoplayback?expire=9999999999&itag=18"


def make(fake, settings, **overrides):
    from dataclasses import replace
    return TestClient(create_app(replace(settings, **overrides), ytdlp=fake), follow_redirects=False)


def test_invalid_id_is_404(fake_ytdlp, settings):
    client = make(fake_ytdlp, settings)
    assert client.get("/watch?v=bad").status_code == 404
    assert client.get("/watch").status_code == 404
    assert fake_ytdlp.calls == 0


def test_redirect_and_cache(fake_ytdlp, settings):
    fake_ytdlp.add_video("dQw4w9WgXcQ", "x", base_time(), url=HLS, protocol="m3u8_native")
    client = make(fake_ytdlp, settings)
    for method in ("head", "get"):
        resp = getattr(client, method)("/watch?v=dQw4w9WgXcQ")
        assert resp.status_code == 302
        assert resp.headers["location"] == HLS
    assert fake_ytdlp.calls == 1  # second request served from cache


def test_unavailable_404_and_auth_403(fake_ytdlp, settings):
    fake_ytdlp.errors["aaaaaaaaaaa"] = AuthRequired("Sign in to confirm you're not a bot")
    client = make(fake_ytdlp, settings)
    assert client.get("/watch?v=bbbbbbbbbbb").status_code == 404  # unknown -> unavailable
    assert client.get("/watch?v=aaaaaaaaaaa").status_code == 403


def test_split_selection_falls_back_to_muxed():
    info = {
        "requested_formats": [{"url": "v"}, {"url": "a"}],
        "formats": [
            {"format_id": "18", "url": MP4, "vcodec": "avc1", "acodec": "mp4a", "protocol": "https"},
            {"format_id": "137", "url": "videoonly", "vcodec": "avc1", "acodec": "none"},
        ],
    }
    resolved = pick_stream("dQw4w9WgXcQ", info)
    assert resolved.url == MP4
    assert resolved.expires_at < 9999999999


def test_stream_mode_relays_range(fake_ytdlp, settings):
    fake_ytdlp.add_video("dQw4w9WgXcQ", "x", base_time(), url=MP4, protocol="https",
                         http_headers={"User-Agent": "UA"})
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("range")
        seen["ua"] = request.headers.get("user-agent")
        async def body():
            yield b"01234"
            yield b"56789"

        return httpx.Response(206, content=body(),
                              headers={"content-type": "video/mp4", "content-range": "bytes 0-9/100"})

    from dataclasses import replace
    app = create_app(replace(settings, proxy_mode="stream"), ytdlp=fake_ytdlp,
                     upstream=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = TestClient(app)
    resp = client.get("/watch?v=dQw4w9WgXcQ", headers={"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.content == b"0123456789"
    assert resp.headers["content-range"] == "bytes 0-9/100"
    assert seen == {"range": "bytes=0-9", "ua": "UA"}


def test_health(fake_ytdlp, settings):
    assert make(fake_ytdlp, settings).get("/healthz").json()["status"] == "ok"


def test_error_classification():
    from jelly_yt.ytdlp import VideoUnavailable, YtDlpError, classify_error

    private = "ERROR: [youtube] x: Private video. Sign in if you've been granted access. Use --cookies"
    assert isinstance(classify_error(Exception(private)), VideoUnavailable)
    bot = "ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies-from-browser"
    assert isinstance(classify_error(Exception(bot)), AuthRequired)
    assert isinstance(classify_error(Exception("HTTP Error 429: Too Many Requests")), AuthRequired)
    assert isinstance(classify_error(Exception("Video unavailable")), VideoUnavailable)
    generic = classify_error(Exception("Unable to connect to proxy"))
    assert type(generic) is YtDlpError
