"""Core ingestion logic shared by the polling worker and the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import naming
from .config import Settings
from .db import Database, PlaylistItem, Target
from .feeds import Feed, FeedEntry, FeedError, fetch_feeds
from .jellyfin import JellyfinClient
from .library import Library
from .nfo import EpisodeInfo, ShowInfo, episode_xml, read_unique_id, set_episode_number, tvshow_xml
from .ytdlp import AuthRequired, ChannelInfo, VideoUnavailable, YtDlpClient, YtDlpError

log = logging.getLogger(__name__)

FEED_MAX_ENTRIES = 15  # YouTube Atom feeds expose at most the 15 newest items
MAX_ATTEMPTS = 5
# Playlists without a public Atom feed: always fully synced via yt-dlp (requires cookies).
PRIVATE_PLAYLISTS = {"WL", "LL"}
THUMB_URLS = ("https://i.ytimg.com/vi/{}/maxresdefault.jpg", "https://i.ytimg.com/vi/{}/hqdefault.jpg")


@dataclass
class IngestResult:
    video_id: str
    status: str  # 'added' | 'exists' | 'deferred' | 'unavailable' | 'error'
    title: str = ""
    published: datetime | None = None
    path: Path | None = None
    show: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"added", "exists"}


@dataclass
class SyncReport:
    target_id: str
    name: str = ""
    added: list[IngestResult] = field(default_factory=list)
    existing: int = 0
    removed: list[str] = field(default_factory=list)
    renumbered: int = 0
    deferred: list[IngestResult] = field(default_factory=list)
    skipped: bool = False
    error: str | None = None
    exception: Exception | None = field(default=None, repr=False)
    changed_paths: set[Path] = field(default_factory=set)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.renumbered or self.changed_paths)

    def record(self, result: IngestResult) -> None:
        if result.status == "added":
            self.added.append(result)
            if result.path:
                self.changed_paths.add(result.path.parent.parent)
        elif result.status == "exists":
            self.existing += 1
        else:
            self.deferred.append(result)

    def summary(self) -> str:
        if self.error:
            return f"{self.name or self.target_id}: error – {self.error}"
        if self.skipped:
            return f"{self.name or self.target_id}: unchanged"
        parts = [f"{len(self.added)} added"]
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.renumbered:
            parts.append(f"{self.renumbered} renumbered")
        if self.deferred:
            parts.append(f"{len(self.deferred)} deferred")
        return f"{self.name or self.target_id}: " + ", ".join(parts)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _published_from_info(info: dict[str, Any], fallback: datetime | None) -> datetime:
    for key in ("release_timestamp", "timestamp"):
        if info.get(key):
            return naming.parse_datetime(info[key])
    for key in ("release_date", "upload_date"):
        parsed = naming.parse_datetime(info.get(key))
        if parsed:
            return parsed
    return fallback or naming.utcnow()


def _runtime_minutes(info: dict[str, Any]) -> int | None:
    duration = info.get("duration")
    if not duration:
        return None
    return max(1, round(float(duration) / 60))


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        ytdlp: YtDlpClient,
        http: httpx.Client | None = None,
        jellyfin: JellyfinClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self.ytdlp = ytdlp
        self.http = http or httpx.Client(
            timeout=settings.http_timeout_seconds, follow_redirects=True, headers={"User-Agent": "jelly-yt/1.0"}
        )
        self.jellyfin = jellyfin or JellyfinClient(settings)
        self.library = Library(settings.media_root)

    # ------------------------------------------------------------------ helpers
    def strm_url(self, video_id: str) -> str:
        return f"{self.settings.proxy_base_url}/watch?v={video_id}"

    def _download_image(self, url: str | None) -> bytes | None:
        if not url:
            return None
        try:
            resp = self.http.get(url)
        except httpx.HTTPError as exc:
            log.debug("Image download failed %s: %s", url, exc)
            return None
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            return None
        return resp.content

    def _thumbnail(self, video_id: str, info: dict[str, Any] | None = None) -> bytes | None:
        for template in THUMB_URLS:
            data = self._download_image(template.format(video_id))
            if data:
                return data
        return self._download_image((info or {}).get("thumbnail"))

    def _folder_for(self, show_id: str, preferred: str) -> str:
        """Stable folder per show; disambiguates when two shows sanitize to the same name."""
        existing = self.db.get_show_folder(show_id)
        if existing:
            return existing
        folder = preferred
        owner = self.db.folder_owner(folder)
        if owner and owner != show_id:
            folder = naming.sanitize(f"{preferred} [{show_id}]")
        self.db.set_show_folder(show_id, folder)
        return folder

    def _defer(self, video_id: str, exc: Exception | str, *, title: str = "") -> IngestResult:
        now = naming.utcnow()
        if isinstance(exc, VideoUnavailable):  # likely permanent: back off a day per attempt
            self.db.defer(video_id, str(exc), _iso(now + timedelta(hours=24)), True, MAX_ATTEMPTS)
            return IngestResult(video_id, "unavailable", title=title, message=str(exc))
        if isinstance(exc, AuthRequired):
            # Cookies expired / rate limited: not the video's fault, retry next run without counting.
            self.db.defer(video_id, str(exc), _iso(now), False, MAX_ATTEMPTS)
            return IngestResult(video_id, "deferred", title=title, message=f"auth/rate-limit: {exc}")
        self.db.defer(video_id, str(exc), _iso(now + timedelta(hours=1)), True, MAX_ATTEMPTS)
        return IngestResult(video_id, "error", title=title, message=str(exc))

    def _probe(self, video_id: str, title: str = "") -> tuple[dict[str, Any] | None, IngestResult | None]:
        try:
            info = self.ytdlp.probe_video(video_id)
        except YtDlpError as exc:
            log.warning("Probe failed for %s: %s", video_id, exc)
            return None, self._defer(video_id, exc, title=title)
        live = info.get("live_status")
        if live in {"is_upcoming", "is_live", "post_live"}:
            release = naming.parse_datetime(info.get("release_timestamp"))
            now = naming.utcnow()
            retry = release if release and release > now else now + timedelta(hours=1)
            self.db.defer(video_id, live, _iso(retry), False, MAX_ATTEMPTS)
            return None, IngestResult(video_id, "deferred", title=info.get("title", title), message=live)
        return info, None

    def _unblocked(self, video_ids: list[str]) -> list[str]:
        blocked = self.db.blocked(video_ids, _iso(naming.utcnow()))
        return [v for v in video_ids if v not in blocked]

    # ------------------------------------------------------------------ channels
    def ensure_channel_show(
        self,
        channel_id: str,
        name_hint: str,
        channel_info: ChannelInfo | None = None,
        genres: list[str] | None = None,
    ) -> tuple[str, str]:
        """Create ``<Channel>/tvshow.nfo`` + ``poster.jpg`` if missing. Returns (folder, show title)."""
        target = self.db.get_target(channel_id)
        show_title = (target.display_name if target else None) or name_hint or channel_id
        folder = self._folder_for(
            channel_id, naming.channel_folder(show_title, target.custom_folder if target else None)
        )
        if not self.library.has_show(folder):
            info = channel_info
            if info is None:
                try:
                    info = self.ytdlp.channel_info_by_id(channel_id)
                except YtDlpError as exc:
                    log.warning("Channel metadata lookup failed for %s: %s", channel_id, exc)
                    info = ChannelInfo(channel_id=channel_id, name=show_title)
            xml = tvshow_xml(
                ShowInfo(
                    title=show_title,
                    unique_id=channel_id,
                    plot=info.description,
                    studio=info.name or show_title,
                    genres=genres or ["YouTube"],
                )
            )
            self.library.write_show(folder, xml, self._download_image(info.avatar_url))
            log.info("Created show folder %s", folder)
        return folder, show_title

    def ingest_channel_video(
        self,
        video_id: str,
        feed_entry: FeedEntry | None = None,
        channel_info: ChannelInfo | None = None,
    ) -> IngestResult:
        """Ingest one video into ``<Channel>/Season <YYYY>/S<YYYY>E<MMDD> - <Title>``."""
        if self.db.is_processed(video_id):
            return IngestResult(video_id, "exists", title=feed_entry.title if feed_entry else "")
        info, failure = self._probe(video_id, feed_entry.title if feed_entry else "")
        if failure:
            return failure
        assert info is not None

        title = info.get("title") or (feed_entry.title if feed_entry else video_id)
        published = _published_from_info(info, feed_entry.published if feed_entry else None)
        channel_id = info.get("channel_id") or (feed_entry.channel_id if feed_entry else "") or "unknown"
        channel_name = (
            info.get("channel") or info.get("uploader") or (feed_entry.author if feed_entry else "") or channel_id
        )
        genres = [g for g in (info.get("categories") or []) if g]

        folder, show_title = self.ensure_channel_show(channel_id, channel_name, channel_info, genres=genres)
        rel_stem = f"{folder}/{naming.channel_episode_stem(published, title)}"
        # Same-day uploads with identical titles would collide; disambiguate with the ID.
        existing_nfo = self.library.path(rel_stem + ".nfo")
        if existing_nfo.exists() and read_unique_id(existing_nfo) not in (None, video_id):
            rel_stem = f"{rel_stem} [{video_id}]"

        nfo = episode_xml(
            EpisodeInfo(
                title=title,
                show_title=show_title,
                season=published.year,
                episode=int(f"{published.month:02d}{published.day:02d}"),
                video_id=video_id,
                plot=info.get("description") or (feed_entry.description if feed_entry else ""),
                aired=published,
                runtime_minutes=_runtime_minutes(info),
                studio=channel_name,
                genres=genres,
            )
        )
        strm = self.library.write_episode(rel_stem, self.strm_url(video_id), nfo, self._thumbnail(video_id, info))
        self.db.mark_processed(video_id, channel_id, title, _iso(published))
        self.db.clear_deferred(video_id)
        log.info("Added %s -> %s", video_id, rel_stem)
        return IngestResult(video_id, "added", title=title, published=published, path=strm, show=show_title)

    def _backfill(
        self,
        target: Target,
        feed: Feed | None,
        recent_ids: list[str] | None = None,
        channel_info: ChannelInfo | None = None,
    ) -> SyncReport:
        """First sync of a channel: ingest the newest INITIAL_BACKFILL_LIMIT uploads and set a cutoff
        so older uploads that surface in the feed are ignored on subsequent polls."""
        report = SyncReport(target.target_id, name=target.display_name)
        limit = max(0, self.settings.initial_backfill_limit)
        entries = sorted(
            (feed.entries if feed else []),
            key=lambda e: e.published or naming.utcnow(),
            reverse=True,
        )
        by_id = {e.video_id: e for e in entries}

        if feed is not None and (limit <= len(entries) or len(entries) < FEED_MAX_ENTRIES):
            selected = [e.video_id for e in entries[:limit]]
        else:
            if recent_ids is None:
                try:
                    recent_ids = self.ytdlp.channel_info_by_id(target.target_id, recent=limit).recent_video_ids
                except YtDlpError as exc:
                    report.error = str(exc)
                    return report
            selected = list(recent_ids[:limit])

        published_dates = [by_id[v].published for v in selected if v in by_id and by_id[v].published]
        todo = self._unblocked(self.db.unseen(selected))
        for vid in reversed(selected):  # oldest first so Jellyfin "Next Up" ordering is natural
            if vid not in todo:
                report.existing += 1 if self.db.is_processed(vid) else 0
                continue
            result = self.ingest_channel_video(vid, by_id.get(vid), channel_info)
            report.record(result)
            if result.published:
                published_dates.append(result.published)

        cutoff = min(published_dates) if published_dates else naming.utcnow()
        self.db.update_state(
            target.target_id, backfill_cutoff=_iso(cutoff), last_polled_at=_iso(naming.utcnow())
        )
        return report

    def poll_channel(self, target: Target, feed: Feed) -> SyncReport:
        state = self.db.get_state(target.target_id)
        cutoff = naming.parse_datetime(state.backfill_cutoff)
        if cutoff is None:
            return self._backfill(target, feed)

        report = SyncReport(target.target_id, name=target.display_name)
        candidates = [e for e in feed.entries if e.published is None or e.published >= cutoff]
        todo = set(self._unblocked(self.db.unseen([e.video_id for e in candidates])))
        report.existing = len(candidates) - len(todo)
        for entry in sorted(
            (e for e in candidates if e.video_id in todo), key=lambda e: e.published or naming.utcnow()
        ):
            report.record(self.ingest_channel_video(entry.video_id, entry))
        self.db.update_state(target.target_id, last_polled_at=_iso(naming.utcnow()))
        return report

    def track_channel(self, channel_url: str, custom_folder: str | None = None) -> tuple[Target, SyncReport]:
        """Resolve a channel URL/handle, start tracking it and run the baseline backfill."""
        limit = max(1, self.settings.initial_backfill_limit)
        info = self.ytdlp.channel_info(channel_url, recent=limit)
        existing = self.db.get_target(info.channel_id)
        target = Target(info.channel_id, "channel", existing.display_name if existing else info.name, custom_folder)
        self.db.add_target(target)
        target = self.db.get_target(info.channel_id) or target
        self.ensure_channel_show(target.target_id, target.display_name, channel_info=info)

        feed: Feed | FeedError | None = None
        try:
            feed = asyncio.run(
                fetch_feeds([("channel", target.target_id)], timeout=self.settings.http_timeout_seconds)
            ).get(target.target_id)
        except RuntimeError:  # already inside an event loop (should not happen: callers use threads)
            feed = None
        feed = feed if isinstance(feed, Feed) else None

        if self.db.get_state(target.target_id).backfill_cutoff:
            # Already baselined: never move the cutoff; just catch up if the feed is reachable.
            report = self.poll_channel(target, feed) if feed is not None else SyncReport(
                target.target_id, name=target.display_name, skipped=True
            )
        else:
            report = self._backfill(target, feed, recent_ids=info.recent_video_ids, channel_info=info)
        return target, report

    # ------------------------------------------------------------------ playlists
    def sync_playlist(self, playlist_id: str, feed: Feed | None = None, force: bool = False) -> SyncReport:
        """Mirror a playlist into ``<Title> [Playlist]/Season 01/S01E<NN>`` (add, renumber, prune)."""
        target = self.db.get_target(playlist_id)
        report = SyncReport(playlist_id, name=target.display_name if target else playlist_id)
        state = self.db.get_state(playlist_id)
        now = naming.utcnow()

        if not force and feed is not None and state.feed_fingerprint == feed.fingerprint():
            last_full = naming.parse_datetime(state.last_full_sync)
            if last_full and now - last_full < timedelta(hours=self.settings.playlist_full_resync_hours):
                report.skipped = True
                self.db.update_state(playlist_id, last_polled_at=_iso(now))
                return report

        try:
            listing = self.ytdlp.flat_playlist(playlist_id)
        except YtDlpError as exc:
            report.error, report.exception = str(exc), exc
            log.error("Playlist listing failed for %s: %s", playlist_id, exc)
            return report

        report.name = listing.title
        show_title = target.display_name if target and target.custom_folder else listing.title
        folder = self._folder_for(
            playlist_id, naming.playlist_folder(listing.title, target.custom_folder if target else None)
        )
        show_dir = self.library.path(folder)
        if not self.library.has_show(folder):
            poster_url = listing.thumbnail_url
            poster = self._download_image(poster_url)
            if poster is None and listing.entries:
                poster = self._thumbnail(listing.entries[0].video_id)
            xml = tvshow_xml(
                ShowInfo(
                    title=show_title,
                    unique_id=playlist_id,
                    plot=listing.description,
                    studio=listing.uploader or "YouTube",
                    genres=["YouTube", "Playlist"],
                )
            )
            self.library.write_show(folder, xml, poster)
            report.changed_paths.add(show_dir)

        entries = listing.entries
        total = len(entries)
        existing = self.db.playlist_items(playlist_id)
        desired = {e.video_id for e in entries}

        # 1. Prune items removed upstream.
        for vid, item in existing.items():
            if vid not in desired:
                self.library.delete_episode(item.file_stem)
                self.db.delete_playlist_item(playlist_id, vid)
                report.removed.append(vid)
                report.changed_paths.add(show_dir)
                log.info("Pruned %s from playlist %s", vid, playlist_id)

        # 2. Renumber survivors whose position changed (two-phase to avoid name clashes).
        moves: list[tuple[str, int, str, str]] = []  # (video_id, index, old_stem, new_stem)
        for index, entry in enumerate(entries, start=1):
            item = existing.get(entry.video_id)
            if not item:
                continue
            title_part = item.file_stem.rsplit("/", 1)[-1].split(" - ", 1)[-1]
            new_stem = f"{folder}/{naming.playlist_episode_stem(index, title_part, total)}"
            if new_stem != item.file_stem:
                moves.append((entry.video_id, index, item.file_stem, new_stem))
        staged = []
        for vid, index, old_stem, new_stem in moves:
            temp = f"{folder}/Season 01/.renaming-{vid}"
            self.library.rename_episode(old_stem, temp)
            staged.append((vid, index, temp, new_stem))
        for vid, index, temp, new_stem in staged:
            self.library.rename_episode(temp, new_stem)
            nfo_path = self.library.path(new_stem + ".nfo")
            if nfo_path.exists():
                set_episode_number(nfo_path, index)
            self.db.upsert_playlist_item(PlaylistItem(playlist_id, vid, index, new_stem))
            report.renumbered += 1
            report.changed_paths.add(show_dir)

        # 3. Add new items.
        new_ids = self._unblocked([e.video_id for e in entries if e.video_id not in existing])
        report.existing = total - len([e for e in entries if e.video_id not in existing])
        for index, entry in enumerate(entries, start=1):
            if entry.video_id not in new_ids:
                continue
            result = self._ingest_playlist_video(playlist_id, folder, show_title, index, total, entry.video_id,
                                                 entry.title)
            report.record(result)
            if result.status == "added":
                report.changed_paths.add(show_dir)

        self.db.update_state(
            playlist_id,
            feed_fingerprint=feed.fingerprint() if feed is not None else state.feed_fingerprint,
            last_full_sync=_iso(now),
            last_polled_at=_iso(now),
        )
        return report

    def _ingest_playlist_video(
        self, playlist_id: str, folder: str, show_title: str, index: int, total: int, video_id: str, title_hint: str
    ) -> IngestResult:
        info, failure = self._probe(video_id, title_hint)
        if failure:
            return failure
        assert info is not None
        title = info.get("title") or title_hint or video_id
        published = _published_from_info(info, None)
        rel_stem = f"{folder}/{naming.playlist_episode_stem(index, title, total)}"
        nfo = episode_xml(
            EpisodeInfo(
                title=title,
                show_title=show_title,
                season=1,
                episode=index,
                video_id=video_id,
                plot=info.get("description") or "",
                aired=published,
                runtime_minutes=_runtime_minutes(info),
                studio=info.get("channel") or info.get("uploader") or "",
                genres=[g for g in (info.get("categories") or []) if g],
            )
        )
        strm = self.library.write_episode(rel_stem, self.strm_url(video_id), nfo, self._thumbnail(video_id, info))
        self.db.upsert_playlist_item(PlaylistItem(playlist_id, video_id, index, rel_stem))
        self.db.clear_deferred(video_id)
        return IngestResult(video_id, "added", title=title, published=published, path=strm, show=show_title)

    def track_playlist(self, playlist_id: str, custom_folder: str | None = None) -> tuple[Target, SyncReport]:
        existing = self.db.get_target(playlist_id)
        if existing is None:
            # Register first (display name refined after listing) so custom_folder is honoured.
            self.db.add_target(Target(playlist_id, "playlist", playlist_id, custom_folder))
        report = self.sync_playlist(playlist_id, force=True)
        if report.error and existing is None:
            self.db.remove_target(playlist_id)
            raise report.exception or YtDlpError(report.error)
        display = existing.display_name if existing and existing.display_name != playlist_id else report.name
        target = Target(playlist_id, "playlist", display, custom_folder)
        self.db.add_target(target)
        return self.db.get_target(playlist_id) or target, report

    # ------------------------------------------------------------------ poll loop
    def poll_all(self) -> list[SyncReport]:
        targets = self.db.list_targets()
        feedable = [(t.target_type, t.target_id) for t in targets if t.target_id not in PRIVATE_PLAYLISTS]
        feeds = asyncio.run(
            fetch_feeds(
                feedable, concurrency=self.settings.feed_concurrency, timeout=self.settings.http_timeout_seconds
            )
        )
        reports: list[SyncReport] = []
        for target in targets:
            feed = feeds.get(target.target_id)
            try:
                if target.target_type == "channel":
                    if not isinstance(feed, Feed):
                        reports.append(SyncReport(target.target_id, target.display_name, error=str(feed)))
                        continue
                    reports.append(self.poll_channel(target, feed))
                elif target.target_type == "playlist":
                    reports.append(self.sync_playlist(target.target_id, feed if isinstance(feed, Feed) else None))
                else:
                    log.warning("Unknown target type %r for %s", target.target_type, target.target_id)
            except Exception as exc:  # one broken target must not stop the run
                log.exception("Sync failed for %s", target.target_id)
                reports.append(SyncReport(target.target_id, target.display_name, error=str(exc)))
        changed = set().union(*(r.changed_paths for r in reports)) if reports else set()
        if any(r.changed for r in reports):
            self.jellyfin.refresh(sorted(changed))
        return reports

    def notify(self, reports: list[SyncReport] | SyncReport) -> bool:
        reports = [reports] if isinstance(reports, SyncReport) else reports
        if not any(r.changed for r in reports):
            return False
        return self.jellyfin.refresh(sorted(set().union(*(r.changed_paths for r in reports))))


def build_pipeline(settings: Settings) -> Pipeline:
    db = Database(settings.database_path)
    return Pipeline(settings, db, YtDlpClient(settings.cookies_path))
