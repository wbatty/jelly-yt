"""Filesystem writer for .strm / .nfo / -thumb.jpg / tvshow.nfo / poster.jpg."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

EPISODE_SUFFIXES = (".strm", ".nfo", "-thumb.jpg")


def atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, rel: str) -> Path:
        resolved = (self.root / rel).resolve()
        root = self.root.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Refusing to touch path outside MEDIA_ROOT: {rel}")
        return resolved

    @staticmethod
    def episode_files(base: Path) -> list[Path]:
        return [base.with_name(base.name + suffix) for suffix in EPISODE_SUFFIXES]

    # -- shows -------------------------------------------------------------------
    def has_show(self, folder: str) -> bool:
        return (self.path(folder) / "tvshow.nfo").is_file()

    def write_show(self, folder: str, tvshow_xml: str, poster: bytes | None) -> None:
        show = self.path(folder)
        atomic_write(show / "tvshow.nfo", tvshow_xml)
        if poster:
            atomic_write(show / "poster.jpg", poster)

    # -- episodes ----------------------------------------------------------------
    def episode_exists(self, rel_stem: str) -> bool:
        return self.path(rel_stem).with_name(self.path(rel_stem).name + ".strm").is_file()

    def write_episode(self, rel_stem: str, strm_url: str, nfo_xml: str, thumb: bytes | None) -> Path:
        base = self.path(rel_stem)
        strm, nfo, jpg = self.episode_files(base)
        # Write metadata first so the scanner never sees a bare .strm.
        atomic_write(nfo, nfo_xml)
        if thumb:
            atomic_write(jpg, thumb)
        atomic_write(strm, strm_url + "\n")
        return strm

    def delete_episode(self, rel_stem: str) -> list[Path]:
        base = self.path(rel_stem)
        removed = []
        for f in self.episode_files(base):
            try:
                f.unlink()
                removed.append(f)
            except FileNotFoundError:
                pass
        self._prune_empty(base.parent)
        return removed

    def rename_episode(self, old_rel_stem: str, new_rel_stem: str) -> None:
        old_base, new_base = self.path(old_rel_stem), self.path(new_rel_stem)
        new_base.parent.mkdir(parents=True, exist_ok=True)
        for old, new in zip(self.episode_files(old_base), self.episode_files(new_base)):
            if old.exists():
                os.replace(old, new)
        if old_base.parent != new_base.parent:
            self._prune_empty(old_base.parent)

    def _prune_empty(self, directory: Path) -> None:
        root = self.root.resolve()
        try:
            if directory.resolve() != root and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
