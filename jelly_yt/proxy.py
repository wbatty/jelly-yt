"""Component A – authenticated on-demand stream proxy.

``GET /watch?v=<VIDEO_ID>`` resolves a short-lived googlevideo.com URL with
yt-dlp (using the mounted cookies) and either answers ``302 Found`` to it
(``PROXY_MODE=redirect``, default) or relays the bytes with Range support
(``PROXY_MODE=stream``).

A single 302 can only point at *one* URL, so the default format selection
prefers muxed (audio+video) renditions: HLS first (up to 1080p on most
videos), then progressive MP4. Split DASH selections such as
``bestvideo+bestaudio`` cannot be redirected to and fall back to the best
muxed format.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import Settings
from .naming import is_valid_video_id
from .ytdlp import AuthRequired, VideoUnavailable, YtDlpClient, YtDlpError

log = logging.getLogger("jelly_yt.proxy")

DEFAULT_FORMATS = {
    "redirect": (
        "best[protocol^=m3u8][vcodec!=none][acodec!=none]"
        "/best[ext=mp4][vcodec!=none][acodec!=none]"
        "/best[vcodec!=none][acodec!=none]/best"
    ),
    "stream": (
        "best[ext=mp4][protocol^=http][vcodec!=none][acodec!=none]"
        "/best[protocol^=http][vcodec!=none][acodec!=none]/best[protocol^=http]"
    ),
}
CACHE_CAP_SECONDS = 4 * 3600
EXPIRY_MARGIN_SECONDS = 600
PASSTHROUGH_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


@dataclass
class Resolved:
    video_id: str
    url: str
    protocol: str = "https"
    headers: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0


def _muxed(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") not in (None, "none") and bool(fmt.get("url"))


def pick_stream(video_id: str, info: dict, allow_hls: bool = True) -> Resolved:
    """Choose a single playable URL from yt-dlp's processed info dict."""
    chosen: dict | None = None
    if info.get("url") and not info.get("requested_formats"):
        chosen = info
    else:
        candidates = [
            f for f in info.get("formats") or []
            if _muxed(f) and (allow_hls or not str(f.get("protocol", "")).startswith("m3u8"))
        ]
        if candidates:
            log.warning("%s: split A/V selection cannot be redirected; using muxed format %s",
                        video_id, candidates[-1].get("format_id"))
            chosen = candidates[-1]  # yt-dlp sorts formats worst -> best
    if not chosen:
        raise VideoUnavailable(f"No playable muxed stream for {video_id}")

    url = chosen["url"]
    now = time.time()
    expires = now + CACHE_CAP_SECONDS
    expire_param = parse_qs(urlparse(url).query).get("expire") or []
    if not expire_param and "/expire/" in url:  # HLS manifest URLs carry path-style params
        segs = urlparse(url).path.split("/")
        expire_param = [segs[segs.index("expire") + 1]] if segs.index("expire") + 1 < len(segs) else []
    if expire_param and expire_param[0].isdigit():
        expires = min(expires, float(expire_param[0]) - EXPIRY_MARGIN_SECONDS)
    return Resolved(
        video_id=video_id,
        url=url,
        protocol=str(chosen.get("protocol") or "https"),
        headers={k: v for k, v in (chosen.get("http_headers") or info.get("http_headers") or {}).items()},
        expires_at=max(expires, now + 60),
    )


class Resolver:
    """yt-dlp resolution with a per-video single-flight lock and expiry-aware cache.

    Jellyfin typically probes then plays (2+ requests within seconds); the cache
    makes every request after the first one effectively instant.
    """

    def __init__(self, ytdlp: YtDlpClient, format_spec: str, allow_hls: bool = True):
        self.ytdlp = ytdlp
        self.format_spec = format_spec
        self.allow_hls = allow_hls
        self._cache: dict[str, Resolved] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def invalidate(self, video_id: str) -> None:
        self._cache.pop(video_id, None)

    def _evict_expired(self) -> None:
        now = time.time()
        for vid in [v for v, r in self._cache.items() if r.expires_at <= now]:
            self._cache.pop(vid, None)
            self._locks.pop(vid, None)

    async def resolve(self, video_id: str) -> tuple[Resolved, bool]:
        cached = self._cache.get(video_id)
        if cached and cached.expires_at > time.time():
            return cached, True
        lock = self._locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(video_id)
            if cached and cached.expires_at > time.time():
                return cached, True
            info = await run_in_threadpool(self.ytdlp.resolve_stream, video_id, self.format_spec)
            resolved = pick_stream(video_id, info, allow_hls=self.allow_hls)
            self._evict_expired()
            self._cache[video_id] = resolved
            return resolved, False


def create_app(settings: Settings | None = None, ytdlp: YtDlpClient | None = None,
               upstream: httpx.AsyncClient | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    ytdlp = ytdlp or YtDlpClient(settings.cookies_path)
    mode = settings.proxy_mode
    resolver = Resolver(ytdlp, settings.proxy_format or DEFAULT_FORMATS[mode], allow_hls=(mode == "redirect"))
    client_holder: dict[str, httpx.AsyncClient] = {}

    def http() -> httpx.AsyncClient:
        if "c" not in client_holder:
            client_holder["c"] = upstream or httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True
            )
        return client_holder["c"]

    async def watch(request: Request) -> Response:
        started = time.perf_counter()
        video_id = request.query_params.get("v", "")
        if not is_valid_video_id(video_id):
            log.info("watch 404 invalid id %r", video_id[:32])
            return PlainTextResponse("Invalid video ID", status_code=404)
        try:
            resolved, cached = await resolver.resolve(video_id)
        except VideoUnavailable as exc:
            log.warning("watch 404 %s: %s", video_id, exc)
            return PlainTextResponse("Video unavailable", status_code=404)
        except AuthRequired as exc:
            log.error("watch 403 %s (cookies expired or rate limited): %s", video_id, exc)
            return PlainTextResponse("Upstream authentication failed or rate limited", status_code=403)
        except YtDlpError as exc:
            log.error("watch 502 %s: %s", video_id, exc)
            return PlainTextResponse("Could not resolve stream", status_code=502)

        elapsed_ms = (time.perf_counter() - started) * 1000
        if mode == "redirect":
            log.info("watch 302 %s %s cached=%s resolve_ms=%.0f", video_id, resolved.protocol, cached, elapsed_ms)
            return RedirectResponse(resolved.url, status_code=302, headers={"Cache-Control": "no-store"})
        return await _relay(request, resolved, cached, elapsed_ms)

    async def _relay(request: Request, resolved: Resolved, cached: bool, elapsed_ms: float) -> Response:
        headers = dict(resolved.headers)
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]
        method = "HEAD" if request.method == "HEAD" else "GET"
        for attempt in range(2):
            req = http().build_request(method, resolved.url, headers=headers)
            upstream_resp = await http().send(req, stream=True)
            if upstream_resp.status_code in (403, 410) and attempt == 0:
                # CDN URL expired early: re-resolve once.
                await upstream_resp.aclose()
                resolver.invalidate(resolved.video_id)
                try:
                    resolved, cached = await resolver.resolve(resolved.video_id)
                except YtDlpError as exc:
                    log.error("re-resolve failed for %s: %s", resolved.video_id, exc)
                    return PlainTextResponse("Could not resolve stream", status_code=502)
                continue
            break
        out_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() in PASSTHROUGH_HEADERS}
        out_headers.setdefault("accept-ranges", "bytes")
        log.info("watch %s %s (stream) cached=%s resolve_ms=%.0f", upstream_resp.status_code,
                 resolved.video_id, cached, elapsed_ms)
        if method == "HEAD":
            await upstream_resp.aclose()
            return Response(status_code=upstream_resp.status_code, headers=out_headers)
        return StreamingResponse(
            upstream_resp.aiter_raw(),
            status_code=upstream_resp.status_code,
            headers=out_headers,
            background=BackgroundTask(upstream_resp.aclose),
        )

    async def health(request: Request) -> Response:
        cookies = settings.usable_cookies()
        return JSONResponse({
            "status": "ok",
            "mode": mode,
            "cookies": bool(cookies),
            "cached_streams": len(resolver._cache),
        })

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        if "c" in client_holder and upstream is None:
            await client_holder["c"].aclose()

    app = Starlette(
        routes=[
            Route("/watch", watch, methods=["GET", "HEAD"]),
            Route("/healthz", health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.state.resolver = resolver
    return app


def main() -> None:
    import uvicorn

    from .logging_setup import configure_logging

    configure_logging()
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.proxy_bind, port=settings.proxy_port, log_level="warning",
                proxy_headers=True)


if __name__ == "__main__":
    main()
