"""Kodi/Jellyfin sidecar .nfo generation (bypasses all external scrapers)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _clean(text: str | None) -> str:
    return _ILLEGAL_XML.sub("", text or "").strip()


@dataclass
class ShowInfo:
    title: str
    unique_id: str
    plot: str = ""
    studio: str = ""
    genres: list[str] = field(default_factory=list)


@dataclass
class EpisodeInfo:
    title: str
    show_title: str
    season: int
    episode: int
    video_id: str
    plot: str = ""
    aired: datetime | None = None
    runtime_minutes: int | None = None
    studio: str = ""
    genres: list[str] = field(default_factory=list)


def _sub(parent: ET.Element, tag: str, text: str | int | None, **attrs: str) -> ET.Element | None:
    if text is None or text == "":
        return None
    el = ET.SubElement(parent, tag, attrs)
    el.text = _clean(str(text))
    return el


def _serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return _XML_DECL + ET.tostring(root, encoding="unicode") + "\n"


def tvshow_xml(show: ShowInfo) -> str:
    root = ET.Element("tvshow")
    _sub(root, "title", show.title)
    _sub(root, "showtitle", show.title)
    _sub(root, "plot", show.plot)
    _sub(root, "studio", show.studio)
    for genre in show.genres:
        _sub(root, "genre", genre)
    _sub(root, "uniqueid", show.unique_id, type="youtube", default="true")
    return _serialize(root)


def episode_xml(ep: EpisodeInfo) -> str:
    root = ET.Element("episodedetails")
    _sub(root, "title", ep.title)
    _sub(root, "showtitle", ep.show_title)
    _sub(root, "season", ep.season)
    _sub(root, "episode", ep.episode)
    _sub(root, "plot", ep.plot)
    if ep.aired:
        day = ep.aired.strftime("%Y-%m-%d")
        _sub(root, "aired", day)
        _sub(root, "premiered", day)
    if ep.runtime_minutes:
        _sub(root, "runtime", ep.runtime_minutes)
    _sub(root, "studio", ep.studio)
    for genre in ep.genres:
        _sub(root, "genre", genre)
    _sub(root, "uniqueid", ep.video_id, type="youtube", default="true")
    return _serialize(root)


def set_episode_number(nfo_path: Path, episode: int) -> None:
    """Rewrite ``<episode>`` in an existing episode .nfo (used when playlist items shift)."""
    tree = ET.parse(nfo_path)
    root = tree.getroot()
    el = root.find("episode")
    if el is None:
        el = ET.SubElement(root, "episode")
    el.text = str(episode)
    nfo_path.write_text(_serialize(root), encoding="utf-8")


def read_unique_id(nfo_path: Path) -> str | None:
    try:
        root = ET.parse(nfo_path).getroot()
    except (ET.ParseError, OSError):
        return None
    el = root.find("uniqueid")
    return el.text.strip() if el is not None and el.text else None
