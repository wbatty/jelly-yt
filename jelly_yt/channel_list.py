"""Read/append ytdlp2STRM's ``plugins/youtube/channel_list.json`` (a JSON array of strings).

The file is usually bind-mounted into both containers as a single file, so it
is rewritten *in place*: an atomic rename would swap the inode and the other
container would keep seeing the old contents.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

_lock = threading.Lock()


class ChannelListError(Exception):
    pass


def _key(entry: str) -> str:
    """Normalise for duplicate detection: host/scheme/trailing slash/case-insensitive."""
    text = entry.strip().lower().rstrip("/")
    text = re.sub(r"^(https?://)?((www|m)\.)?youtube\.com/", "", text)
    return re.sub(r"/(videos|featured)$", "", text)


def read_channels(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError as exc:
        raise ChannelListError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ChannelListError(f"{path} must be a JSON array of strings")
    return data


def add_channel(path: Path, entry: str) -> bool:
    """Append ``entry`` unless an equivalent one exists. Returns True if the file changed."""
    with _lock:
        channels = read_channels(path)
        if any(_key(c) == _key(entry) for c in channels):
            return False
        channels.append(entry)
        payload = json.dumps(channels, indent=4, ensure_ascii=False) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "r+" if path.exists() else "w", encoding="utf-8") as fh:
            fh.seek(0)
            fh.write(payload)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
        return True
