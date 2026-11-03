# jelly-yt: Telegram → Jellyfin bot for ytdlp2STRM

A Telegram bot that adds YouTube content to Jellyfin from your phone. It works
alongside [ytdlp2STRM](https://github.com/fe80Grau/ytdlp2STRM), which handles
playback and channel subscriptions. The bot adds two things ytdlp2STRM doesn't
do well:

* **Single videos.** ytdlp2STRM can only follow whole channels.
* **Whole playlists, kept in sync.** ytdlp2STRM caps playlists at its global
  `videos_limit` (10 by default) and never removes anything.

That makes it suited to a curated library, such as a kids' library with only
the playlists and videos you picked.

| You send | What happens |
| --- | --- |
| A video (`watch?v=`, `youtu.be/`, `/shorts/`, `/live/`) | It is added to `MEDIA_ROOT/<Channel>/Season <YYYY>/`. |
| A playlist (`…list=PL…`) | **Every** item is added to `MEDIA_ROOT/<Title> [Playlist]/Season 01/S01E<NN>`, and the playlist is resynced every `PLAYLIST_SYNC_INTERVAL_MINUTES`: new items are added, removed items are deleted, and reordered items are renumbered. |
| A channel (`@handle`, `/channel/UC…`, `/c/…`, `/user/…`) | It is appended to ytdlp2STRM's `channel_list.json`. **ytdlp2STRM reads that file only at startup, so restart it afterwards.** |

The bot writes `.strm` files that point at ytdlp2STRM's playback endpoint
(`<YTDLP2STRM_URL>/youtube/direct/<id>`). Each one also gets an `.nfo` file
(title, plot, air date, runtime) and a thumbnail, so Jellyfin needs no
internet scrapers. After a change, the bot can ask Jellyfin to rescan.

Commands: `/list`, `/remove <playlist link or id>`, `/sync`, `/help`.
`/remove` stops syncing a playlist but keeps its files.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather). Get your numeric
   user ID from, for example, @userinfobot.
2. `cp .env.example .env` and fill it in. `YTDLP2STRM_URL` must match what
   ytdlp2STRM writes into its own `.strm` files. Open one of them and copy
   everything before `/youtube/…`.
3. Create the channel list file *before* the first start. If it doesn't exist,
   Docker creates a directory in its place:
   ```bash
   mkdir -p ytdlp2strm && [ -f ytdlp2strm/channel_list.json ] || echo '[]' > ytdlp2strm/channel_list.json
   ```
   If you already run ytdlp2STRM, point `CHANNEL_LIST_HOST` at its existing
   `channel_list.json` instead. The bot edits that file in place, so it stays
   compatible with a single-file bind mount.
4. Start the bot:
   ```bash
   docker compose up -d --build bot
   ```
   The compose file also has an example `ytdlp2strm` service. Run
   `docker compose up -d` to start both.
5. In Jellyfin, add `MEDIA_ROOT` (for example `/media/YoutubeCurated`) as its
   own **Shows** library:
   * Untick every metadata downloader and image fetcher, and keep the **Nfo**
     reader.
   * For kids, give their Jellyfin user access to only this library.

### Optional

* **`JELLYFIN_URL` and `JELLYFIN_API_KEY`:** the bot asks Jellyfin to rescan
  as soon as something is added. If you also set `JELLYFIN_MEDIA_ROOT`, the
  scan covers only the changed folders.
* **`COOKIES_PATH`:** a Netscape-format `cookies.txt`, needed for private
  playlists and age-restricted videos. Playback still goes through
  ytdlp2STRM, so set up its cookie options too.
* **`ALLOWED_TELEGRAM_USER_IDS`:** accepts several IDs, for example both
  parents.

## Behaviour notes

* **Videos that can't be added yet** are retried on each sync:
  * Premieres and live streams are retried after they start.
  * If YouTube rejects the cookies or rate-limits the bot, the video is retried
    on the next sync.
  * Unavailable videos (private, removed) back off for a day per attempt and
    are dropped after 5 attempts.
* **Playlist renumbering** renames the existing files in place and rewrites
  their `<episode>` number. Nothing is fetched from YouTube again; each sync
  costs one `yt-dlp --flat-playlist` request per playlist.
* **Same-day uploads with the same title** from one channel get `[<videoId>]`
  appended to the filename so they don't overwrite each other.
* **State** lives in SQLite at `MEDIA_ROOT/.jelly-yt/state.db`. Jellyfin
  ignores hidden folders.

## Development

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

The tests run fully offline, using a fake yt-dlp client and mocked HTTP.
