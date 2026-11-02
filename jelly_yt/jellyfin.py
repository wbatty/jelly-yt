"""Jellyfin library refresh notifications."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(timeout=10.0)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.jellyfin_url and self.settings.jellyfin_api_key)

    def _headers(self) -> dict[str, str]:
        token = self.settings.jellyfin_api_key or ""
        return {
            "Authorization": f'MediaBrowser Client="jelly-yt", Device="jelly-yt", DeviceId="jelly-yt", '
            f'Version="1.0.0", Token="{token}"',
            "X-Emby-Token": token,
        }

    def _url(self, path: str) -> str:
        return f"{self.settings.jellyfin_url.rstrip('/')}{path}"

    def _jellyfin_path(self, local: Path) -> str:
        rel = Path(local).resolve().relative_to(self.settings.media_root.resolve())
        return str(PurePosixPath(self.settings.jellyfin_media_root) / PurePosixPath(*rel.parts))

    def refresh(self, changed_paths: list[Path] | None = None) -> bool:
        """Trigger a scan. Uses a targeted ``/Library/Media/Updated`` notification when
        ``JELLYFIN_MEDIA_ROOT`` is configured (fast), else a full ``/Library/Refresh``."""
        if not self.enabled:
            log.info("Jellyfin not configured; skipping library refresh")
            return False
        try:
            if changed_paths and self.settings.jellyfin_media_root:
                updates = [
                    {"Path": self._jellyfin_path(p), "UpdateType": "Created"} for p in sorted(set(changed_paths))
                ]
                resp = self._client.post(
                    self._url("/Library/Media/Updated"), json={"Updates": updates}, headers=self._headers()
                )
            else:
                resp = self._client.post(self._url("/Library/Refresh"), headers=self._headers())
            resp.raise_for_status()
            log.info("Jellyfin library refresh requested (HTTP %s)", resp.status_code)
            return True
        except (httpx.HTTPError, ValueError) as exc:
            log.error("Jellyfin refresh failed: %s", exc)
            return False
