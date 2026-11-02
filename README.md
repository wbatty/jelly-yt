# jelly-yt — YouTube → Jellyfin pipeline

Ad-free YouTube channel subscriptions and playlists inside Jellyfin, using no
video storage on disk:

* **`.strm` stubs** point at a local stream proxy (`http://PROXY_HOST:8080/watch?v=<id>`).
* **`.nfo` sidecars + thumbnails** give Jellyfin full metadata with every
  internet scraper turned off.
* **Stream proxy** resolves a fresh googlevideo.com URL with `yt-dlp` and your
  `cookies.txt` when you press play.
* **Feed worker** polls YouTube Atom feeds and only calls `yt-dlp` for videos
  it hasn't seen yet.
* **Telegram bot**: paste a video, playlist or channel link to add it.

```text
[ Telegram ] ──► bot.py ───────────┐
[ Atom feeds ] ─► ingest_worker.py ┼──► .strm / .nfo / -thumb.jpg in MEDIA_ROOT
[ seen.db ] ◄──────────────────────┘                │
[ Jellyfin clients ] ─► Jellyfin ◄──────────────────┘
                           │ reads .strm URL
                           ▼
                     proxy.py :8080 ──(yt-dlp + cookies)──► YouTube CDN
```

## Layout produced

```text
/srv/media/youtube/
├── Veritasium/                               # channel: year = season
│   ├── tvshow.nfo, poster.jpg
│   └── Season 2026/
│       ├── S2026E1015 - Why Gravity is Weird.strm
│       ├── S2026E1015 - Why Gravity is Weird.nfo
│       └── S2026E1015 - Why Gravity is Weird-thumb.jpg
└── Road Trip [Playlist]/                     # playlist: position = episode
    ├── tvshow.nfo, poster.jpg
    └── Season 01/S01E01 - <Title>.strm|.nfo|-thumb.jpg
```

## Quick start (Docker Compose)

1. `cp .env.example .env` and fill it in. `PROXY_HOST` (or `PROXY_PUBLIC_URL`)
   is written into every `.strm` file, so Jellyfin must be able to reach it.
2. Export your YouTube cookies in Netscape format (for example with the
   "Get cookies.txt LOCALLY" browser extension, from a private window you then
   close) to `$MEDIA_ROOT_HOST/cookies.txt`. The services work on a private copy,
   so the file can be mounted read-only and yt-dlp never overwrites it.
3. `docker compose up -d --build`
4. In Jellyfin, add a library:
   * Content type: **Shows**, folder: the media root.
   * Untick **every** metadata downloader and image fetcher (TheTVDB, TMDb,
     OMDb, …). Keep the **Nfo** metadata reader on.
   * Optional: turn on *real-time monitoring*.
5. Create an API key (Dashboard → API Keys) and put it in `JELLYFIN_API_KEY`.
   Set `JELLYFIN_MEDIA_ROOT` to the library path as Jellyfin sees it. With that
   set, refreshes go to `/Library/Media/Updated` and only touch the changed
   folders. Without it, a full `/Library/Refresh` runs.

Add subscriptions from the bot or the CLI:

```bash
docker compose run --rm worker python -m jelly_yt.ingest_worker add @veritasium
```

```bash
docker compose run --rm worker python -m jelly_yt.ingest_worker add "https://www.youtube.com/playlist?list=WL" --folder "Watch Later"
```

```bash
docker compose run --rm worker python -m jelly_yt.ingest_worker list
```

The worker loops every `POLL_INTERVAL_MINUTES`. To use cron instead, run
`python -m jelly_yt.ingest_worker run --once`.

## Components

| Module | Role |
| --- | --- |
| `jelly_yt/proxy.py` | `GET\|HEAD /watch?v=<id>` → `302` to the CDN URL (or a chunked relay with Range support when `PROXY_MODE=stream`). Returns 404 for an invalid or unavailable ID, 403 for expired cookies, a bot check or a 429, and 502 for other failures. Also serves `GET /healthz`. |
| `jelly_yt/ingest_worker.py` | Polling loop and CLI (`run [--once]`, `add`, `list`, `remove`). |
| `jelly_yt/bot.py` | Telegram bot (python-telegram-bot v20+). Only `ALLOWED_TELEGRAM_USER_ID` is answered. Everyone else is ignored without a reply. Commands: `/list`, `/remove <id>`, `/sync`. |
| `jelly_yt/ingest.py` | Shared pipeline: channel polling and backfill, single-video ingest, playlist mirroring (add, renumber, prune). |
| `jelly_yt/feeds.py` | Concurrent Atom feed fetch and parse (stdlib XML, no yt-dlp). |
| `jelly_yt/ytdlp.py` | In-process yt-dlp wrapper with error classification and a private cookie copy. |

### Telegram routing

| Message contains | Action |
| --- | --- |
| `list=PL…`, `list=WL`, `list=LL`, … | Track the playlist and mirror all of its items into `<Title> [Playlist]/Season 01/`. |
| `@handle`, `/channel/UC…`, `/c/…`, `/user/…` | Resolve the channel ID, track it and backfill the newest `INITIAL_BACKFILL_LIMIT` uploads. |
| `watch?v=`, `youtu.be/`, `/shorts/`, `/live/` | Ingest one video into `<Channel>/Season <YYYY>/`. |

After any change, the bot asks Jellyfin to rescan and reports the outcome.

## Behaviour notes and design decisions

* **Single-URL redirects.** A `302` can point at only one URL. YouTube's
  `bestvideo+bestaudio` formats are separate video-only and audio-only streams,
  so a client sent to just one of them gets either no audio or no picture. The
  proxy therefore picks a *muxed* (audio + video) format: first HLS (usually up
  to 1080p), then progressive MP4, then any muxed format. If you set
  `PROXY_FORMAT` to something that resolves to separate streams, the proxy logs
  a warning and falls back to the best muxed format.
* **Resolution cache.** Jellyfin usually requests a URL twice (probe, then
  play). Resolved URLs are cached per video until shortly before their
  `expire=` time, and concurrent requests for the same video share one yt-dlp
  call. Only the first request pays the yt-dlp cost. Every response is logged
  with its status and `resolve_ms`, which is how you check AC-01.
* **Video ID validation** accepts the real YouTube alphabet
  (`[A-Za-z0-9_-]{11}`). The spec says "alphanumeric", but many real IDs
  contain `-` or `_`.
* **Idempotency (AC-03).** Feeds are fetched concurrently (`FEED_CONCURRENCY`).
  Their IDs are checked against `processed_videos` before anything else, so a
  run with no new uploads makes zero yt-dlp calls. The worker logs the number
  of yt-dlp calls and the run time for each pass.
* **Backfill cutoff.** The first sync of a channel ingests the newest
  `INITIAL_BACKFILL_LIMIT` uploads and stores a cutoff date. Older entries that
  later appear in the feed are ignored.
* **Playlists.** The Atom feed shows at most 15 items and doesn't exist for
  private lists, so it is used only as a cheap change check. When the feed's
  fingerprint changes, or every `PLAYLIST_FULL_RESYNC_HOURS`, the worker runs a
  single `yt-dlp --flat-playlist` request:
  * Items removed upstream have their `.strm`, `.nfo` and `-thumb.jpg` deleted
    (AC-06).
  * Items that moved are renamed in place and their `<episode>` number is
    rewritten. Two-phase renames prevent name clashes, and no re-probe is
    needed.
  * Only new items are probed.

  `WL` and `LL` have no feed, so they get a full listing on every poll. They
  need valid cookies (AC-04).
* **Deferred videos.** Upcoming premieres and live streams are retried after
  their start time. Unavailable videos back off for 24 h per attempt and are
  dropped after 5 attempts. Auth or rate-limit failures are retried on the next
  run and don't count as attempts.
* **Same-day uploads with the same title** on one channel get ` [<videoId>]`
  added to the filename so files don't clash. Same-day uploads with different
  titles share the episode number `S<YYYY>E<MMDD>` (spec format), and Jellyfin
  lists both.
* **Extra tables.** Alongside the spec's `processed_videos` and
  `tracked_targets`, `seen.db` has:
  * `target_state`: backfill cutoff, feed fingerprint, sync timestamps.
  * `playlist_items`: video, position and file per playlist.
  * `deferred_videos`: retry bookkeeping.
  * `show_folders`: keeps a show's folder name fixed when a channel is renamed,
    and gives two channels whose names sanitize to the same string separate
    folders.
* **Jellyfin refresh** runs only when something actually changed.
* **JavaScript runtime.** Current yt-dlp needs one (Deno) to solve YouTube's
  player challenges. The Docker image includes it. For a bare-metal install,
  install `deno` and `yt-dlp[default]`, and keep yt-dlp up to date, because
  YouTube breaks old versions often.

### Remote clients

Jellyfin hands the `.strm` URL to its own ffmpeg, or to clients that direct-play
remote sources. Phones or TVs outside your LAN may not be able to reach
`PROXY_HOST`. In that case either let Jellyfin remux/transcode (the default for
most clients), or publish the proxy at an address they can reach and set
`PROXY_PUBLIC_URL`.

Googlevideo URLs are tied to the IP address that resolved them. The proxy
works best when it shares an egress IP with whatever fetches the stream (that
is, Jellyfin). Otherwise use `PROXY_MODE=stream`.

## Development

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

The tests run fully offline, using a fake yt-dlp client and mocked HTTP.
