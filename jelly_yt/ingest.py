"""Curated library: single videos and whole playlists as .strm stubs served by ytdlp2STRM.

Channels are not mirrored here; they are handed to ytdlp2STRM via its channel_list.json.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import naming
from .channel_list import add_channel
from .config import Settings
from .db import Database, PlaylistItem, Target
from .jellyfin import JellyfinClient
from .library import Library
from .nfo import EpisodeInfo, ShowInfo, episode_xml, read_unique_id, set_episode_number, tvshow_xml
from .ytdlp import AuthRequired, ChannelInfo, VideoUnavailable, YtDlpClient, YtDlpError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
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
        parts = [f"{len(self.added)} added"]
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.renumbered:
            parts.append(f"{self.renumbered} renumbered")
        if self.deferred:
            parts.append(f"{len(self.deferred)} waiting")
        return f"{self.name or self.target_id}: " + ", ".join(parts)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _published_from_info(info: dict[str, Any]) -> datetime:
    for key in ("release_timestamp", "timestamp"):
        if info.get(key):
            return naming.parse_datetime(info[key])
    for key in ("release_date", "upload_date"):
        parsed = naming.parse_datetime(info.get(key))
        if parsed:
            return parsed
    return naming.utcnow()


def _runtime_minutes(info: dict[str, Any]) -> int | None:
    duration = info.get("duration")
    return max(1, round(float(duration) / 60)) if duration else None


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

    def _defer(self, video_id: str, exc: Exception, *, title: str = "") -> IngestResult:
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
    def add_channel(self, channel_url: str) -> bool:
        """Hand a channel to ytdlp2STRM. Returns True if newly added."""
        if not self.settings.channel_list_path:
            raise RuntimeError("YTDLP2STRM_CHANNEL_LIST is not configured")
        return add_channel(self.settings.channel_list_path, channel_url)

    # ------------------------------------------------------------------ single videos
    def ensure_channel_show(self, channel_id: str, name: str, genres: list[str] | None = None) -> str:
        """Create ``<Channel>/tvshow.nfo`` + ``poster.jpg`` if missing. Returns the folder."""
        folder = self._folder_for(channel_id, naming.channel_folder(name or channel_id))
        if not self.library.has_show(folder):
            try:
                info = self.ytdlp.channel_info_by_id(channel_id)
            except YtDlpError as exc:
                log.warning("Channel metadata lookup failed for %s: %s", channel_id, exc)
                info = ChannelInfo(channel_id=channel_id, name=name)
            xml = tvshow_xml(ShowInfo(
                title=name or info.name, unique_id=channel_id, plot=info.description,
                studio=info.name or name, genres=genres or ["YouTube"],
            ))
            self.library.write_show(folder, xml, self._download_image(info.avatar_url))
        return folder

    def add_video(self, video_id: str) -> IngestResult:
        """Bot entry point for a single video: remembered so waiting items are retried later."""
        self.db.add_request(video_id)
        result = self.ingest_video(video_id)
        if result.ok or result.status == "unavailable":
            self.db.drop_request(video_id)
        return result

    def ingest_video(self, video_id: str) -> IngestResult:
        """Write one video into ``<Channel>/Season <YYYY>/S<YYYY>E<MMDD> - <Title>``."""
        if self.db.is_processed(video_id):
            return IngestResult(video_id, "exists")
        info, failure = self._probe(video_id)
        if failure:
            return failure
        assert info is not None

        title = info.get("title") or video_id
        published = _published_from_info(info)
        channel_id = info.get("channel_id") or "unknown"
        channel_name = info.get("channel") or info.get("uploader") or channel_id
        genres = [g for g in (info.get("categories") or []) if g]

        folder = self.ensure_channel_show(channel_id, channel_name, genres)
        rel_stem = f"{folder}/{naming.channel_episode_stem(published, title)}"
        existing_nfo = self.library.path(rel_stem + ".nfo")
        if existing_nfo.exists() and read_unique_id(existing_nfo) not in (None, video_id):
            rel_stem = f"{rel_stem} [{video_id}]"  # same-day, same-title collision

        nfo = episode_xml(EpisodeInfo(
            title=title, show_title=channel_name, season=published.year,
            episode=int(f"{published.month:02d}{published.day:02d}"), video_id=video_id,
            plot=info.get("description") or "", aired=published, runtime_minutes=_runtime_minutes(info),
            studio=channel_name, genres=genres,
        ))
        strm = self.library.write_episode(
            rel_stem, self.settings.strm_url(video_id), nfo, self._thumbnail(video_id, info)
        )
        self.db.mark_processed(video_id, channel_id, title, _iso(published))
        self.db.clear_deferred(video_id)
        log.info("Added %s -> %s", video_id, rel_stem)
        return IngestResult(video_id, "added", title=title, published=published, path=strm, show=channel_name)

    # ------------------------------------------------------------------ playlists
    def sync_playlist(self, playlist_id: str) -> SyncReport:
        """Mirror a whole playlist into ``<Title> [Playlist]/Season 01/S01E<NN>`` (add, renumber, prune)."""
        target = self.db.get_target(playlist_id)
        report = SyncReport(playlist_id, name=target.display_name if target else playlist_id)
        now = naming.utcnow()
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
            poster = self._download_image(listing.thumbnail_url)
            if poster is None and listing.entries:
                poster = self._thumbnail(listing.entries[0].video_id)
            xml = tvshow_xml(ShowInfo(
                title=show_title, unique_id=playlist_id, plot=listing.description,
                studio=listing.uploader or "YouTube", genres=["YouTube", "Playlist"],
            ))
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

        # 2. Renumber survivors whose position changed (two-phase to avoid name clashes).
        moves = []
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
        new_entries = [e for e in entries if e.video_id not in existing]
        new_ids = set(self._unblocked([e.video_id for e in new_entries]))
        report.existing = total - len(new_entries)
        for index, entry in enumerate(entries, start=1):
            if entry.video_id not in new_ids:
                continue
            result = self._ingest_playlist_video(
                playlist_id, folder, show_title, index, total, entry.video_id, entry.title
            )
            report.record(result)
            if result.status == "added":
                report.changed_paths.add(show_dir)

        self.db.update_state(playlist_id, last_full_sync=_iso(now), last_polled_at=_iso(now))
        return report

    def _ingest_playlist_video(
        self, playlist_id: str, folder: str, show_title: str, index: int, total: int, video_id: str, title_hint: str
    ) -> IngestResult:
        info, failure = self._probe(video_id, title_hint)
        if failure:
            return failure
        assert info is not None
        title = info.get("title") or title_hint or video_id
        published = _published_from_info(info)
        rel_stem = f"{folder}/{naming.playlist_episode_stem(index, title, total)}"
        nfo = episode_xml(EpisodeInfo(
            title=title, show_title=show_title, season=1, episode=index, video_id=video_id,
            plot=info.get("description") or "", aired=published, runtime_minutes=_runtime_minutes(info),
            studio=info.get("channel") or info.get("uploader") or "",
            genres=[g for g in (info.get("categories") or []) if g],
        ))
        strm = self.library.write_episode(
            rel_stem, self.settings.strm_url(video_id), nfo, self._thumbnail(video_id, info)
        )
        self.db.upsert_playlist_item(PlaylistItem(playlist_id, video_id, index, rel_stem))
        self.db.clear_deferred(video_id)
        return IngestResult(video_id, "added", title=title, published=published, path=strm, show=show_title)

    def track_playlist(self, playlist_id: str, custom_folder: str | None = None) -> tuple[Target, SyncReport]:
        existing = self.db.get_target(playlist_id)
        if existing is None:
            self.db.add_target(Target(playlist_id, "playlist", playlist_id, custom_folder))
        report = self.sync_playlist(playlist_id)
        if report.error and existing is None:
            self.db.remove_target(playlist_id)
            raise report.exception or YtDlpError(report.error)
        display = existing.display_name if existing and existing.display_name != playlist_id else report.name
        self.db.add_target(Target(playlist_id, "playlist", display, custom_folder))
        return self.db.get_target(playlist_id), report

    # ------------------------------------------------------------------ periodic
    def sync_all(self) -> list[SyncReport]:
        """Resync every tracked playlist and retry waiting single videos."""
        reports: list[SyncReport] = []
        for target in self.db.list_targets("playlist"):
            try:
                reports.append(self.sync_playlist(target.target_id))
            except Exception as exc:  # one broken playlist must not stop the run
                log.exception("Sync failed for %s", target.target_id)
                reports.append(SyncReport(target.target_id, target.display_name, error=str(exc)))

        pending = self._unblocked(self.db.pending_requests())
        if pending:
            singles = SyncReport("videos", name="Single videos")
            for vid in pending:
                result = self.add_video(vid)
                singles.record(result)
            reports.append(singles)
        return reports

    def notify(self, reports: list[SyncReport] | SyncReport) -> bool:
        reports = [reports] if isinstance(reports, SyncReport) else reports
        if not any(r.changed for r in reports):
            return False
        return self.jellyfin.refresh(sorted(set().union(*(r.changed_paths for r in reports))))


def build_pipeline(settings: Settings) -> Pipeline:
    return Pipeline(settings, Database(settings.database_path), YtDlpClient(settings.cookies_path))
